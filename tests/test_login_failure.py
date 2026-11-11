import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from odc_core import login_failure
from odc_core.login_failure import LoginFailureKind as K


def _config(tmp_path, env="prod", **settings):
    return {
        "env": env,
        "graph": {"sender_address": "bot@example.com"},
        "login_failure_notification": {"state_dir": str(tmp_path), **settings},
    }


ROW = {
    "client_name": "Acme Ltd",
    "username": "billing@acme.co.uk",
    "password": "s3cret!",
    "url": "https://portal.example.com/login",
}


# --- classify_portal_message ------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    ("Invalid username or password", K.CREDENTIALS_REJECTED),
    ("User name or password invalid", K.CREDENTIALS_REJECTED),
    ("The credentials you entered were invalid, please try again", K.CREDENTIALS_REJECTED),
    ("Your email address or password is incorrect.", K.CREDENTIALS_REJECTED),
    ("Incorrect password", K.CREDENTIALS_REJECTED),
    ("Login failed", K.CREDENTIALS_REJECTED),
    ("We couldn't sign you in", K.CREDENTIALS_REJECTED),
    ("We don't recognise the details you've entered", K.CREDENTIALS_REJECTED),
    ("Too many login attempts. You have made too many login attempts.", K.ACCOUNT_LOCKED),
    ("Your account has been locked", K.ACCOUNT_LOCKED),
    ("You are locked out. Try again in 30 minutes", K.ACCOUNT_LOCKED),
    ("Your password has expired", K.PASSWORD_CHANGE_REQUIRED),
    ("You must change your password to continue", K.PASSWORD_CHANGE_REQUIRED),
    ("", K.OTHER),
    (None, K.OTHER),
    ("Service temporarily unavailable", K.OTHER),
    ("Forgotten your password?", K.OTHER),
    ("Something went wrong, please try again later", K.OTHER),
])
def test_classify_portal_message(text, kind):
    assert login_failure.classify_portal_message(text) == kind


def test_escalate_keeps_most_specific():
    assert login_failure.escalate(K.OTHER, K.CREDENTIALS_REJECTED) == K.CREDENTIALS_REJECTED
    assert login_failure.escalate(K.CREDENTIALS_REJECTED, K.OTHER) == K.CREDENTIALS_REJECTED
    assert login_failure.escalate(K.CREDENTIALS_REJECTED, K.ACCOUNT_LOCKED) == K.ACCOUNT_LOCKED
    assert login_failure.escalate(None, None) == K.OTHER
    assert login_failure.escalate("not a kind", "account locked") == K.ACCOUNT_LOCKED


def test_escalate_otp_not_received_ranks_below_password_kinds():
    assert login_failure.escalate(K.OTHER, K.OTP_NOT_RECEIVED) == K.OTP_NOT_RECEIVED
    assert login_failure.escalate(K.OTP_NOT_RECEIVED, K.OTHER) == K.OTP_NOT_RECEIVED
    assert login_failure.escalate(K.CREDENTIALS_REJECTED, K.OTP_NOT_RECEIVED) == K.CREDENTIALS_REJECTED
    assert login_failure.escalate(K.OTP_NOT_RECEIVED, K.PASSWORD_CHANGE_REQUIRED) == K.PASSWORD_CHANGE_REQUIRED


def test_otp_not_received_is_never_classified_from_page_text():
    assert login_failure.classify_portal_message("We have sent a one-time passcode to your email") == K.OTHER


def test_build_client_email_otp_not_received():
    subject, body = login_failure.build_client_email(
        supplier_name="Pozitive Energy", username="billing@acme.co.uk",
        portal_url="https://portal.example.com", kind=K.OTP_NOT_RECEIVED, client_name="Acme Ltd", job_id=5571,
    )
    assert "(otp not received)" in subject
    assert "one-time passcode" in body
    assert "noreply_automation@inspiredenergy.co.uk" in body


# --- client_notice_recipient ------------------------------------------------

@pytest.mark.parametrize("kind", [K.CREDENTIALS_REJECTED, K.ACCOUNT_LOCKED, K.PASSWORD_CHANGE_REQUIRED, K.OTP_NOT_RECEIVED])
def test_recipient_prod_non_inspired_is_systemslink(tmp_path, kind):
    recipient, _ = login_failure.client_notice_recipient(
        _config(tmp_path), client_name="Acme Ltd", username=" billing@acme.co.uk ", kind=kind,
    )
    assert recipient == "support@systems-link.com"


def test_recipient_override_from_config(tmp_path):
    recipient, _ = login_failure.client_notice_recipient(
        _config(tmp_path, recipient="other@systems-link.com"),
        client_name="Acme Ltd", username="billing@acme.co.uk", kind=K.PASSWORD_CHANGE_REQUIRED,
    )
    assert recipient == "other@systems-link.com"


@pytest.mark.parametrize("client", ["Inspired PLC", "inspired plc", "IGNITE", " Ignite "])
def test_no_recipient_for_inspired_process_clients(tmp_path, client):
    recipient, reason = login_failure.client_notice_recipient(
        _config(tmp_path), client_name=client, username="a@b.com", kind=K.CREDENTIALS_REJECTED,
    )
    assert recipient is None
    assert "Inspired-process" in reason


def test_no_recipient_for_other_kind(tmp_path):
    recipient, _ = login_failure.client_notice_recipient(
        _config(tmp_path), client_name="Acme", username="a@b.com", kind=K.OTHER,
    )
    assert recipient is None


def test_recipient_when_username_not_email(tmp_path):
    recipient, _ = login_failure.client_notice_recipient(
        _config(tmp_path), client_name="Acme", username="ACME001", kind=K.CREDENTIALS_REJECTED,
    )
    assert recipient == "support@systems-link.com"


def test_no_recipient_without_client_name(tmp_path):
    recipient, reason = login_failure.client_notice_recipient(
        _config(tmp_path), client_name=" ", username="a@b.com", kind=K.CREDENTIALS_REJECTED,
    )
    assert recipient is None
    assert "client_name" in reason


def test_no_recipient_when_disabled(tmp_path):
    recipient, _ = login_failure.client_notice_recipient(
        _config(tmp_path, enabled="false"), client_name="Acme", username="a@b.com", kind=K.ACCOUNT_LOCKED,
    )
    assert recipient is None


def test_dev_run_skips_without_dev_recipient(tmp_path):
    recipient, _ = login_failure.client_notice_recipient(
        _config(tmp_path, env="dev"), client_name="Acme", username="a@b.com", kind=K.ACCOUNT_LOCKED,
    )
    assert recipient is None


def test_dev_run_redirects_to_dev_recipient(tmp_path):
    recipient, _ = login_failure.client_notice_recipient(
        _config(tmp_path, env="dev", dev_recipient="me@inspiredenergy.co.uk"),
        client_name="Acme", username="a@b.com", kind=K.ACCOUNT_LOCKED,
    )
    assert recipient == "me@inspiredenergy.co.uk"


# --- notices_dir --------------------------------------------------------------

def test_notices_dir_defaults_under_pdf_auto_base():
    cfg = {"env": "prod", "pdf_auto": {"base_path": "/share/ODC"}}
    assert login_failure.notices_dir(cfg).as_posix() == "/share/ODC/LIVE/LOGIN_FAILURE_NOTICES"
    cfg["env"] = "dev"
    assert login_failure.notices_dir(cfg).as_posix() == "/share/ODC/DEV/LOGIN_FAILURE_NOTICES"


# --- build_client_email -------------------------------------------------------

def test_email_names_client_login_and_job_without_password():
    subject, body = login_failure.build_client_email(
        supplier_name="EDF Energy", username="billing@acme.co.uk",
        portal_url="https://edf/login", kind=K.CREDENTIALS_REJECTED,
        when=datetime(2026, 10, 19), client_name="Acme Ltd", job_id=5448,
    )
    assert subject == "Action required: Acme Ltd - EDF Energy portal login needs updating (credentials rejected)"
    assert "Client: Acme Ltd" in body
    assert "Account login: billing@acme.co.uk" in body
    assert "ODC job: 5448" in body
    assert "https://edf/login" in body
    assert "19/10/2026" in body
    assert "Kind regards" not in body
    assert "s3cret" not in body


def test_email_password_change_informs_only():
    subject, body = login_failure.build_client_email(
        supplier_name="TGP Mygateway", username="bills@acme.com", portal_url=None,
        kind=K.PASSWORD_CHANGE_REQUIRED, client_name="Acme Ltd", job_id=1,
    )
    assert "(password change required)" in subject
    assert "asking for the password on this login to be changed" in body
    assert "Please let the client know that the password on this login needs to be changed" in body
    assert "supply" not in body
    assert "screenshot" not in body


def test_email_mentions_attached_screenshot():
    _, body = login_failure.build_client_email(
        supplier_name="TGP Mygateway", username="bills@acme.com", portal_url=None,
        kind=K.PASSWORD_CHANGE_REQUIRED, client_name="Acme Ltd", job_id=1, screenshot_attached=True,
    )
    assert "A screenshot of the page the portal showed is attached." in body


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_attaches_existing_screenshot(mock_send, tmp_path):
    state = tmp_path / "state"
    shot = tmp_path / "21090_TGP MYGATEWAY_password_change_required.png"
    shot.write_bytes(b"png")
    login_failure.handle_login_failure(
        MagicMock(), _config(state), supplier_name="TGP Mygateway", job_id=5448, row=ROW,
        detail="Change Password page", kind=K.PASSWORD_CHANGE_REQUIRED, screenshot=str(shot),
    )
    args, kwargs = mock_send.call_args
    assert kwargs["attachments"] == [shot]
    assert "screenshot of the page" in args[3]


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_missing_screenshot_sends_without(mock_send, tmp_path):
    login_failure.handle_login_failure(
        MagicMock(), _config(tmp_path), supplier_name="TGP Mygateway", job_id=5448, row=ROW,
        detail="x", kind=K.PASSWORD_CHANGE_REQUIRED, screenshot=tmp_path / "gone.png",
    )
    args, kwargs = mock_send.call_args
    assert kwargs["attachments"] == []
    assert "screenshot" not in args[3]


# --- notify_client / handle_login_failure -------------------------------------

@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_emails_systemslink_without_team_alert(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="EDF Energy", job_id=42,
        row=ROW, detail="Invalid username or password", kind=K.CREDENTIALS_REJECTED,
    )
    assert sent is True
    errors.add.assert_not_called()
    args, kwargs = mock_send.call_args
    assert args[1] == "support@systems-link.com"
    assert kwargs["reply_to"] is None
    assert kwargs["sender_address"] == "noreply_automation@inspiredenergy.co.uk"
    assert "Acme Ltd" in args[2]
    assert "billing@acme.co.uk" in args[3]
    assert "ODC job: 42" in args[3]
    assert "s3cret" not in args[3]
    markers = list(tmp_path.glob("*.json"))
    assert len(markers) == 1
    assert json.loads(markers[0].read_text())["job_id"] == "42"


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_inspired_client_team_alert_only(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Wave", job_id=1,
        row={**ROW, "client_name": "Inspired PLC"}, detail="Invalid password", kind=K.CREDENTIALS_REJECTED,
    )
    assert sent is False
    errors.add.assert_called_once()
    assert "Wave login failed for job 1 (credentials rejected)" in errors.add.call_args.args[0]
    mock_send.assert_not_called()


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_non_email_login_still_notifies_systemslink(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Wave", job_id=1,
        row={**ROW, "username": "acme123"}, detail="Invalid password", kind=K.CREDENTIALS_REJECTED,
    )
    assert sent is True
    errors.add.assert_not_called()
    assert mock_send.call_args.args[1] == "support@systems-link.com"
    assert "Account login: acme123" in mock_send.call_args.args[3]


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_password_change_notifies_systemslink(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="TGP Mygateway", job_id=5448,
        row={**ROW, "client_name": "ESOS Energy Ltd"}, detail="Change Password page",
        kind=K.PASSWORD_CHANGE_REQUIRED,
    )
    assert sent is True
    errors.add.assert_not_called()
    args, _ = mock_send.call_args
    assert args[1] == "support@systems-link.com"
    assert "ESOS Energy Ltd - TGP Mygateway" in args[2]


@patch("odc_core.login_failure.graph_client.send_mail")
def test_reply_to_set_when_configured(mock_send, tmp_path):
    login_failure.handle_login_failure(
        MagicMock(), _config(tmp_path, reply_to="team@inspiredenergy.co.uk"),
        supplier_name="SSE", job_id=1, row=ROW, detail="x", kind=K.ACCOUNT_LOCKED,
    )
    assert mock_send.call_args.kwargs["reply_to"] == "team@inspiredenergy.co.uk"


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_within_cooldown_no_team_alert(mock_send, tmp_path):
    cfg = _config(tmp_path)
    for _ in range(2):
        errors = MagicMock()
        login_failure.handle_login_failure(
            errors, cfg, supplier_name="EDF Energy", job_id=1,
            row=ROW, detail="Invalid password", kind=K.CREDENTIALS_REJECTED,
        )
        errors.add.assert_not_called()
    assert mock_send.call_count == 1


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_other_kind_team_alert_only(mock_send, tmp_path):
    errors = MagicMock()
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Wave", job_id=1,
        row=ROW, detail="Portal failed to load: timeout",
    )
    errors.add.assert_called_once()
    assert "(other)" in errors.add.call_args.args[0]
    mock_send.assert_not_called()


@patch("odc_core.login_failure.graph_client.send_mail")
def test_cooldown_stops_repeat_email(mock_send, tmp_path):
    cfg = _config(tmp_path, cooldown_hours=24)
    kwargs = dict(supplier_name="EDF Energy", job_id=1, client_name="Acme",
                  username="billing@acme.co.uk", portal_url=None, kind=K.ACCOUNT_LOCKED)
    now = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)
    assert login_failure.notify_client(cfg, now=now, **kwargs) is True
    assert login_failure.notify_client(cfg, now=now + timedelta(hours=23), **kwargs) is False
    assert login_failure.notify_client(cfg, now=now + timedelta(hours=25), **kwargs) is True
    assert mock_send.call_count == 2


@patch("odc_core.login_failure.graph_client.send_mail")
def test_cooldown_is_per_supplier_and_login(mock_send, tmp_path):
    cfg = _config(tmp_path)
    base = dict(job_id=1, client_name="Acme", portal_url=None, kind=K.ACCOUNT_LOCKED)
    login_failure.notify_client(cfg, supplier_name="EDF Energy", username="a@acme.com", **base)
    login_failure.notify_client(cfg, supplier_name="SSE", username="a@acme.com", **base)
    login_failure.notify_client(cfg, supplier_name="EDF Energy", username="b@acme.com", **base)
    assert mock_send.call_count == 3


@patch("odc_core.login_failure.graph_client.send_mail", side_effect=RuntimeError("graph down"))
def test_handle_never_raises_on_send_failure(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="EDF Energy", job_id=1,
        row=ROW, detail="x", kind=K.CREDENTIALS_REJECTED,
    )
    assert sent is False
    assert errors.add.call_count == 2
    assert "login failed for job 1" in errors.add.call_args_list[0].args[0]
    assert "notice to SystemsLink failed" in errors.add.call_args_list[1].args[0]
    assert not list(tmp_path.glob("*.json"))


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_overrides_for_multi_credential(mock_send, tmp_path):
    errors = MagicMock()
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="TotalEnergies", job_id=7,
        row={**ROW, "username": "multi_credential"}, username="cred2@acme.co.uk",
        detail="bad", kind=K.CREDENTIALS_REJECTED,
    )
    assert mock_send.call_args.args[1] == "support@systems-link.com"
    assert "Account login: cred2@acme.co.uk" in mock_send.call_args.args[3]


@patch("odc_core.login_failure.graph_client.send_mail")
def test_team_alert_false_still_emails_systemslink(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="SFB", job_id=3, row=ROW,
        detail="Invalid password", kind=K.CREDENTIALS_REJECTED, team_alert=False,
    )
    assert sent is True
    errors.add.assert_not_called()


# --- toolkit reporting (0.14.0) -----------------------------------------------

FAIL_PROC = "Titan_INSE_DEV.dbo.spODC_ReportLoginFailure"
OK_PROC = "Titan_INSE_DEV.dbo.spODC_ReportLoginSuccess"
MULTI_ROW = {**ROW, "username": "multi_credential", "password": "multi_credential"}


def _toolkit_config(tmp_path, env="prod", **settings):
    block = {"jobs": "ODC_jobs", "report_login_failure": FAIL_PROC, "report_login_success": OK_PROC}
    return {
        **_config(tmp_path, env=env, **settings),
        "database": {"dsn": "Jupiter"},
        "tables": {"dev": dict(block), "live": dict(block)},
    }


def _connection(mock_connect, row):
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value = cursor
    mock_connect.return_value = conn
    return conn, cursor


@patch("odc_core.db.pyodbc.connect")
def test_report_failure_not_configured_makes_no_db_call(mock_connect, tmp_path):
    for cfg in (
        _config(tmp_path),
        {**_config(tmp_path), "database": {"dsn": "Jupiter"}, "tables": {"live": {"jobs": "ODC_jobs"}}},
        {**_config(tmp_path), "database": {"dsn": "Jupiter"}, "tables": {"live": {"report_login_failure": "  "}}},
        {**_config(tmp_path), "tables": None},
    ):
        assert login_failure.report_login_failure(cfg, job_id=1, kind=K.ACCOUNT_LOCKED) is None
    mock_connect.assert_not_called()


@patch("odc_core.db.pyodbc.connect")
def test_report_failure_binds_truncated_values_and_reads_outputs(mock_connect, tmp_path):
    conn, cursor = _connection(mock_connect, (17, True, False))
    report = login_failure.report_login_failure(
        _toolkit_config(tmp_path), job_id="5448", kind=K.PASSWORD_CHANGE_REQUIRED,
        detail="d" * 1500, username="u" * 150, portal_url="h" * 400,
        screenshot="C:\\shots\\" + "s" * 600, process_id="21090",
    )
    assert report == login_failure.LoginFailureReport(failure_id=17, paused=True, has_owner=False)
    mock_connect.assert_called_once_with("DSN=Jupiter")
    sql, *params = cursor.execute.call_args.args
    assert f"EXEC {FAIL_PROC} @job_id = ?" in sql
    assert "@failure_id = @f OUTPUT" in sql and sql.rstrip().endswith("SELECT @f, @p, @o;")
    assert params[0] == 5448
    assert params[1] == "password change required"
    assert [len(p) for p in params[2:6]] == [1000, 100, 300, 500]
    assert params[6] == 21090
    assert params[7] == 0
    assert all(p is None or type(p) in (str, int) for p in params)
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


@patch("odc_core.db.pyodbc.connect")
def test_report_failure_uses_env_block_and_explicit_overrides(mock_connect, tmp_path):
    _, cursor = _connection(mock_connect, (1, False, False))
    cfg = _toolkit_config(tmp_path, env="dev")
    cfg["tables"]["live"] = {"jobs": "ODC_jobs"}
    assert login_failure.report_login_failure(cfg, job_id=1, kind=K.OTHER) is not None
    cfg["env"] = "prod"
    assert login_failure.report_login_failure(cfg, job_id=1, kind=K.OTHER) is None
    assert mock_connect.call_count == 1

    login_failure.report_login_failure(
        {}, job_id=2, kind=K.OTHER, tables={"report_login_failure": "db.dbo.other_proc"}, dsn="Other",
    )
    mock_connect.assert_called_with("DSN=Other")
    assert "EXEC db.dbo.other_proc " in cursor.execute.call_args.args[0]
    assert cursor.execute.call_args.args[3:8] == (None, None, None, None, None)


@patch("odc_core.db.pyodbc.connect", side_effect=RuntimeError("db down"))
def test_report_failure_raises_on_db_error(mock_connect, tmp_path):
    with pytest.raises(RuntimeError):
        login_failure.report_login_failure(_toolkit_config(tmp_path), job_id=1, kind=K.ACCOUNT_LOCKED)


@patch("odc_core.login_failure.graph_client.send_mail")
@patch("odc_core.db.pyodbc.connect")
def test_handle_not_configured_keeps_old_behaviour(mock_connect, mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="EDF Energy", job_id=42,
        row=ROW, detail="Invalid username or password", kind=K.CREDENTIALS_REJECTED, process_id=9,
    )
    assert sent is True
    mock_connect.assert_not_called()
    mock_send.assert_called_once()
    errors.add.assert_not_called()


@pytest.mark.parametrize("kind", [K.CREDENTIALS_REJECTED, K.ACCOUNT_LOCKED, K.PASSWORD_CHANGE_REQUIRED, K.OTP_NOT_RECEIVED])
@patch("odc_core.login_failure.graph_client.send_mail")
@patch("odc_core.db.pyodbc.connect")
def test_handle_toolkit_owns_paused_service_with_owner(mock_connect, mock_send, tmp_path, caplog, kind):
    _, cursor = _connection(mock_connect, (31, 1, 1))
    errors = MagicMock()
    with caplog.at_level("INFO", logger="odc_core.login_failure"):
        sent = login_failure.handle_login_failure(
            errors, _toolkit_config(tmp_path), supplier_name="EDF Energy", job_id=42,
            row=ROW, detail="Invalid username or password", kind=kind, process_id=21090,
        )
    assert sent is False
    mock_send.assert_not_called()
    errors.add.assert_not_called()
    assert not list(tmp_path.glob("*.json"))
    assert "failure id 31" in caplog.text and "notified by the toolkit" in caplog.text
    params = cursor.execute.call_args.args[1:]
    assert params[:5] == (42, str(kind), "Invalid username or password", "billing@acme.co.uk",
                          "https://portal.example.com/login")
    assert params[6:] == (21090, 0)
    assert "s3cret!" not in [str(p) for p in params]
    assert "s3cret" not in caplog.text


@pytest.mark.parametrize("outputs", [(31, 1, 0), (31, 0, 1), (None, 0, 0)])
@patch("odc_core.login_failure.graph_client.send_mail")
@patch("odc_core.db.pyodbc.connect")
def test_handle_no_owner_or_not_paused_takes_old_path(mock_connect, mock_send, tmp_path, outputs):
    _connection(mock_connect, outputs)
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _toolkit_config(tmp_path), supplier_name="EDF Energy", job_id=42,
        row=ROW, detail="Invalid password", kind=K.CREDENTIALS_REJECTED,
    )
    assert sent is True
    assert mock_send.call_args.args[1] == "support@systems-link.com"
    errors.add.assert_not_called()


@patch("odc_core.login_failure.graph_client.send_mail")
@patch("odc_core.db.pyodbc.connect")
def test_handle_first_otp_miss_recorded_by_toolkit_is_quiet(mock_connect, mock_send, tmp_path, caplog):
    _connection(mock_connect, (31, 0, 1))
    errors = MagicMock()
    with caplog.at_level("WARNING", logger="odc_core.login_failure"):
        sent = login_failure.handle_login_failure(
            errors, _toolkit_config(tmp_path), supplier_name="Pozitive Energy", job_id=5571,
            row=ROW, detail="OTP not received", kind=K.OTP_NOT_RECEIVED,
        )
    assert sent is False
    mock_send.assert_not_called()
    errors.add.assert_not_called()
    assert "second miss" in caplog.text


@pytest.mark.parametrize("outputs", [(31, 0, 0), (None, 0, 0)])
@patch("odc_core.login_failure.graph_client.send_mail")
@patch("odc_core.db.pyodbc.connect")
def test_handle_otp_miss_without_owner_takes_old_path(mock_connect, mock_send, tmp_path, outputs):
    _connection(mock_connect, outputs)
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _toolkit_config(tmp_path), supplier_name="Pozitive Energy", job_id=5571,
        row=ROW, detail="OTP not received", kind=K.OTP_NOT_RECEIVED,
    )
    assert sent is True
    assert mock_send.call_args.args[1] == "support@systems-link.com"
    errors.add.assert_not_called()


@patch("odc_core.login_failure.graph_client.send_mail")
@patch("odc_core.db.pyodbc.connect")
def test_handle_other_kind_takes_old_path_even_if_paused(mock_connect, mock_send, tmp_path):
    _, cursor = _connection(mock_connect, (31, 1, 1))
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _toolkit_config(tmp_path), supplier_name="Wave", job_id=1,
        row=ROW, detail="Portal failed to load: timeout",
    )
    assert sent is False
    assert cursor.execute.call_args.args[2] == "other"
    errors.add.assert_called_once()
    assert "Wave login failed for job 1 (other)" in errors.add.call_args.args[0]
    mock_send.assert_not_called()


@patch("odc_core.login_failure.graph_client.send_mail")
@patch("odc_core.db.pyodbc.connect")
def test_handle_multi_credential_takes_old_path_and_passes_flag(mock_connect, mock_send, tmp_path):
    _, cursor = _connection(mock_connect, (31, 1, 1))
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _toolkit_config(tmp_path), supplier_name="SFB", job_id=3, row=MULTI_ROW,
        username="cred2@acme.co.uk", detail="Invalid password", kind=K.CREDENTIALS_REJECTED,
        team_alert=False,
    )
    assert sent is True
    params = cursor.execute.call_args.args[1:]
    assert params[3] == "cred2@acme.co.uk"
    assert params[7] == 1
    mock_send.assert_called_once()
    errors.add.assert_not_called()


@patch("odc_core.login_failure.graph_client.send_mail")
@patch("odc_core.db.pyodbc.connect")
def test_handle_multi_credential_flag_resolution(mock_connect, mock_send, tmp_path):
    _, cursor = _connection(mock_connect, (31, 1, 1))
    cfg = _toolkit_config(tmp_path)
    base = dict(supplier_name="SFB", job_id=3, detail="x", kind=K.CREDENTIALS_REJECTED)

    # A sentinel job-level login with the default team_alert is still multi-credential.
    login_failure.handle_login_failure(MagicMock(), cfg, row=MULTI_ROW, **base)
    assert cursor.execute.call_args.args[8] == 1

    # An explicit value wins over team_alert in both directions.
    assert login_failure.handle_login_failure(
        MagicMock(), cfg, row=ROW, team_alert=False, multi_credential=False, **base) is False
    assert cursor.execute.call_args.args[8] == 0
    login_failure.handle_login_failure(MagicMock(), cfg, row=ROW, multi_credential=True, **base)
    assert cursor.execute.call_args.args[8] == 1


@patch("odc_core.login_failure.graph_client.send_mail")
@patch("odc_core.db.pyodbc.connect", side_effect=RuntimeError("db down"))
def test_handle_report_raising_takes_old_path_plus_error(mock_connect, mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _toolkit_config(tmp_path), supplier_name="EDF Energy", job_id=42,
        row=ROW, detail="Invalid password", kind=K.CREDENTIALS_REJECTED,
    )
    assert sent is True
    mock_send.assert_called_once()
    errors.add.assert_called_once()
    assert errors.add.call_args.args[0] == (
        "EDF Energy login failure for job 42 could not be recorded for the toolkit"
    )
    assert isinstance(errors.add.call_args.kwargs["exception"], RuntimeError)


@patch("odc_core.login_failure.graph_client.send_mail")
@patch("odc_core.db.pyodbc.connect")
def test_handle_report_raising_other_kind_adds_both_errors(mock_connect, mock_send, tmp_path):
    errors = MagicMock()
    login_failure.handle_login_failure(
        errors, _toolkit_config(tmp_path), supplier_name="Wave", job_id="not-a-number", row=ROW, detail="timeout",
    )
    mock_connect.assert_not_called()
    assert errors.add.call_count == 2
    assert "login failed for job not-a-number (other)" in errors.add.call_args_list[0].args[0]
    assert "could not be recorded for the toolkit" in errors.add.call_args_list[1].args[0]


@patch("odc_core.db.pyodbc.connect")
def test_report_success_not_configured(mock_connect, tmp_path):
    assert login_failure.report_login_success(_config(tmp_path), job_id=1) is False
    cfg = _toolkit_config(tmp_path)
    del cfg["tables"]["live"]["report_login_success"]
    assert login_failure.report_login_success(cfg, job_id=1, process_id=2) is False
    mock_connect.assert_not_called()


@pytest.mark.parametrize("output,expected", [(1, True), (True, True), (0, False), (None, False)])
@patch("odc_core.db.pyodbc.connect")
def test_report_success_configured(mock_connect, tmp_path, output, expected):
    conn, cursor = _connection(mock_connect, (output,))
    assert login_failure.report_login_success(
        _toolkit_config(tmp_path, env="dev"), job_id="42", process_id="21090",
    ) is expected
    sql, *params = cursor.execute.call_args.args
    assert f"EXEC {OK_PROC} @job_id = ?, @process_id = ?, @resolved = @r OUTPUT" in sql
    assert params == [42, 21090]
    conn.commit.assert_called_once()


@patch("odc_core.db.pyodbc.connect", side_effect=RuntimeError("db down"))
def test_report_success_never_raises(mock_connect, tmp_path, caplog):
    assert login_failure.report_login_success(_toolkit_config(tmp_path), job_id=42) is False
    assert "could not report the successful login for job 42" in caplog.text
    assert login_failure.report_login_success(_toolkit_config(tmp_path), job_id="abc") is False
