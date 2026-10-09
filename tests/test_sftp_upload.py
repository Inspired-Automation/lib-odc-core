import ftplib
import json
import ssl
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from odc_core import sftp_upload
from odc_core.sftp_upload import SftpSession, SftpUploadError

TABLES = {
    "jobs": "ODC_jobs",
    "job_details": "ODC_job_details",
    "scrape_data": "ODC_scrape_data",
    "grab_all_data": "ODC_grab_all_data",
}


def _config(tmp_path, **overrides):
    cfg = {
        "host": "sftp.example.test",
        "username": "user",
        "password": "secret",
        "remote_dir": "",
        "known_hosts_file": str(tmp_path / "known_hosts"),
    }
    cfg.update(overrides)
    return {"sftp": cfg}


def _pdf(tmp_path, name="EI_ACC_Elec_INV1_20260930.pdf", size=2048):
    path = tmp_path / name
    path.write_bytes(b"%PDF-" + b"x" * (size - 5))
    return path


class _FakeConnection:
    """In-memory server behind the interface SftpSession uploads through."""

    def __init__(self, remote_files=None):
        self.remote = dict(remote_files or {})
        self.put = MagicMock(side_effect=self._put)
        self.replace = MagicMock(side_effect=self._replace)

    def alive(self):
        return True

    def size(self, path):
        return self.remote.get(path)

    def _put(self, local, path):
        self.remote[path] = Path(local).stat().st_size

    def _replace(self, src, dst):
        self.remote[dst] = self.remote.pop(src)

    def remove(self, path):
        del self.remote[path]


def _session_with_fake_server(tmp_path, remote_files=None, **cfg):
    session = SftpSession(_config(tmp_path, **cfg))
    conn = _FakeConnection(remote_files)
    session._conn = conn
    return session, conn


# ---------------------------------------------------------------- is_enabled_for

def test_is_enabled_for_matches_case_and_space_insensitively():
    config = {"sftp": {"clients": ["Horizon", "Other Co"]}}
    assert sftp_upload.is_enabled_for(" horizon ", config) is True
    assert sftp_upload.is_enabled_for("OTHER  CO", config) is True
    assert sftp_upload.is_enabled_for("BoxFish", config) is False


def test_is_enabled_for_missing_block_or_blank_client():
    assert sftp_upload.is_enabled_for("Horizon", {}) is False
    assert sftp_upload.is_enabled_for("Horizon", {"sftp": {"clients": []}}) is False
    assert sftp_upload.is_enabled_for("", {"sftp": {"clients": ["Horizon"]}}) is False


# ---------------------------------------------------------------- protocol choice

def test_protocol_defaults_to_ftps_on_port_21(tmp_path):
    session = SftpSession(_config(tmp_path))
    assert session.protocol == "ftps"
    assert session.port == 21
    assert isinstance(session._conn, sftp_upload._FtpsConnection)


def test_protocol_sftp_defaults_to_port_22(tmp_path):
    session = SftpSession(_config(tmp_path, protocol=" SFTP "))
    assert session.protocol == "sftp"
    assert session.port == 22
    assert isinstance(session._conn, sftp_upload._SftpConnection)


def test_explicit_port_wins(tmp_path):
    assert SftpSession(_config(tmp_path, port=2121)).port == 2121


def test_unknown_protocol_is_rejected(tmp_path):
    with pytest.raises(SftpUploadError, match="sftp.protocol"):
        SftpSession(_config(tmp_path, protocol="ftp"))


# ------------------------------------------------------------------------ upload

def test_upload_writes_part_then_renames_and_writes_sidecar(tmp_path):
    local = _pdf(tmp_path)
    session, conn = _session_with_fake_server(tmp_path)

    assert session.upload(local) is True

    conn.put.assert_called_once_with(local, local.name + ".part")
    conn.replace.assert_called_once_with(local.name + ".part", local.name)
    assert conn.remote == {local.name: 2048}
    record = json.loads(sftp_upload.sidecar_path(local).read_text(encoding="utf-8"))
    assert record["remote_path"] == local.name
    assert record["size"] == 2048
    assert record["protocol"] == "ftps"
    assert "secret" not in sftp_upload.sidecar_path(local).read_text(encoding="utf-8")


def test_upload_uses_remote_dir(tmp_path):
    local = _pdf(tmp_path)
    session, conn = _session_with_fake_server(tmp_path, remote_dir="inbound/invoices")

    assert session.upload(local) is True
    assert conn.remote == {f"inbound/invoices/{local.name}": 2048}


def test_upload_existing_same_size_is_done_without_put(tmp_path):
    local = _pdf(tmp_path)
    session, conn = _session_with_fake_server(tmp_path, remote_files={local.name: 2048})

    assert session.upload(local) is True
    conn.put.assert_not_called()
    assert sftp_upload.is_uploaded(local)


def test_upload_existing_different_size_is_replaced(tmp_path):
    local = _pdf(tmp_path)
    session, conn = _session_with_fake_server(tmp_path, remote_files={local.name: 10})

    assert session.upload(local) is True
    conn.put.assert_called_once()
    assert conn.remote[local.name] == 2048


def test_upload_size_mismatch_removes_part_and_fails(tmp_path):
    local = _pdf(tmp_path)
    session, conn = _session_with_fake_server(tmp_path)
    conn.put.side_effect = lambda local_path, path: conn.remote.__setitem__(path, 1)

    assert session.upload(local) is False
    assert conn.remote == {}
    assert not sftp_upload.is_uploaded(local)


def test_upload_skips_file_with_sidecar(tmp_path):
    local = _pdf(tmp_path)
    sftp_upload.sidecar_path(local).write_text("{}", encoding="utf-8")
    session, conn = _session_with_fake_server(tmp_path)

    assert session.upload(local) is True
    conn.put.assert_not_called()


@pytest.mark.parametrize("error", [PermissionError("denied"), ftplib.error_perm("553 Not allowed")])
def test_upload_server_error_on_live_connection_fails_without_reconnect(tmp_path, error):
    local = _pdf(tmp_path)
    session, conn = _session_with_fake_server(tmp_path)
    conn.put.side_effect = error
    session.connect = MagicMock()

    assert session.upload(local) is False
    session.connect.assert_not_called()
    assert session.broken is False


def test_upload_reconnects_once_after_dropped_connection(tmp_path):
    local = _pdf(tmp_path)
    session, conn = _session_with_fake_server(tmp_path)
    state = {"alive": True}
    conn.alive = lambda: state["alive"]

    def drop_then_work(local_path, path):
        if conn.put.call_count == 1:
            state["alive"] = False
            raise EOFError("connection closed")
        conn._put(local_path, path)

    conn.put.side_effect = drop_then_work
    session.close = MagicMock()
    session.connect = MagicMock(side_effect=lambda: state.update(alive=True))

    assert session.upload(local) is True
    session.connect.assert_called_once()
    assert conn.remote == {local.name: 2048}


def test_broken_session_after_failed_reconnect(tmp_path):
    local = _pdf(tmp_path)
    session = SftpSession(_config(tmp_path))
    session._alive = lambda: False
    session.connect = MagicMock(side_effect=SftpUploadError("network down"))

    assert session.upload(local) is False
    assert session.broken is True
    assert session.upload(local) is False
    session.connect.assert_called_once()


def test_connect_requires_credentials(tmp_path):
    with pytest.raises(SftpUploadError, match="sftp.password"):
        SftpSession(_config(tmp_path, password="")).connect()


# ---------------------------------------------------------------- FTPS connection

@pytest.fixture
def ftp_cls():
    with patch.object(sftp_upload, "_SessionReuseFTP_TLS") as cls:
        yield cls


def test_ftps_connect_logs_in_over_tls_and_protects_data(tmp_path, ftp_cls):
    session = SftpSession(_config(tmp_path, port=2121, timeout_s=15))
    session.connect()

    ftp = ftp_cls.return_value
    context = ftp_cls.call_args.kwargs["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert ftp_cls.call_args.kwargs["timeout"] == 15
    ftp.connect.assert_called_once_with("sftp.example.test", 2121)
    ftp.login.assert_called_once_with("user", "secret")
    ftp.prot_p.assert_called_once()
    ftp.voidcmd.assert_called_with("TYPE I")


def test_ftps_bad_login_raises_clear_error(tmp_path, ftp_cls):
    ftp_cls.return_value.login.side_effect = ftplib.error_perm("530 Access denied.")
    with pytest.raises(SftpUploadError, match="rejected"):
        SftpSession(_config(tmp_path)).connect()
    ftp_cls.return_value.close.assert_called_once()


def test_ftps_untrusted_certificate_raises_clear_error(tmp_path, ftp_cls):
    exc = ssl.SSLCertVerificationError("cert")
    exc.verify_message = "self-signed certificate"
    ftp_cls.return_value.login.side_effect = exc
    with pytest.raises(SftpUploadError, match="not trusted.*self-signed"):
        SftpSession(_config(tmp_path)).connect()


def test_ftps_network_error_raises(tmp_path, ftp_cls):
    ftp_cls.return_value.connect.side_effect = TimeoutError("timed out")
    with pytest.raises(SftpUploadError, match="FTPS connection .* failed"):
        SftpSession(_config(tmp_path)).connect()


def _ftps_conn():
    conn = sftp_upload._FtpsConnection("h", 21, "u", "p", 5)
    conn._ftp = MagicMock()
    return conn, conn._ftp


def test_ftps_size_missing_file_is_none():
    conn, ftp = _ftps_conn()
    ftp.size.side_effect = ftplib.error_perm("550 No such file")
    assert conn.size("a.pdf") is None
    ftp.size.side_effect = None
    ftp.size.return_value = 2048
    assert conn.size("a.pdf") == 2048


def test_ftps_put_stores_binary(tmp_path):
    conn, ftp = _ftps_conn()
    conn.put(_pdf(tmp_path), "a.pdf.part")
    assert ftp.storbinary.call_args.args[0] == "STOR a.pdf.part"


def test_ftps_replace_deletes_target_when_rename_refused():
    conn, ftp = _ftps_conn()
    ftp.rename.side_effect = [ftplib.error_perm("553 exists"), None]
    conn.replace("a.part", "a")
    ftp.delete.assert_called_once_with("a")
    assert ftp.rename.call_count == 2


def test_ftps_alive_uses_noop():
    conn, ftp = _ftps_conn()
    assert conn.alive() is True
    ftp.voidcmd.side_effect = EOFError()
    assert conn.alive() is False
    assert sftp_upload._FtpsConnection("h", 21, "u", "p", 5).alive() is False


def test_ftps_data_connection_reuses_control_tls_session():
    ftp = sftp_upload._SessionReuseFTP_TLS()
    ftp.host = "h"
    ftp._prot_p = True
    ftp.sock = MagicMock()
    ftp.context = MagicMock()
    raw = MagicMock()
    with patch.object(ftplib.FTP, "ntransfercmd", return_value=(raw, None)):
        conn, _ = ftp.ntransfercmd("STOR x")
    ftp.context.wrap_socket.assert_called_once_with(raw, server_hostname="h", session=ftp.sock.session)
    assert conn is ftp.context.wrap_socket.return_value


# ---------------------------------------------------------------- SFTP connection

def test_sftp_replace_falls_back_to_rename_without_posix_rename():
    conn = sftp_upload._SftpConnection("h", 22, "u", "p", 5, {})
    conn._sftp = MagicMock()
    conn._sftp.posix_rename.side_effect = OSError("unsupported")
    conn._sftp.remove.side_effect = FileNotFoundError("a")
    conn.replace("a.part", "a")
    conn._sftp.rename.assert_called_once_with("a.part", "a")


def test_sftp_size_missing_file_is_none():
    conn = sftp_upload._SftpConnection("h", 22, "u", "p", 5, {})
    conn._sftp = MagicMock()
    conn._sftp.stat.side_effect = FileNotFoundError("a")
    assert conn.size("a") is None


# ---------------------------------------------------------------- host keys (real keys)

@pytest.fixture(scope="module")
def rsa_keys():
    return paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024)


def test_tofu_first_connection_pins_and_saves_key(tmp_path, rsa_keys):
    key, _ = rsa_keys
    known = tmp_path / "config" / "sftp_known_hosts"
    client = paramiko.SSHClient()
    policy = sftp_upload._TrustOnFirstUsePolicy(known, "sftp.example.test")

    policy.missing_host_key(client, "sftp.example.test", key)

    reloaded = paramiko.HostKeys(str(known))
    assert reloaded.lookup("sftp.example.test")[key.get_name()] == key


def test_tofu_rejects_when_host_already_pinned(tmp_path, rsa_keys):
    pinned, _ = rsa_keys
    known = tmp_path / "known_hosts"
    client = paramiko.SSHClient()
    client.get_host_keys().add("sftp.example.test", "ssh-ed25519", pinned)  # pinned under another key type
    policy = sftp_upload._TrustOnFirstUsePolicy(known, "sftp.example.test")

    with pytest.raises(SftpUploadError, match="not the pinned one"):
        policy.missing_host_key(client, "sftp.example.test", pinned)
    assert not known.exists()


def test_parse_host_key_round_trip(rsa_keys):
    key, _ = rsa_keys
    parsed = sftp_upload._parse_host_key(f"{key.get_name()} {key.get_base64()} comment")
    assert parsed == key


def test_parse_host_key_rejects_garbage():
    with pytest.raises(SftpUploadError):
        sftp_upload._parse_host_key("not-a-key")


def test_connect_with_pinned_host_key_uses_reject_policy(tmp_path, rsa_keys):
    key, _ = rsa_keys
    session = SftpSession(_config(tmp_path, protocol="sftp", host_key=f"{key.get_name()} {key.get_base64()}"))
    with patch.object(paramiko, "SSHClient") as client_cls:
        client = client_cls.return_value
        session.connect()
    policy = client.set_missing_host_key_policy.call_args.args[0]
    assert isinstance(policy, paramiko.RejectPolicy)
    client.get_host_keys.return_value.add.assert_called_once_with("sftp.example.test", key.get_name(), key)
    assert client.connect.call_args.kwargs["look_for_keys"] is False


def test_connect_bad_host_key_raises_clear_error(tmp_path, rsa_keys):
    key, other = rsa_keys
    session = SftpSession(_config(tmp_path, protocol="sftp"))
    with patch.object(paramiko, "SSHClient") as client_cls:
        client_cls.return_value.connect.side_effect = paramiko.BadHostKeyException("sftp.example.test", other, key)
        with pytest.raises(SftpUploadError, match="changed"):
            session.connect()
        client_cls.return_value.close.assert_called_once()


def test_host_entry_non_default_port():
    assert sftp_upload._host_entry("h", 22) == "h"
    assert sftp_upload._host_entry("h", 2222) == "[h]:2222"


# ---------------------------------------------------------------- pending_files

def _connect_mock(rows_per_query):
    cursor = MagicMock()
    cursor.fetchall.side_effect = rows_per_query
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    return conn, cursor


@patch("odc_core.db.pyodbc.connect")
def test_pending_files_filters_uploaded_missing_and_void(mock_connect, tmp_path):
    todo = _pdf(tmp_path, "todo.pdf")
    done = _pdf(tmp_path, "done.pdf")
    sftp_upload.sidecar_path(done).write_text("{}", encoding="utf-8")
    void = _pdf(tmp_path, "void.pdf", size=100)
    missing = tmp_path / "missing.pdf"
    conn, cursor = _connect_mock([
        [(str(todo),), (str(done),), (None,)],
        [(str(void),), (str(missing),), (str(todo),)],
    ])
    mock_connect.return_value = conn

    result = sftp_upload.pending_files("Electric Ireland", "Horizon", date(2026, 8, 1), TABLES, "Jupiter")

    assert result == [str(todo)]
    first_sql, *first_params = cursor.execute.call_args_list[0].args
    assert "ODC_scrape_data" in first_sql and "ODC_job_details" in first_sql
    assert "[status]" not in first_sql  # ODC_scrape_data has no status column
    assert first_params == ["Electric Ireland", "Horizon", "2026-08-01"]
    second_sql = cursor.execute.call_args_list[1].args[0]
    assert "ODC_grab_all_data" in second_sql


@patch("odc_core.db.pyodbc.connect")
def test_pending_files_without_grab_all_table(mock_connect, tmp_path):
    conn, cursor = _connect_mock([[]])
    mock_connect.return_value = conn
    tables = {k: v for k, v in TABLES.items() if k != "grab_all_data"}

    assert sftp_upload.pending_files("S", "C", date(2026, 1, 1), tables, "Jupiter") == []
    assert cursor.execute.call_count == 1
