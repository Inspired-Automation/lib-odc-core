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


# --- client_notice_recipient ------------------------------------------------

def test_recipient_prod_non_inspired_credential_failure(tmp_path):
    recipient, _ = login_failure.client_notice_recipient(
        _config(tmp_path), client_name="Acme Ltd", username=" billing@acme.co.uk ", kind=K.CREDENTIALS_REJECTED,
    )
    assert recipient == "billing@acme.co.uk"


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


def test_no_recipient_when_username_not_email(tmp_path):
    recipient, reason = login_failure.client_notice_recipient(
        _config(tmp_path), client_name="Acme", username="ACME001", kind=K.CREDENTIALS_REJECTED,
    )
    assert recipient is None
    assert "not an email" in reason


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

def test_email_body_has_no_password_and_reply_to():
    subject, body = login_failure.build_client_email(
        supplier_name="EDF Energy", username="billing@acme.co.uk",
        portal_url="https://edf/login", kind=K.CREDENTIALS_REJECTED,
        when=datetime(2026, 10, 19),
    )
    assert subject == "Action required: we were unable to sign in to your EDF Energy online account"
    assert "billing@acme.co.uk" in body
    assert "19/10/2026" in body
    assert "support@systems-link.com" in body
    assert "Kind regards" not in body
    assert "s3cret" not in body


# --- notify_client / handle_login_failure -------------------------------------

@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_emails_client_without_team_alert(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="EDF Energy", job_id=42,
        row=ROW, detail="Invalid username or password", kind=K.CREDENTIALS_REJECTED,
    )
    assert sent is True
    errors.add.assert_not_called()
    args, kwargs = mock_send.call_args
    assert args[1] == "billing@acme.co.uk"
    assert kwargs["reply_to"] == "support@systems-link.com"
    assert kwargs["sender_address"] == "noreply_automation@inspiredenergy.co.uk"
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
def test_handle_non_email_login_team_alert_only(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Wave", job_id=1,
        row={**ROW, "username": "acme123"}, detail="Invalid password", kind=K.CREDENTIALS_REJECTED,
    )
    assert sent is False
    errors.add.assert_called_once()
    mock_send.assert_not_called()


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
    assert "email to the client failed" in errors.add.call_args_list[1].args[0]
    assert not list(tmp_path.glob("*.json"))


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_overrides_for_multi_credential(mock_send, tmp_path):
    errors = MagicMock()
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="TotalEnergies", job_id=7,
        row={**ROW, "username": "multi_credential"}, username="cred2@acme.co.uk",
        detail="bad", kind=K.CREDENTIALS_REJECTED,
    )
    assert mock_send.call_args.args[1] == "cred2@acme.co.uk"


@patch("odc_core.login_failure.graph_client.send_mail")
def test_team_alert_false_still_emails_client(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="SFB", job_id=3, row=ROW,
        detail="Invalid password", kind=K.CREDENTIALS_REJECTED, team_alert=False,
    )
    assert sent is True
    errors.add.assert_not_called()
