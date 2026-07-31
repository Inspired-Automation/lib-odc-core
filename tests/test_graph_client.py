from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from odc_core import graph_client

CONFIG = {
    "graph": {
        "tenant_id": "tenant",
        "client_id": "client",
        "client_secret": "secret",
        "sender_address": "bot@example.com",
    }
}


def _mock_msal_app(token_result):
    app = MagicMock()
    app.acquire_token_for_client.return_value = token_result
    return app


def test_acquire_token_missing_config_raises():
    with pytest.raises(ValueError):
        graph_client._acquire_token({"graph": {}})


@patch("odc_core.graph_client.msal.ConfidentialClientApplication")
def test_acquire_token_success(mock_app_cls):
    mock_app_cls.return_value = _mock_msal_app({"access_token": "tok123"})

    token = graph_client._acquire_token(CONFIG)

    assert token == "tok123"


@patch("odc_core.graph_client.msal.ConfidentialClientApplication")
def test_acquire_token_failure_raises_runtime_error(mock_app_cls):
    mock_app_cls.return_value = _mock_msal_app(
        {"error": "invalid_client", "error_description": "bad secret"}
    )

    with pytest.raises(RuntimeError):
        graph_client._acquire_token(CONFIG)


@patch("odc_core.graph_client.requests.post")
@patch("odc_core.graph_client._acquire_token", return_value="tok123")
def test_send_mail_success(mock_token, mock_post):
    mock_post.return_value = MagicMock(status_code=202)

    graph_client.send_mail(CONFIG, "user@example.com", "Subject", "Body")

    mock_post.assert_called_once()
    assert "sendMail" in mock_post.call_args.args[0]


@patch("odc_core.graph_client.requests.post")
@patch("odc_core.graph_client._acquire_token", return_value="tok123")
def test_send_mail_failure_raises(mock_token, mock_post):
    mock_post.return_value = MagicMock(status_code=400, text="bad request")

    with pytest.raises(RuntimeError):
        graph_client.send_mail(CONFIG, "user@example.com", "Subject", "Body")


def test_send_mail_missing_sender_raises():
    with pytest.raises(ValueError):
        graph_client.send_mail({"graph": {}}, "user@example.com", "s", "b")


def test_parse_otp_from_body_extracts_digits():
    body = "Hello,START123456END goodbye"
    code = graph_client._parse_otp_from_body(body, body_start="START", body_end="END")
    assert code == "123456"


def test_parse_otp_from_body_missing_start_returns_empty():
    code = graph_client._parse_otp_from_body("no marker here", body_start="START", body_end="END")
    assert code == ""


def test_parse_otp_from_body_no_digits_returns_empty():
    # Marker present but no 4-8 digit group: must return "" so read_otp_code
    # keeps polling rather than treating the body text as a code.
    body = "Hello,STARTno code in hereEND goodbye"
    code = graph_client._parse_otp_from_body(body, body_start="START", body_end="END")
    assert code == ""


def test_parse_otp_from_body_ignores_digits_in_html_styles():
    # Regression: the real Wave OTP mail renders the code inside a styled div, and
    # the hex colour sat between the marker and the code - so the digit search
    # returned 202020 instead of 965206 on every single run.
    body = (
        '<div style="font-size:16px">Your one-time code is:</div>'
        '<div style="letter-spacing:2px; color:#202020">965206</div>'
        "<div>This code expires in 10 minutes.</div>"
    )
    code = graph_client._parse_otp_from_body(
        body,
        body_start="Your one-time code is:",
        body_end="This code expires in 10 minutes.",
    )
    assert code == "965206"


def test_parse_otp_from_body_handles_marker_split_across_tags():
    body = "<p>Your one-time <b>code</b> is:</p><h1>745227</h1><p>expires</p>"
    code = graph_client._parse_otp_from_body(
        body, body_start="Your one-time code is:", body_end="expires",
    )
    assert code == "745227"


def test_parse_otp_from_body_unescapes_entities():
    body = "<p>Code&nbsp;is:</p><span>4321</span>"
    code = graph_client._parse_otp_from_body(body, body_start="is:", body_end="ZZZ")
    assert code == "4321"


@patch("odc_core.graph_client.requests.get")
@patch("odc_core.graph_client._acquire_token", return_value="tok123")
def test_list_recent_messages_requests_text_body(mock_token, mock_get):
    # The Prefer header is what stops Graph returning the HTML alternative.
    mock_get.return_value = MagicMock(status_code=200, json=lambda: {"value": []})

    graph_client._list_recent_messages(
        "tok123", "mailbox@example.com", datetime.now(timezone.utc), "code",
    )

    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Prefer"] == 'outlook.body-content-type="text"'


@patch("odc_core.graph_client.time.sleep", return_value=None)
@patch("odc_core.graph_client._list_recent_messages")
@patch("odc_core.graph_client._acquire_token", return_value="tok123")
def test_read_otp_code_found(mock_token, mock_list, mock_sleep):
    mock_list.return_value = [
        {"subject": "Your code", "body": {"content": "STARTCODE654321ENDrest"}}
    ]

    code = graph_client.read_otp_code(
        CONFIG,
        "mailbox@example.com",
        datetime.now(timezone.utc),
        subject_contains="code",
        body_start="STARTCODE",
        body_end="END",
        timeout_s=5,
        poll_interval_s=0,
    )

    assert code == "654321"


@patch("odc_core.graph_client.time.sleep", return_value=None)
@patch("odc_core.graph_client.time.time")
@patch("odc_core.graph_client._list_recent_messages", return_value=[])
@patch("odc_core.graph_client._acquire_token", return_value="tok123")
def test_read_otp_code_times_out(mock_token, mock_list, mock_time, mock_sleep):
    # Monotonic clock rather than a fixed list, so the test still asserts the
    # TimeoutError if the implementation changes how often it calls time().
    clock = iter(range(0, 1000))
    mock_time.side_effect = lambda: next(clock)

    with pytest.raises(TimeoutError):
        graph_client.read_otp_code(
            CONFIG,
            "mailbox@example.com",
            datetime.now(timezone.utc),
            subject_contains="code",
            body_start="STARTCODE",
            body_end="END",
            timeout_s=5,
            poll_interval_s=0,
        )
