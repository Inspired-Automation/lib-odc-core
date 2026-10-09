"""Upload filed ODC documents to a client's file server over FTPS or SFTP.

Added in 0.10.0 as SFTP only. 0.13.0 added FTPS and made it the default:
the client's dev/staging and live servers both accept explicit FTPS
(``AUTH TLS`` on port 21), so ``config["sftp"]["protocol"]`` is ``"ftps"``
unless set to ``"sftp"``. The module, class and config-block names keep
"sftp" so callers need no code change.

Switched on per client by config, not by a database column: a job uploads
when its ``client_name`` is listed in ``config["sftp"]["clients"]``. This is
expected to replace the PDF Auto copy (``pdf_auto_copy``) for those clients
in time; for now the two are independent.

Every upload is atomic and recorded locally, whichever protocol is used:

1. upload to ``<name>.part`` in ``remote_dir`` (the login folder when blank),
2. check the remote size matches the local file,
3. rename to the final name,
4. write a ``<file>.uploaded`` sidecar next to the local file.

A remote file that already exists with the same size counts as uploaded.
Anything not yet marked is picked up by ``pending_files()`` on a later run
(the "sweep"), because a bill the duplicate check skips is never downloaded
again and so would otherwise never be re-uploaded.

FTPS: the control and data channels are both encrypted (``PROT P``) and the
server certificate is verified against the Windows/system CA store, with
hostname checking. Data connections reuse the control connection's TLS
session, which many FTPS servers require.

SFTP: host keys are pinned, trust-on-first-use: a configured ``host_key``
wins; otherwise the key saved in ``known_hosts_file`` on the first
connection is the only one accepted afterwards. A changed key refuses the
connection. ``paramiko.AutoAddPolicy`` is never used.

Credentials come only from the caller's ``config["sftp"]`` (``config.yaml`` /
the Control Room Runtime config panel) and are never logged.
"""

from __future__ import annotations

import base64
import ftplib
import hashlib
import json
import logging
import posixpath
import ssl
from datetime import UTC, date, datetime
from pathlib import Path

import paramiko

from . import db

logger = logging.getLogger(__name__)

PROTOCOL_FTPS = "ftps"
PROTOCOL_SFTP = "sftp"
DEFAULT_PROTOCOL = PROTOCOL_FTPS
DEFAULT_PORTS = {PROTOCOL_FTPS: 21, PROTOCOL_SFTP: 22}
DEFAULT_TIMEOUT_S = 60
DEFAULT_KNOWN_HOSTS_FILE = "config/sftp_known_hosts"
SIDECAR_SUFFIX = ".uploaded"
PART_SUFFIX = ".part"
_MIN_FILE_SIZE = 1024  # bytes; smaller files are VOID in file_save_as / grab_all

# Errors an upload can raise from either protocol's client library.
_TRANSFER_ERRORS = (paramiko.SSHException, ftplib.Error, EOFError, OSError)

_PENDING_GRAB_ALL_SQL = """
SELECT g.[client_file_path]
FROM {grab_all_data} AS g
JOIN {jobs} AS j ON j.[id] = g.[job_id]
WHERE j.[supplier] = ?
AND   j.[client_name] = ?
AND   g.[bill_date_corrected] >= ?
"""

_PENDING_SCRAPE_SQL = """
SELECT s.[client_file_path]
FROM {scrape_data} AS s
JOIN {job_details} AS d ON d.[id] = s.[job_details_id]
JOIN {jobs} AS j ON j.[id] = d.[job_id]
WHERE j.[supplier] = ?
AND   j.[client_name] = ?
AND   s.[bill_date_corrected] >= ?
"""


class SftpUploadError(RuntimeError):
    """The upload connection could not be made (bad config, auth, host key/certificate, network)."""


def _normalise(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def is_enabled_for(client_name: str | None, config: dict) -> bool:
    """True when client_name is listed in config["sftp"]["clients"] (case/space-insensitive)."""
    clients = (config.get("sftp") or {}).get("clients") or []
    wanted = _normalise(client_name)
    return bool(wanted) and any(_normalise(c) == wanted for c in clients)


def sidecar_path(local_path: str | Path) -> Path:
    path = Path(local_path)
    return path.with_name(path.name + SIDECAR_SUFFIX)


def is_uploaded(local_path: str | Path) -> bool:
    """True when the local file has an upload sidecar."""
    return sidecar_path(local_path).exists()


# ------------------------------------------------------------------- SFTP

def fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH-style SHA256 fingerprint, e.g. 'SHA256:abc...'."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _host_entry(host: str, port: int) -> str:
    return host if port == DEFAULT_PORTS[PROTOCOL_SFTP] else f"[{host}]:{port}"


def _parse_host_key(value: str) -> paramiko.PKey:
    """Parse 'ssh-ed25519 AAAA...' (optionally with a trailing comment) into a key."""
    parts = value.split()
    if len(parts) < 2:
        raise SftpUploadError("sftp.host_key must look like '<key-type> <base64>'")
    try:
        return paramiko.PKey.from_type_string(parts[0], base64.b64decode(parts[1]))
    except (ValueError, paramiko.SSHException) as exc:
        raise SftpUploadError(f"sftp.host_key could not be parsed: {exc}") from exc


class _TrustOnFirstUsePolicy(paramiko.MissingHostKeyPolicy):
    """Accept and save the server key only when nothing is pinned for this host yet.

    paramiko calls missing_host_key() both when the host is unknown and when
    the host is known under a *different key type*; the second case is
    rejected, otherwise a server offering a new key type would slip past
    the pin.
    """

    def __init__(self, known_hosts_file: Path, host_entry: str) -> None:
        self._file = known_hosts_file
        self._entry = host_entry

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
        if client.get_host_keys().lookup(self._entry):
            raise SftpUploadError(
                f"SFTP host key for {self._entry} is not the pinned one "
                f"(server offered {key.get_name()} {fingerprint(key)}); refusing to connect"
            )
        client.get_host_keys().add(self._entry, key.get_name(), key)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(self._file))
        logger.warning(
            "SFTP_UPLOAD - first connection to %s: pinned host key %s %s in %s. "
            "Confirm this fingerprint with the server owner.",
            self._entry, key.get_name(), fingerprint(key), self._file,
        )


class _SftpConnection:
    """paramiko SFTP behind the small interface SftpSession uploads through."""

    def __init__(self, host: str, port: int, username: str, password: str, timeout_s: float, sftp_cfg: dict) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout_s = timeout_s
        self._host_key: str = (sftp_cfg.get("host_key") or "").strip()
        self._known_hosts = Path(sftp_cfg.get("known_hosts_file") or DEFAULT_KNOWN_HOSTS_FILE)
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    def connect(self) -> None:
        entry = _host_entry(self._host, self._port)
        client = paramiko.SSHClient()
        if self._host_key:
            key = _parse_host_key(self._host_key)
            client.get_host_keys().add(entry, key.get_name(), key)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            if self._known_hosts.exists():
                client.load_host_keys(str(self._known_hosts))
            client.set_missing_host_key_policy(_TrustOnFirstUsePolicy(self._known_hosts, entry))
        try:
            client.connect(
                self._host, port=self._port, username=self._username, password=self._password,
                timeout=self._timeout_s, banner_timeout=self._timeout_s, auth_timeout=self._timeout_s,
                allow_agent=False, look_for_keys=False,
            )
            self._sftp = client.open_sftp()
        except SftpUploadError:
            client.close()  # host key refused by _TrustOnFirstUsePolicy
            raise
        except paramiko.BadHostKeyException as exc:
            client.close()
            raise SftpUploadError(
                f"SFTP host key for {entry} changed (server offered {fingerprint(exc.key)}, "
                f"pinned {fingerprint(exc.expected_key)}); refusing to connect"
            ) from exc
        except paramiko.AuthenticationException as exc:
            client.close()
            raise SftpUploadError(f"SFTP login to {entry} was rejected (check sftp.username / sftp.password)") from exc
        except (paramiko.SSHException, OSError, EOFError) as exc:
            client.close()
            raise SftpUploadError(f"SFTP connection to {entry} failed: {exc}") from exc
        self._client = client

    def close(self) -> None:
        for closer in (self._sftp, self._client):
            if closer is not None:
                try:
                    closer.close()
                except (paramiko.SSHException, OSError, EOFError):
                    logger.debug("SFTP_UPLOAD - close failed", exc_info=True)
        self._sftp = None
        self._client = None

    def alive(self) -> bool:
        transport = self._client.get_transport() if self._client else None
        return bool(transport and transport.is_active() and self._sftp is not None)

    def size(self, path: str) -> int | None:
        """Remote file size, or None when it does not exist."""
        assert self._sftp is not None
        try:
            return self._sftp.stat(path).st_size
        except FileNotFoundError:
            return None

    def put(self, local: Path, path: str) -> None:
        assert self._sftp is not None
        self._sftp.put(str(local), path, confirm=False)

    def replace(self, src: str, dst: str) -> None:
        assert self._sftp is not None
        try:
            self._sftp.posix_rename(src, dst)
        except OSError:
            # Server without the posix-rename extension: plain rename fails if the target exists.
            try:
                self._sftp.remove(dst)
            except FileNotFoundError:
                pass
            self._sftp.rename(src, dst)

    def remove(self, path: str) -> None:
        assert self._sftp is not None
        self._sftp.remove(path)


# ------------------------------------------------------------------- FTPS

class _SessionReuseFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS whose data connections resume the control connection's TLS session.

    Many FTPS servers refuse a data connection that does not (vsftpd's
    require_ssl_reuse, FileZilla Server); it is harmless where not required.
    """

    def ntransfercmd(self, cmd: str, rest: int | str | None = None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class _FtpsConnection:
    """Explicit FTPS (AUTH TLS, then PROT P) behind the interface SftpSession uploads through."""

    def __init__(self, host: str, port: int, username: str, password: str, timeout_s: float) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._timeout_s = timeout_s
        self._ftp: ftplib.FTP_TLS | None = None

    def connect(self) -> None:
        entry = f"{self._host}:{self._port}"
        ftp = _SessionReuseFTP_TLS(context=ssl.create_default_context(), timeout=self._timeout_s)
        try:
            ftp.connect(self._host, self._port)
            ftp.login(self._username, self._password)  # sends AUTH TLS before USER/PASS
            ftp.prot_p()
            ftp.voidcmd("TYPE I")  # binary; SIZE is also only reliable in binary mode
        except ssl.SSLCertVerificationError as exc:
            ftp.close()
            raise SftpUploadError(
                f"FTPS certificate for {entry} was not trusted ({exc.verify_message}); refusing to connect"
            ) from exc
        except ftplib.error_perm as exc:
            ftp.close()
            if str(exc).startswith("530"):
                raise SftpUploadError(
                    f"FTPS login to {entry} was rejected (check sftp.username / sftp.password)"
                ) from exc
            raise SftpUploadError(f"FTPS connection to {entry} failed: {exc}") from exc
        except ftplib.all_errors as exc:
            ftp.close()
            raise SftpUploadError(f"FTPS connection to {entry} failed: {exc}") from exc
        self._ftp = ftp

    def close(self) -> None:
        if self._ftp is not None:
            try:
                self._ftp.quit()
            except ftplib.all_errors:
                logger.debug("SFTP_UPLOAD - FTPS quit failed", exc_info=True)
                self._ftp.close()
        self._ftp = None

    def alive(self) -> bool:
        if self._ftp is None or self._ftp.sock is None:
            return False
        try:
            self._ftp.voidcmd("NOOP")
        except ftplib.all_errors:
            return False
        return True

    def size(self, path: str) -> int | None:
        """Remote file size, or None when it does not exist."""
        assert self._ftp is not None
        try:
            return self._ftp.size(path)
        except ftplib.error_perm:  # 550: no such file
            return None

    def put(self, local: Path, path: str) -> None:
        assert self._ftp is not None
        with local.open("rb") as fh:
            self._ftp.storbinary(f"STOR {path}", fh)

    def replace(self, src: str, dst: str) -> None:
        assert self._ftp is not None
        try:
            self._ftp.rename(src, dst)
        except ftplib.error_perm:
            # Some servers refuse RNTO onto an existing file.
            try:
                self._ftp.delete(dst)
            except ftplib.error_perm:
                pass
            self._ftp.rename(src, dst)

    def remove(self, path: str) -> None:
        assert self._ftp is not None
        self._ftp.delete(path)


# ---------------------------------------------------------------- session

class SftpSession:
    """One FTPS or SFTP connection for a run. Use as a context manager.

    ``config["sftp"]["protocol"]`` picks the protocol (``"ftps"`` by
    default, or ``"sftp"``); the port defaults to 21 or 22 to match.

    ``upload()`` never raises for an individual file: it returns False and
    logs, so a failed upload never fails the bill that was just saved. After
    one failed reconnect the session marks itself broken and every later
    upload returns False straight away; the next run's sweep catches up.
    """

    def __init__(self, config: dict) -> None:
        sftp_cfg = config.get("sftp") or {}
        self.protocol: str = (sftp_cfg.get("protocol") or DEFAULT_PROTOCOL).strip().lower()
        if self.protocol not in DEFAULT_PORTS:
            raise SftpUploadError(f"sftp.protocol must be 'ftps' or 'sftp', not {self.protocol!r}")
        self.host: str = (sftp_cfg.get("host") or "").strip()
        self.port: int = int(sftp_cfg.get("port") or DEFAULT_PORTS[self.protocol])
        self._username: str = sftp_cfg.get("username") or ""
        self._password: str = sftp_cfg.get("password") or ""
        self.remote_dir: str = (sftp_cfg.get("remote_dir") or "").strip()
        timeout_s = float(sftp_cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
        if self.protocol == PROTOCOL_FTPS:
            self._conn = _FtpsConnection(self.host, self.port, self._username, self._password, timeout_s)
        else:
            self._conn = _SftpConnection(self.host, self.port, self._username, self._password, timeout_s, sftp_cfg)
        self.broken = False

    # ---------------------------------------------------------- connection

    def _check_config(self) -> None:
        missing = [k for k, v in (("host", self.host), ("username", self._username), ("password", self._password)) if not v]
        if missing:
            raise SftpUploadError(f"config sftp.{', sftp.'.join(missing)} not set")

    def connect(self) -> None:
        """Open the connection. Raises SftpUploadError on any failure."""
        self._check_config()
        self._conn.connect()
        logger.info(
            "SFTP_UPLOAD - connected to %s:%d over %s (remote dir %r)",
            self.host, self.port, self.protocol.upper(), self.remote_dir or "(login folder)",
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SftpSession:
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _alive(self) -> bool:
        return self._conn.alive()

    # -------------------------------------------------------------- upload

    def _remote(self, name: str) -> str:
        return posixpath.join(self.remote_dir, name) if self.remote_dir else name

    def _upload_once(self, local: Path, remote_name: str) -> str:
        """Upload one file atomically. Returns the remote path. Raises on failure."""
        final = self._remote(remote_name)
        size = local.stat().st_size
        if self._conn.size(final) == size:
            logger.info("SFTP_UPLOAD - %s already on the server with the same size", final)
            return final
        part = final + PART_SUFFIX
        self._conn.put(local, part)
        remote_size = self._conn.size(part)
        if remote_size != size:
            self._conn.remove(part)
            raise OSError(f"size mismatch after upload ({remote_size} remote vs {size} local)")
        self._conn.replace(part, final)
        return final

    def upload(self, local_path: str | Path, remote_name: str | None = None) -> bool:
        """Upload local_path and write its sidecar. Returns True on success; never raises."""
        local = Path(local_path)
        name = remote_name or local.name
        if self.broken:
            return False
        if is_uploaded(local):
            return True
        if not local.is_file():
            logger.error("SFTP_UPLOAD - local file missing: %s", local)
            return False

        for attempt in (1, 2):
            if not self._alive():
                try:
                    self.close()
                    self.connect()
                except SftpUploadError as exc:
                    logger.error("SFTP_UPLOAD - reconnect failed, no more uploads this run: %s", exc)
                    self.broken = True
                    return False
            try:
                remote = self._upload_once(local, name)
            except _TRANSFER_ERRORS as exc:
                # A dropped connection gets one reconnect; a server-side error
                # (permission, disk full) on a live connection does not.
                if attempt == 1 and not self._alive():
                    logger.warning("SFTP_UPLOAD - connection dropped uploading %s (%s); reconnecting once", name, exc)
                    continue
                logger.error("SFTP_UPLOAD - %s upload failed: %s", name, exc)
                return False
            self._write_sidecar(local, remote)
            logger.info(
                "SFTP_UPLOAD - uploaded %s (%d bytes) over %s to %s:%s",
                local.name, local.stat().st_size, self.protocol.upper(), self.host, remote,
            )
            return True
        return False

    def _write_sidecar(self, local: Path, remote: str) -> None:
        record = {
            "uploaded_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "protocol": self.protocol,
            "host": self.host,
            "remote_path": remote,
            "size": local.stat().st_size,
        }
        try:
            sidecar_path(local).write_text(json.dumps(record), encoding="utf-8")
        except OSError:
            logger.exception("SFTP_UPLOAD - uploaded %s but could not write its sidecar", local)


def pending_files(
    supplier: str, client_name: str, since: date, tables: dict, dsn: str,
) -> list[str]:
    """Local files filed for this supplier and client since `since` that still need uploading.

    Reads client_file_path from ODC_grab_all_data (when tables has
    grab_all_data) and ODC_scrape_data. Skips files with a sidecar, files
    missing locally, and VOID-sized files (< 1 KB). ODC_scrape_data has no
    status column, so VOID is judged by size for both tables.
    """
    since_iso = since.isoformat()
    queries = [_PENDING_SCRAPE_SQL.format(
        scrape_data=tables["scrape_data"], job_details=tables["job_details"], jobs=tables["jobs"],
    )]
    if tables.get("grab_all_data"):
        queries.append(_PENDING_GRAB_ALL_SQL.format(grab_all_data=tables["grab_all_data"], jobs=tables["jobs"]))

    def work(conn) -> list[str]:
        cursor = conn.cursor()
        paths: list[str] = []
        for sql in queries:
            cursor.execute(sql, supplier, client_name, since_iso)
            paths.extend(row[0] for row in cursor.fetchall() if row[0])
        return paths

    found = db.run(dsn, work, description="sftp_upload.pending_files")

    pending: list[str] = []
    seen: set[str] = set()
    for raw in found:
        if raw in seen:
            continue
        seen.add(raw)
        path = Path(raw)
        try:
            if not path.is_file() or is_uploaded(path) or path.stat().st_size < _MIN_FILE_SIZE:
                continue
        except OSError:
            logger.debug("SFTP_UPLOAD - cannot stat %s; skipping", raw, exc_info=True)
            continue
        pending.append(raw)
    logger.info(
        "SFTP_UPLOAD - %d of %d filed document(s) for %s / %s since %s still to upload",
        len(pending), len(seen), supplier, client_name, since_iso,
    )
    return pending
