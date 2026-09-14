import json
from unittest.mock import MagicMock, patch

from odc_core import human_in_loop


def _page_mock(*, button_confirmed: bool = False) -> MagicMock:
    page = MagicMock()

    def evaluate(script, *args):
        if "dataset.confirmed" in script:
            return button_confirmed
        return None

    page.evaluate.side_effect = evaluate
    return page


def _banner_messages(page: MagicMock) -> list[str]:
    return [
        call.args[1]["message"]
        for call in page.evaluate.call_args_list
        if call.args and call.args[0] == human_in_loop._BANNER_JS
    ]


def test_request_assist_writes_reason_json(tmp_path):
    human_in_loop.request_assist("JOB1", "captcha on the Energia portal", tmp_path)

    request_path = tmp_path / human_in_loop.ASSIST_REQUEST_FILENAME
    assert request_path.exists()
    assert json.loads(request_path.read_text(encoding="utf-8")) == {
        "reason": "captcha on the Energia portal",
    }
    # no leftover temp file
    assert list(tmp_path.iterdir()) == [request_path]


def test_request_assist_swallows_write_failure(tmp_path):
    missing_dir = tmp_path / "does-not-exist"

    human_in_loop.request_assist("JOB1", "captcha", missing_dir)  # must not raise


def test_release_assist_writes_empty_file(tmp_path):
    human_in_loop.release_assist("JOB1", tmp_path)

    release_path = tmp_path / human_in_loop.ASSIST_RELEASE_FILENAME
    assert release_path.exists()
    assert release_path.read_text(encoding="utf-8") == ""


def test_release_assist_swallows_write_failure(tmp_path):
    missing_dir = tmp_path / "does-not-exist"

    human_in_loop.release_assist("JOB1", missing_dir)  # must not raise


@patch("odc_core.human_in_loop.time.sleep")
def test_started_message_defaults_to_generic_wording(mock_sleep, tmp_path):
    page = _page_mock(button_confirmed=True)

    human_in_loop.wait_for_human_login(
        page, MagicMock(return_value=False), "JOB1", tmp_path, 600, {},
    )

    assert human_in_loop.DEFAULT_STARTED_MESSAGE in _banner_messages(page)


@patch("odc_core.human_in_loop.time.sleep")
def test_started_message_override_reaches_banner(mock_sleep, tmp_path):
    page = _page_mock(button_confirmed=True)
    custom = "Please resolve the reCAPTCHA challenge, then click 'I'm logged in'"

    human_in_loop.wait_for_human_login(
        page, MagicMock(return_value=False), "JOB1", tmp_path, 600, {},
        started_message=custom,
    )

    messages = _banner_messages(page)
    assert custom in messages
    assert human_in_loop.DEFAULT_STARTED_MESSAGE not in messages


@patch("odc_core.human_in_loop.time.sleep")
def test_success_via_button_click_releases_assist(mock_sleep, tmp_path):
    page = _page_mock(button_confirmed=True)
    is_logged_in = MagicMock(return_value=False)

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", tmp_path, 600, {},
    )

    assert result is True
    mock_sleep.assert_called_once_with(human_in_loop.TAKEOVER_PAUSE_S)
    assert (tmp_path / human_in_loop.ASSIST_RELEASE_FILENAME).exists()


@patch("odc_core.human_in_loop.browser_helpers.take_error_screenshot")
@patch("odc_core.human_in_loop.time.sleep")
def test_is_logged_in_alone_does_not_trigger_success(mock_sleep, mock_screenshot, tmp_path):
    # is_logged_in() is diagnostic-only: a URL/DOM marker can be true on an
    # intermediate page mid-login, so only the explicit button click may
    # end the wait successfully - see human_in_loop._poll_until_logged_in().
    page = _page_mock(button_confirmed=False)
    is_logged_in = MagicMock(return_value=True)

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", tmp_path, 0, {},
    )

    assert result is False
    assert (tmp_path / human_in_loop.ASSIST_RELEASE_FILENAME).exists()


@patch("odc_core.human_in_loop.browser_helpers.take_error_screenshot")
@patch("odc_core.human_in_loop.time.sleep")
def test_timeout_returns_false_and_still_releases_assist(mock_sleep, mock_screenshot, tmp_path):
    page = _page_mock()
    is_logged_in = MagicMock(return_value=False)

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", tmp_path, 0, {},
    )

    assert result is False
    mock_screenshot.assert_called_once()
    assert (tmp_path / human_in_loop.ASSIST_RELEASE_FILENAME).exists()


@patch("odc_core.human_in_loop.time.sleep")
def test_is_logged_in_exception_still_releases_assist_and_propagates(mock_sleep, tmp_path):
    page = _page_mock()
    is_logged_in = MagicMock(side_effect=RuntimeError("portal exploded"))

    try:
        human_in_loop.wait_for_human_login(
            page, is_logged_in, "JOB1", tmp_path, 600, {},
        )
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True
    assert (tmp_path / human_in_loop.ASSIST_RELEASE_FILENAME).exists()


def test_confirm_button_state_uses_dom_not_window_global():
    # Regression guard: a window.* global set by the click handler is not
    # reliably visible to a separate page.evaluate() call under stealth
    # browser-automation patches (e.g. patchright) that isolate injected
    # scripts into a different JS world than real user interaction. The DOM
    # is the one thing guaranteed shared across worlds - see this module's
    # comment above _LOGIN_CONFIRM_BUTTON_JS.
    assert "window.__odc" not in human_in_loop._LOGIN_CONFIRM_BUTTON_JS
    assert "dataset.confirmed" in human_in_loop._LOGIN_CONFIRM_BUTTON_JS

    page = MagicMock()
    human_in_loop._login_confirmed_by_button(page)
    read_script = page.evaluate.call_args.args[0]
    assert "window." not in read_script
    assert "dataset.confirmed" in read_script


def test_show_banner_swallows_evaluate_errors():
    page = MagicMock()
    page.evaluate.side_effect = Exception("mid-navigation")

    human_in_loop._show_banner(page, "hello", "#000000")  # must not raise


def test_login_confirmed_by_button_returns_false_on_error():
    page = MagicMock()
    page.evaluate.side_effect = Exception("navigated away")

    assert human_in_loop._login_confirmed_by_button(page) is False


def test_login_confirmed_by_button_reads_flag():
    page = _page_mock(button_confirmed=True)

    assert human_in_loop._login_confirmed_by_button(page) is True


def test_inject_login_confirm_button_calls_add_init_script_and_evaluate():
    page = MagicMock()

    human_in_loop._inject_login_confirm_button(page)

    page.add_init_script.assert_called_once_with(human_in_loop._LOGIN_CONFIRM_BUTTON_JS)
    page.evaluate.assert_called_once_with(human_in_loop._LOGIN_CONFIRM_BUTTON_JS)
