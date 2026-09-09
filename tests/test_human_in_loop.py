from unittest.mock import MagicMock, patch

from odc_core import human_in_loop

TABLES = {"jobs": "ODC_jobs"}


def _page_mock(*, button_confirmed: bool = False) -> MagicMock:
    page = MagicMock()

    def evaluate(script, *args):
        if "__odcHumanLoopConfirmed" in script:
            return button_confirmed
        return None

    page.evaluate.side_effect = evaluate
    return page


@patch("odc_core.jobstodo.clear_human_wait")
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait")
@patch("odc_core.human_in_loop.time.sleep")
def test_success_via_button_click_calls_complete_then_clear_in_order(
    mock_sleep, mock_set, mock_complete, mock_clear,
):
    page = _page_mock(button_confirmed=True)
    is_logged_in = MagicMock(return_value=False)
    call_order: list[str] = []
    mock_complete.side_effect = lambda *a, **k: call_order.append("complete")
    mock_clear.side_effect = lambda *a, **k: call_order.append("clear")

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", 600, "RDS01", "rdpuser", "rdppass", TABLES, "Jupiter", {},
    )

    assert result is True
    mock_set.assert_called_once_with(
        "JOB1", 600, "RDS01", "rdpuser", "rdppass", TABLES, "Jupiter",
    )
    mock_complete.assert_called_once_with("JOB1", TABLES, "Jupiter")
    mock_clear.assert_called_once_with("JOB1", TABLES, "Jupiter")
    # clear_human_wait must run after set_human_wait_complete (COMPLETE -> NULL, not skipped).
    assert call_order == ["complete", "clear"]
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
        page, is_logged_in, "JOB1", 0, "RDS01", "rdpuser", "rdppass", TABLES, "Jupiter", {},
    )

    assert result is False
    mock_complete.assert_not_called()
    mock_clear.assert_called_once_with("JOB1", TABLES, "Jupiter")


@patch("odc_core.jobstodo.clear_human_wait")
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait")
@patch("odc_core.human_in_loop.time.sleep")
def test_success_via_button_click(mock_sleep, mock_set, mock_complete, mock_clear):
    page = _page_mock(button_confirmed=True)
    is_logged_in = MagicMock(return_value=False)

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", 600, "RDS01", "rdpuser", "rdppass", TABLES, "Jupiter", {},
    )

    assert result is True
    mock_complete.assert_called_once_with("JOB1", TABLES, "Jupiter")
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
        page, is_logged_in, "JOB1", 0, "RDS01", "rdpuser", "rdppass", TABLES, "Jupiter", {},
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
        page, is_logged_in, "JOB1", 600, "RDS01", "rdpuser", "rdppass", TABLES, "Jupiter", {},
    )

    assert result is True
    mock_complete.assert_called_once()
    mock_clear.assert_called_once()


@patch("odc_core.jobstodo.clear_human_wait", side_effect=Exception("column does not exist"))
@patch("odc_core.jobstodo.set_human_wait_complete")
@patch("odc_core.jobstodo.set_human_wait")
@patch("odc_core.human_in_loop.time.sleep")
def test_clear_human_wait_failure_is_swallowed(mock_sleep, mock_set, mock_complete, mock_clear):
    page = _page_mock(button_confirmed=True)
    is_logged_in = MagicMock(return_value=False)

    result = human_in_loop.wait_for_human_login(
        page, is_logged_in, "JOB1", 600, "RDS01", "rdpuser", "rdppass", TABLES, "Jupiter", {},
    )

    assert result is True
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
            page, is_logged_in, "JOB1", 600, "RDS01", "rdpuser", "rdppass", TABLES, "Jupiter", {},
        )
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True
    mock_complete.assert_not_called()
    mock_clear.assert_called_once_with("JOB1", TABLES, "Jupiter")


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
