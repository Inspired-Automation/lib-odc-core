import json
from datetime import date
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
        "port": 22,
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


def _session_with_fake_sftp(tmp_path, remote_files=None, **cfg):
    """An SftpSession whose connection is a fake in-memory SFTP server."""
    remote = dict(remote_files or {})
    sftp = MagicMock()

    def stat(path):
        if path not in remote:
            raise FileNotFoundError(path)
        return MagicMock(st_size=remote[path])

    def put(local, path, confirm=False):
        remote[path] = len(open(local, "rb").read())

    def posix_rename(src, dst):
        remote[dst] = remote.pop(src)

    sftp.stat.side_effect = stat
    sftp.put.side_effect = put
    sftp.posix_rename.side_effect = posix_rename
    def remove(path):
        if path not in remote:
            raise FileNotFoundError(path)  # what paramiko raises for ENOENT
        del remote[path]

    sftp.remove.side_effect = remove

    session = SftpSession(_config(tmp_path, **cfg))
    session._sftp = sftp
    session._alive = lambda: True
    return session, sftp, remote


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


# ------------------------------------------------------------------------ upload

def test_upload_writes_part_then_renames_and_writes_sidecar(tmp_path):
    local = _pdf(tmp_path)
    session, sftp, remote = _session_with_fake_sftp(tmp_path)

    assert session.upload(local) is True

    sftp.put.assert_called_once_with(str(local), local.name + ".part", confirm=False)
    sftp.posix_rename.assert_called_once_with(local.name + ".part", local.name)
    assert remote == {local.name: 2048}
    record = json.loads(sftp_upload.sidecar_path(local).read_text(encoding="utf-8"))
    assert record["remote_path"] == local.name
    assert record["size"] == 2048
    assert "secret" not in sftp_upload.sidecar_path(local).read_text(encoding="utf-8")


def test_upload_uses_remote_dir(tmp_path):
    local = _pdf(tmp_path)
    session, _, remote = _session_with_fake_sftp(tmp_path, remote_dir="inbound/invoices")

    assert session.upload(local) is True
    assert remote == {f"inbound/invoices/{local.name}": 2048}


def test_upload_existing_same_size_is_done_without_put(tmp_path):
    local = _pdf(tmp_path)
    session, sftp, _ = _session_with_fake_sftp(tmp_path, remote_files={local.name: 2048})

    assert session.upload(local) is True
    sftp.put.assert_not_called()
    assert sftp_upload.is_uploaded(local)


def test_upload_existing_different_size_is_replaced(tmp_path):
    local = _pdf(tmp_path)
    session, sftp, remote = _session_with_fake_sftp(tmp_path, remote_files={local.name: 10})

    assert session.upload(local) is True
    sftp.put.assert_called_once()
    assert remote[local.name] == 2048


def test_upload_size_mismatch_removes_part_and_fails(tmp_path):
    local = _pdf(tmp_path)
    session, sftp, remote = _session_with_fake_sftp(tmp_path)
    sftp.put.side_effect = lambda local_path, path, confirm=False: remote.__setitem__(path, 1)

    assert session.upload(local) is False
    assert remote == {}
    assert not sftp_upload.is_uploaded(local)


def test_upload_falls_back_to_rename_without_posix_rename(tmp_path):
    local = _pdf(tmp_path)
    session, sftp, remote = _session_with_fake_sftp(tmp_path)
    sftp.posix_rename.side_effect = OSError("unsupported")
    sftp.rename.side_effect = lambda src, dst: remote.__setitem__(dst, remote.pop(src))

    assert session.upload(local) is True
    assert remote == {local.name: 2048}


def test_upload_skips_file_with_sidecar(tmp_path):
    local = _pdf(tmp_path)
    sftp_upload.sidecar_path(local).write_text("{}", encoding="utf-8")
    session, sftp, _ = _session_with_fake_sftp(tmp_path)

    assert session.upload(local) is True
    sftp.put.assert_not_called()


def test_upload_server_error_on_live_connection_fails_without_reconnect(tmp_path):
    local = _pdf(tmp_path)
    session, sftp, _ = _session_with_fake_sftp(tmp_path)
    sftp.put.side_effect = PermissionError("denied")
    session.connect = MagicMock()

    assert session.upload(local) is False
    session.connect.assert_not_called()
    assert session.broken is False


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
    session = SftpSession(_config(tmp_path, host_key=f"{key.get_name()} {key.get_base64()}"))
    with patch.object(paramiko, "SSHClient") as client_cls:
        client = client_cls.return_value
        session.connect()
    policy = client.set_missing_host_key_policy.call_args.args[0]
    assert isinstance(policy, paramiko.RejectPolicy)
    client.get_host_keys.return_value.add.assert_called_once_with("sftp.example.test", key.get_name(), key)
    assert client.connect.call_args.kwargs["look_for_keys"] is False


def test_connect_bad_host_key_raises_clear_error(tmp_path, rsa_keys):
    key, other = rsa_keys
    session = SftpSession(_config(tmp_path))
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
