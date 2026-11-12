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
    ("Service temporarily unavailable", K.PORTAL_UNAVAILABLE),
    ("The site is undergoing scheduled maintenance", K.PORTAL_UNAVAILABLE),
    ("The code you entered is incorrect", K.OTP_REJECTED),
    ("Your verification code has expired", K.OTP_REJECTED),
    ("Invalid passcode", K.OTP_REJECTED),
    ("Please enter the code we sent you", K.OTHER),
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
def test_handle_emails_systemslink_and_tells_the_control_room(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="EDF Energy", job_id=42,
        row=ROW, detail="Invalid username or password", kind=K.CREDENTIALS_REJECTED,
    )
    assert sent is True
    errors.add.assert_called_once()  # 0.14.1: the Control Room is always told
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
    errors.add.assert_called_once()  # 0.14.1: the Control Room is always told
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
    errors.add.assert_called_once()  # 0.14.1: the Control Room is always told
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
def test_handle_within_cooldown_emails_once_but_reports_each_time(mock_send, tmp_path):
    cfg = _config(tmp_path)
    for _ in range(2):
        errors = MagicMock()
        login_failure.handle_login_failure(
            errors, cfg, supplier_name="EDF Energy", job_id=1,
            row=ROW, detail="Invalid password", kind=K.CREDENTIALS_REJECTED,
        )
        errors.add.assert_called_once()  # 0.14.1: the Control Room is always told
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
def test_team_alert_false_still_emails_systemslink_and_reports_the_login(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="SFB", job_id=3, row=ROW,
        detail="Invalid password", kind=K.CREDENTIALS_REJECTED, team_alert=False,
    )
    assert sent is True
    errors.add.assert_called_once()  # 0.14.1: the Control Room is always told


# --- Control Room triage (0.14.1) ---------------------------------------------

def _details(errors, call=0):
    return errors.add.call_args_list[call].kwargs["details"]


@pytest.mark.parametrize("kind,category", [
    (K.CREDENTIALS_REJECTED, "login_error"),
    (K.ACCOUNT_LOCKED, "login_error"),
    (K.PASSWORD_CHANGE_REQUIRED, "login_error"),
    (K.OTP_NOT_RECEIVED, "otp_not_received"),
    (K.RECAPTCHA_NOT_COMPLETED, "recaptcha_not_completed"),
    (K.OTHER, "other"),
    (None, "other"),
    ("nonsense", "other"),
])
def test_category_for(kind, category):
    assert login_failure.category_for(kind) == category


def test_recaptcha_is_not_something_systemslink_or_an_owner_fixes():
    assert K.RECAPTCHA_NOT_COMPLETED not in login_failure.CLIENT_NOTIFIABLE_KINDS
    assert K.RECAPTCHA_NOT_COMPLETED not in login_failure.OWNER_FIXABLE_KINDS
    assert login_failure.escalate(K.RECAPTCHA_NOT_COMPLETED, K.CREDENTIALS_REJECTED) == K.CREDENTIALS_REJECTED


def test_failure_details_carries_what_triage_needs_and_never_the_password(tmp_path):
    shot = tmp_path / "login.png"
    details = login_failure.failure_details(
        supplier_name="EDF Energy", job_id="5448", kind=K.ACCOUNT_LOCKED, detail="Locked " * 400,
        username="bills@acme.example", portal_url="https://portal.example", client_name="Acme Ltd",
        screenshot=shot, process_id="21090", multi_credential=True,
    )
    assert details["source"] == "odc_login_failure"
    assert details["category"] == "login_error" and details["kind"] == "account locked"
    assert details["odc_job_id"] == 5448 and details["process_id"] == 21090
    assert details["username"] == "bills@acme.example" and details["multi_credential"] is True
    assert details["screenshot"].endswith("login.png")
    assert len(details["detail"]) == 1000
    assert "password" not in details


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_always_tells_the_control_room_even_when_systemslink_is_emailed(mock_send, tmp_path):
    errors = MagicMock()
    sent = login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="EDF Energy", job_id=42,
        row=ROW, detail="Invalid password", kind=K.CREDENTIALS_REJECTED, process_id=777,
    )
    assert sent is True
    assert mock_send.call_args.args[1] == "support@systems-link.com"
    errors.add.assert_called_once()
    assert "EDF Energy login failed for job 42 (credentials rejected)" in errors.add.call_args.args[0]
    details = _details(errors)
    assert details["category"] == "login_error" and details["odc_job_id"] == 42
    assert details["username"] == ROW["username"] and details["process_id"] == 777
    assert details["multi_credential"] is False


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_other_kind_is_reported_as_other(mock_send, tmp_path):
    errors = MagicMock()
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Wave", job_id=1, row=ROW,
        detail="Portal failed to load: timeout",
    )
    mock_send.assert_not_called()
    assert _details(errors)["category"] == "other"


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_recaptcha_timeout_is_reported_under_its_own_category(mock_send, tmp_path):
    errors = MagicMock()
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Energia", job_id=9, row=ROW,
        detail="Nobody completed the reCAPTCHA", kind=K.RECAPTCHA_NOT_COMPLETED,
    )
    mock_send.assert_not_called()
    assert _details(errors)["category"] == "recaptcha_not_completed"


@pytest.mark.parametrize("kind", [K.CREDENTIALS_REJECTED, K.ACCOUNT_LOCKED, K.PASSWORD_CHANGE_REQUIRED, K.OTP_NOT_RECEIVED])
@patch("odc_core.login_failure.graph_client.send_mail")
def test_one_failed_login_of_several_is_reported_naming_the_login(mock_send, tmp_path, kind):
    errors = MagicMock()
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Source for Business", job_id=7, row=ROW,
        detail="Invalid password", kind=kind, username="second@acme.example", team_alert=False,
    )
    errors.add.assert_called_once()
    details = _details(errors)
    assert details["username"] == "second@acme.example"
    assert details["multi_credential"] is True


@patch("odc_core.login_failure.graph_client.send_mail")
def test_one_login_of_several_failing_for_our_own_reasons_stays_quiet(mock_send, tmp_path):
    errors = MagicMock()
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Source for Business", job_id=7, row=ROW,
        detail="Page did not load", username="second@acme.example", team_alert=False,
    )
    errors.add.assert_not_called()


@patch("odc_core.login_failure.graph_client.send_mail")
def test_multi_credential_is_worked_out_from_the_job_row(mock_send, tmp_path):
    errors = MagicMock()
    row = dict(ROW, username="multi_credential", password="multi_credential")
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Source for Business", job_id=7, row=row,
        detail="All logins failed", kind=K.CREDENTIALS_REJECTED,
    )
    assert _details(errors)["multi_credential"] is True
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Source for Business", job_id=7, row=row,
        detail="x", kind=K.CREDENTIALS_REJECTED, multi_credential=False,
    )
    assert _details(errors, 1)["multi_credential"] is False


@patch("odc_core.login_failure.graph_client.send_mail")
def test_an_old_automation_core_without_details_is_still_told(mock_send, tmp_path):
    calls = []

    class OldErrors:
        def add(self, message, *, exception=None):
            calls.append(message)

    login_failure.handle_login_failure(
        OldErrors(), _config(tmp_path), supplier_name="Wave", job_id=1, row=ROW, detail="x",
    )
    assert calls == ["Wave login failed for job 1 (other): Unclassified login failure - see detail. Detail: x"]


@patch("odc_core.db.pyodbc.connect")
def test_the_bot_no_longer_reports_to_the_database(mock_connect, tmp_path):
    config = _config(tmp_path)
    config["database"] = {"dsn": "Jupiter"}
    config["tables"] = {"live": {
        "report_login_failure": "Titan_INSE.dbo.spODC_ReportLoginFailure",
        "report_login_success": "Titan_INSE.dbo.spODC_ReportLoginSuccess",
    }}
    assert login_failure.report_login_failure(config, job_id=5, kind=K.CREDENTIALS_REJECTED) is None
    assert login_failure.report_login_success(config, job_id=5) is False
    with patch("odc_core.login_failure.graph_client.send_mail"):
        login_failure.handle_login_failure(
            MagicMock(), config, supplier_name="EDF Energy", job_id=5, row=ROW,
            detail="Invalid password", kind=K.CREDENTIALS_REJECTED,
        )
    mock_connect.assert_not_called()


# --- 0.14.2: specific kinds, descriptions, page diagnosis ---------------------

def test_every_kind_has_a_description():
    for kind in K:
        assert login_failure.describe(kind)
    assert login_failure.describe(K.CREDENTIALS_REJECTED).startswith("Invalid credentials")
    assert login_failure.describe("nonsense") == login_failure.describe(K.OTHER)


@pytest.mark.parametrize("kind", [
    K.PORTAL_UNAVAILABLE, K.LOGIN_FORM_NOT_FOUND, K.LANDING_PAGE_NOT_REACHED,
    K.OTP_PAGE_NOT_REACHED, K.OTP_REJECTED,
])
def test_new_kinds_stay_in_the_other_category_and_are_not_emailed(kind):
    # inse-toolkit rejects any category it does not know.
    assert login_failure.category_for(kind) == "other"
    assert kind not in login_failure.CLIENT_NOTIFIABLE_KINDS
    assert kind not in login_failure.OWNER_FIXABLE_KINDS


def test_escalate_prefers_the_furthest_stage_and_credentials_over_all():
    e = login_failure.escalate
    assert e(K.PORTAL_UNAVAILABLE, K.LOGIN_FORM_NOT_FOUND) == K.LOGIN_FORM_NOT_FOUND
    assert e(K.OTP_PAGE_NOT_REACHED, K.PORTAL_UNAVAILABLE) == K.OTP_PAGE_NOT_REACHED
    assert e(K.LANDING_PAGE_NOT_REACHED, K.OTHER) == K.LANDING_PAGE_NOT_REACHED
    assert e(K.OTP_REJECTED, K.CREDENTIALS_REJECTED) == K.CREDENTIALS_REJECTED
    assert e(K.LANDING_PAGE_NOT_REACHED, K.CREDENTIALS_REJECTED) == K.CREDENTIALS_REJECTED


class _FakePage:
    def __init__(self, messages, url="https://portal.example.com/login?err=1"):
        self._messages = messages
        self.url = url
        self.frames = [self]

    def evaluate(self, script, arg=None):
        if isinstance(arg, list):
            return list(self._messages)
        return {}

    def locator(self, selector):
        loc = MagicMock()
        loc.first.is_visible.return_value = False
        loc.count.return_value = 0
        return loc


class _BrokenPage:
    url = "about:blank"

    @property
    def frames(self):
        raise RuntimeError("page closed")

    def evaluate(self, *a, **k):
        raise RuntimeError("page closed")

    def locator(self, *a, **k):
        raise RuntimeError("page closed")


@patch("odc_core.password_change.detect_password_change", return_value=None)
def test_diagnose_page_classifies_the_banner(_pc):
    d = login_failure.diagnose_page(_FakePage(["Cookie settings", "Incorrect email or password"]))
    assert d.kind == K.CREDENTIALS_REJECTED
    assert d.portal_message.startswith("Incorrect email or password")
    assert d.page_url == "https://portal.example.com/login?err=1"


@patch("odc_core.password_change.detect_password_change", return_value=None)
def test_diagnose_page_keeps_unrecognised_text(_pc):
    d = login_failure.diagnose_page(_FakePage(["Something unexpected happened (ref 123)"]))
    assert d.kind == K.OTHER
    assert d.portal_message == "Something unexpected happened (ref 123)"


@patch("odc_core.password_change.detect_password_change", return_value="url /change-password")
def test_diagnose_page_spots_password_change(_pc):
    d = login_failure.diagnose_page(_FakePage([]))
    assert d.kind == K.PASSWORD_CHANGE_REQUIRED
    assert "change-password" in d.portal_message


def test_diagnose_page_never_raises():
    d = login_failure.diagnose_page(_BrokenPage())
    assert d.kind == K.OTHER and d.portal_message is None
    assert login_failure.diagnose_page(None) == login_failure.PageDiagnosis(K.OTHER, None, None)


def test_failure_details_carries_description_message_and_url():
    details = login_failure.failure_details(
        supplier_name="Acme Energy", job_id=7, kind=K.LANDING_PAGE_NOT_REACHED,
        portal_message="Welcome back", page_url="https://portal.example.com/home",
    )
    assert details["category"] == "other"
    assert details["kind"] == "landing page not reached"
    assert details["kind_description"].startswith("Landing page not reached")
    assert details["portal_message"] == "Welcome back"
    assert details["page_url"] == "https://portal.example.com/home"


@patch("odc_core.password_change.detect_password_change", return_value=None)
@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_reads_the_page_and_raises_the_kind(mock_send, _pc, tmp_path, caplog):
    errors = MagicMock()
    page = _FakePage(["Your email address or password is incorrect."])
    with caplog.at_level("ERROR"):
        login_failure.handle_login_failure(
            errors, _config(tmp_path), supplier_name="Acme Energy", job_id=9, row=ROW,
            detail="dashboard never loaded", kind=K.LANDING_PAGE_NOT_REACHED, page=page,
        )
    message = errors.add.call_args.args[0]
    details = errors.add.call_args.kwargs["details"]
    assert details["kind"] == "credentials rejected"
    assert details["category"] == "login_error"
    assert details["portal_message"] == "Your email address or password is incorrect."
    assert "Invalid credentials" in message and "Portal said:" in message
    assert "s3cret!" not in message and "s3cret!" not in caplog.text
    assert "portal_message='Your email address or password is incorrect.'" in caplog.text
    mock_send.assert_called_once()


@patch("odc_core.password_change.detect_password_change", return_value=None)
@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_never_lowers_the_bots_kind(mock_send, _pc, tmp_path):
    errors = MagicMock()
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Acme Energy", job_id=9, row=ROW,
        kind=K.OTP_PAGE_NOT_REACHED, page=_FakePage([]),
    )
    details = errors.add.call_args.kwargs["details"]
    assert details["kind"] == "otp page not reached"
    assert details["page_url"] == "https://portal.example.com/login?err=1"
    mock_send.assert_not_called()


@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_with_a_closed_page_still_reports(mock_send, tmp_path):
    errors = MagicMock()
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Acme Energy", job_id=9, row=ROW,
        kind=K.PORTAL_UNAVAILABLE, detail="goto timed out", page=_BrokenPage(),
    )
    assert errors.add.call_args.kwargs["details"]["kind"] == "portal unavailable"


# --- 0.14.2: screenshot on failure, multi-credential helpers ------------------

@patch("odc_core.password_change.detect_password_change", return_value=None)
@patch("odc_core.browser_helpers.take_error_screenshot", return_value="C:/logs/shot.png")
@patch("odc_core.login_failure.graph_client.send_mail")
def test_handle_takes_a_screenshot_when_the_bot_did_not(mock_send, mock_shot, _pc, tmp_path):
    errors = MagicMock()
    page = _FakePage(["Incorrect password"])
    page.is_closed = lambda: False
    login_failure.handle_login_failure(
        errors, _config(tmp_path), supplier_name="Acme Energy", job_id=9, row=ROW, page=page,
    )
    assert mock_shot.call_args.args[1] == "login_credentials_rejected"
    assert errors.add.call_args.kwargs["details"]["screenshot"]


class _Supplier:
    pass


@patch("odc_core.password_change.detect_password_change", return_value=None)
@patch("odc_core.login_failure.graph_client.send_mail")
def test_multi_credential_failures_are_reported_per_login_and_together(mock_send, _pc, tmp_path):
    errors = MagicMock()
    s = _Supplier()
    s._login_failure_detail = "Landing page not reached"
    s._login_failure_kind = K.LANDING_PAGE_NOT_REACHED
    s._login_failure_screenshot = None
    first = login_failure.handle_credential_login_failure(
        errors, _config(tmp_path), s, supplier_name="Drax", job_id=5, row=ROW,
        username="a@x.com", page=_FakePage(["Your password is incorrect"]),
        credential_index=1, credential_total=2,
    )
    assert first.kind == K.CREDENTIALS_REJECTED
    assert first.detail.startswith("[credential 1/2]")
    assert s._login_failure_kind == K.OTHER and s._login_failure_detail == ""
    per_login = errors.add.call_args.kwargs["details"]
    assert per_login["username"] == "a@x.com" and per_login["multi_credential"] is True

    s._login_failure_kind = K.PORTAL_UNAVAILABLE
    second = login_failure.handle_credential_login_failure(
        errors, _config(tmp_path), s, supplier_name="Drax", job_id=5, row=ROW, username="b@x.com",
    )
    assert second.kind == K.PORTAL_UNAVAILABLE

    errors.reset_mock()
    kind = login_failure.report_all_credentials_failed(
        errors, supplier_name="Drax", job_id=5, failures=[first, second], row=ROW, pending_accounts=3,
    )
    assert kind == K.CREDENTIALS_REJECTED
    message = errors.add.call_args.args[0]
    details = errors.add.call_args.kwargs["details"]
    assert "all 2 credential(s)" in message and "3 account(s) still pending" in message
    assert details["category"] == "other" and details["all_credentials_failed"] is True
    assert [c["username"] for c in details["credentials"]] == ["a@x.com", "b@x.com"]
    assert details["credentials"][0]["portal_message"] == "Your password is incorrect"
    assert "s3cret!" not in json.dumps(details)


def test_some_logins_worked_is_not_called_all():
    errors = MagicMock()
    f = login_failure.CredentialFailure("c@x.com", K.CREDENTIALS_REJECTED, "d", None, None, None)
    login_failure.report_all_credentials_failed(
        errors, supplier_name="Drax", job_id=5, failures=[f], pending_accounts=2, credential_total=3,
    )
    assert "login failed on 1 of 3 credential(s) for job 5" in errors.add.call_args.args[0]
    details = errors.add.call_args.kwargs["details"]
    assert details["all_credentials_failed"] is False and details["credential_total"] == 3
