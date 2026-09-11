from unittest.mock import MagicMock, patch

from odc_core import human_in_loop

TABLES = {"jobs": "ODC_jobs"}


def _page_mock(*, button_confirmed: bool = False) -> MagicMock:
    page = MagicMock()

    def evaluate(script, *args):
        if "dataset.confirmed" in script:
            return button_confirmed
        return None

    page.evaluate.side_effect = evaluate
    return page


@patch("odc_core.human_in_loop.socket.gethostname", return_value="MAN-RDS-V12")
def test_get_current_hostname_wraps_socket(mock_gethostname):
    assert human_in_loop.get_current_hostname() == "MAN-RDS-V12"
    mock_gethostname.assert_called_once_with()


def test_get_current_session_id_reads_via_kernel32(monkeypatch):
    def fake_process_id_to_session_id(pid, session_id_ref):
        session_id_ref._obj.value = 3
        return 1  # non-zero == success

    monkeypatch.setattr(
        human_in_loop.ctypes.windll.kernel32,
        "ProcessIdToSessionId",
        fake_process_id_to_session_id,
        raising=False,
    )

    assert human_in_loop.get_current_session_id() == 3


def test_get_current_session_id_raises_on_failure(monkeypatch):
    monkeypatch.setattr(
        human_in_loop.ctypes.windll.kernel32,
        "ProcessIdToSessionId",
        lambda pid, session_id_ref: 0,  # 0 == failure
        raising=False,
    )

    try:
        human_in_loop.get_current_session_id()
        raised = False
    except OSError:
        raised = True

    assert raised is True


@patch("odc_core.human_in_loop.socket.create_connection")
@patch("odc_core.human_in_loop.subprocess.Popen")
def test_start_vnc_server_launches_and_confirms_port_open(mock_popen, mock_create_connection):
    result = human_in_loop.start_vnc_server(3)

    assert result is True
    mock_popen.assert_called_once_with(["tvnserver", "-run"])
    mock_create_connection.assert_called_once_with(
        ("127.0.0.1", human_in_loop.VNC_PORT_BASE + 3),
        timeout=human_in_loop.VNC_PORT_POLL_INTERVAL_S,
    )


@patch("odc_core.human_in_loop.subprocess.Popen", side_effect=FileNotFoundError("no tvnserver"))
def test_start_vnc_server_swallows_launch_failure(mock_popen):
    result = human_in_loop.start_vnc_server(3)  # must not raise

    assert result is False
    mock_popen.assert_called_once()


@patch("odc_core.human_in_loop.time.sleep")
@patch("odc_core.human_in_loop.socket.create_connection", side_effect=OSError("refused"))
@patch("odc_core.human_in_loop.subprocess.Popen")
def test_start_vnc_server_times_out_when_port_never_opens(
    mock_popen, mock_create_connection, mock_sleep,
):
    result = human_in_loop.start_vnc_server(3, timeout_s=0.01)

    assert result is False
    mock_create_connection.assert_called()


@patch("odc_core.human_in_loop.start_vnc_server")
@patch("odc_core.human_in_loop.get_current_session_id", return_value=3)
@patch("odc_core.human_in_loop.get_current_hostname", return_value="MAN-RDS-V12")
def test_prepare_vnc_session_resolves_and_starts_when_human_in_loop_truthy(
    mock_get_hostname, mock_get_session_id, mock_start_vnc,
):
    result = human_in_loop.prepare_vnc_session(1)

    assert result == ("MAN-RDS-V12", 3)
    mock_start_vnc.assert_called_once_with(3, timeout_s=human_in_loop.VNC_PORT_POLL_TIMEOUT_S)


@patch("odc_core.human_in_loop.start_vnc_server")
@patch("odc_core.human_in_loop.get_current_session_id")
@patch("odc_core.human_in_loop.get_current_hostname")
def test_prepare_vnc_session_returns_none_when_human_in_loop_falsy(
    mock_get_hostname, mock_get_session_id, mock_start_vnc,
):
    for falsy in (None, 0, False):
        assert human_in_loop.prepare_vnc_session(falsy) is None

    mock_get_hostname.assert_not_called()
    mock_get_session_id.assert_not_called()
    mock_start_vnc.assert_not_called()


def _banner_messages(page: MagicMock) -> list[str]:
    return [
        call.args[1]["message"]
        for call in page.evaluate.call_args_list
        if call.args and call.args[0] == human_in_loop._BANNER_JS
    ]


@patch("odc_core.jobstodo.clear_human_wait")
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait")
@patch("odc_core.human_in_loop.time.sleep")
def test_started_message_defaults_to_generic_wording(mock_sleep, mock_set, mock_complete, mock_clear):
    page = _page_mock(button_confirmed=True)

    human_in_loop.wait_for_human_login(
        page, MagicMock(return_value=False), "JOB1", 600,
        "RDS01", 3, TABLES, "Jupiter", {},
    )

    assert human_in_loop.DEFAULT_STARTED_MESSAGE in _banner_messages(page)


@patch("odc_core.jobstodo.clear_human_wait")
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait")
@patch("odc_core.human_in_loop.time.sleep")
def test_started_message_override_reaches_banner(mock_sleep, mock_set, mock_complete, mock_clear):
    page = _page_mock(button_confirmed=True)
    custom = "Please resolve the reCAPTCHA challenge, then click 'I'm logged in'"

    human_in_loop.wait_for_human_login(
        page, MagicMock(return_value=False), "JOB1", 600,
        "RDS01", 3, TABLES, "Jupiter", {},
        started_message=custom,
    )

    messages = _banner_messages(page)
    assert custom in messages
    assert human_in_loop.DEFAULT_STARTED_MESSAGE not in messages


@patch("odc_core.jobstodo.clear_human_wait")
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait")
@patch("odc_core.human_in_loop.time.sleep")
def test_success_via_button_click_completes_and_does_not_clear(
    mock_sleep, mock_set, mock_complete, mock_clear,
):
    # HUMAN_WAIT_COMPLETE is a persistent terminal state on success - a
    # poller must be able to observe it without racing a fixed window, so
    # clear_human_wait() must NOT run right after set_human_wait_complete().
    page = _page_mock(button_confirmed=True)
    is_logged_in = MagicMock(return_value=False)

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", 600, "RDS01", 3, TABLES, "Jupiter", {},
    )

    assert result is True
    mock_set.assert_called_once_with("JOB1", 600, "RDS01", 3, TABLES, "Jupiter")
    mock_complete.assert_called_once_with("JOB1", TABLES, "Jupiter")
    mock_clear.assert_not_called()
    mock_sleep.assert_called_once_with(human_in_loop.TAKEOVER_PAUSE_S)


@patch("odc_core.human_in_loop.browser_helpers.take_error_screenshot")
@patch("odc_core.jobstodo.clear_human_wait")
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait")
@patch("odc_core.human_in_loop.time.sleep")
def test_is_logged_in_alone_does_not_trigger_success(
    mock_sleep, mock_set, mock_complete, mock_clear, mock_screenshot,
):
    # is_logged_in() is diagnostic-only: a URL/DOM marker can be true on an
    # intermediate page mid-login, so only the explicit button click may
    # end the wait successfully - see human_in_loop._poll_until_logged_in().
    page = _page_mock(button_confirmed=False)
    is_logged_in = MagicMock(return_value=True)

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", 0, "RDS01", 3, TABLES, "Jupiter", {},
    )

    assert result is False
    mock_complete.assert_not_called()
    mock_clear.assert_called_once_with("JOB1", TABLES, "Jupiter")


@patch("odc_core.human_in_loop.browser_helpers.take_error_screenshot")
@patch("odc_core.jobstodo.clear_human_wait")
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait")
@patch("odc_core.human_in_loop.time.sleep")
def test_timeout_returns_false_and_never_completes(
    mock_sleep, mock_set, mock_complete, mock_clear, mock_screenshot,
):
    page = _page_mock()
    is_logged_in = MagicMock(return_value=False)

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", 0, "RDS01", 3, TABLES, "Jupiter", {},
    )

    assert result is False
    mock_complete.assert_not_called()
    mock_clear.assert_called_once_with("JOB1", TABLES, "Jupiter")
    mock_screenshot.assert_called_once()


@patch("odc_core.jobstodo.clear_human_wait")
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait", side_effect=Exception("column does not exist"))
@patch("odc_core.human_in_loop.time.sleep")
def test_set_human_wait_failure_is_swallowed_and_wait_still_runs(
    mock_sleep, mock_set, mock_complete, mock_clear,
):
    page = _page_mock(button_confirmed=True)
    is_logged_in = MagicMock(return_value=False)

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", 600, "RDS01", 3, TABLES, "Jupiter", {},
    )

    assert result is True
    mock_complete.assert_called_once()
    mock_clear.assert_not_called()


@patch("odc_core.human_in_loop.browser_helpers.take_error_screenshot")
@patch("odc_core.jobstodo.clear_human_wait", side_effect=Exception("column does not exist"))
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait")
@patch("odc_core.human_in_loop.time.sleep")
def test_clear_human_wait_failure_is_swallowed(
    mock_sleep, mock_set, mock_complete, mock_clear, mock_screenshot,
):
    # Only a non-success (here, timeout) path calls clear_human_wait() at
    # all, so that is where its own failure needs to be exercised.
    page = _page_mock(button_confirmed=False)
    is_logged_in = MagicMock(return_value=False)

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", 0, "RDS01", 3, TABLES, "Jupiter", {},
    )

    assert result is False
    mock_clear.assert_called_once()


@patch("odc_core.jobstodo.clear_human_wait")
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait")
@patch("odc_core.human_in_loop.time.sleep")
def test_is_logged_in_exception_still_clears_wait_and_propagates(
    mock_sleep, mock_set, mock_complete, mock_clear,
):
    page = _page_mock()
    is_logged_in = MagicMock(side_effect=RuntimeError("portal exploded"))

    try:
        human_in_loop.wait_for_human_login(
            page, is_logged_in, "JOB1", 600, "RDS01", 3, TABLES, "Jupiter", {},
        )
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True
    mock_complete.assert_not_called()
    mock_clear.assert_called_once_with("JOB1", TABLES, "Jupiter")


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
