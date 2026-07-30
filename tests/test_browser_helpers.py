from unittest.mock import MagicMock, patch

from odc_core import browser_helpers


@patch("odc_core.browser_helpers.time.sleep", return_value=None)
def test_human_delay_sleeps_within_configured_range(mock_sleep):
    config = {"delays": {"action_min_s": 1.0, "action_max_s": 1.0}}

    browser_helpers.human_delay(config)

    mock_sleep.assert_called_once()
    assert mock_sleep.call_args.args[0] == 1.0


@patch("odc_core.browser_helpers.time.sleep", return_value=None)
def test_move_mouse_toward_moves_mouse_when_box_available(mock_sleep):
    locator = MagicMock()
    locator.bounding_box.return_value = {"x": 0, "y": 0, "width": 100, "height": 50}
    page = MagicMock()

    browser_helpers.move_mouse_toward(page, locator)

    assert page.mouse.move.called


@patch("odc_core.browser_helpers.time.sleep", return_value=None)
def test_move_mouse_toward_no_box_does_not_move_mouse(mock_sleep):
    locator = MagicMock()
    locator.bounding_box.return_value = None
    page = MagicMock()

    browser_helpers.move_mouse_toward(page, locator)

    page.mouse.move.assert_not_called()


@patch("odc_core.browser_helpers.time.sleep", return_value=None)
def test_move_mouse_toward_resolves_string_selector(mock_sleep):
    page = MagicMock()
    resolved = MagicMock()
    resolved.bounding_box.return_value = {"x": 0, "y": 0, "width": 10, "height": 10}
    page.locator.return_value.first = resolved

    browser_helpers.move_mouse_toward(page, "#target")

    page.locator.assert_called_once_with("#target")
    resolved.scroll_into_view_if_needed.assert_called_once()


@patch("odc_core.browser_helpers.time.sleep", return_value=None)
def test_move_mouse_toward_survives_bounding_box_error(mock_sleep):
    locator = MagicMock()
    locator.bounding_box.side_effect = RuntimeError("detached")
    page = MagicMock()

    browser_helpers.move_mouse_toward(page, locator)

    page.mouse.move.assert_not_called()


@patch("odc_core.browser_helpers.move_mouse_toward")
def test_human_click_clicks_locator(mock_move):
    locator = MagicMock()
    page = MagicMock()

    browser_helpers.human_click(page, locator)

    locator.click.assert_called_once()


@patch("odc_core.browser_helpers.move_mouse_toward")
def test_human_click_resolves_string_selector(mock_move):
    page = MagicMock()
    resolved = MagicMock()
    page.locator.return_value.first = resolved

    browser_helpers.human_click(page, "#submit")

    page.locator.assert_called_once_with("#submit")
    resolved.click.assert_called_once()


def test_human_type_uses_configured_delay_range():
    page = MagicMock()
    config = {"delays": {"keystroke_min_ms": 10, "keystroke_max_ms": 10}}

    browser_helpers.human_type(page, "#field", "hello", config)

    page.type.assert_called_once_with("#field", "hello", delay=10)


def test_take_error_screenshot_writes_to_logs_dir(tmp_path):
    page = MagicMock()
    config = {
        "_logs_dir": str(tmp_path),
        "_process_id": "PROC1",
        "_supplier_name": "crown",
    }

    browser_helpers.take_error_screenshot(page, "login_failed", config)

    page.screenshot.assert_called_once()
    saved_path = page.screenshot.call_args.kwargs["path"]
    assert page.screenshot.call_args.kwargs["full_page"] is True
    assert saved_path.startswith(str(tmp_path))
    assert "PROC1_CROWN_login_failed_" in saved_path
    assert saved_path.endswith(".png")


def test_take_error_screenshot_swallows_failures(tmp_path):
    # A screenshot problem must never interrupt the main process.
    page = MagicMock()
    page.screenshot.side_effect = RuntimeError("browser closed")

    browser_helpers.take_error_screenshot(page, "login_failed", {"_logs_dir": str(tmp_path)})


def test_dismiss_cookie_banner_clicks_first_visible_button():
    page = MagicMock()
    button = MagicMock()
    button.is_visible.return_value = True
    page.locator.return_value.first = button

    result = browser_helpers.dismiss_cookie_banner(page)

    assert result is True
    button.click.assert_called_once()


def test_dismiss_cookie_banner_returns_false_when_none_visible():
    page = MagicMock()
    button = MagicMock()
    button.is_visible.return_value = False
    page.locator.return_value.first = button

    result = browser_helpers.dismiss_cookie_banner(page)

    assert result is False


@patch("odc_core.browser_helpers.dismiss_cookie_banner")
def test_dismiss_cookie_banners_covers_all_open_pages(mock_dismiss):
    page1 = MagicMock()
    page2 = MagicMock()
    page1.is_closed.return_value = False
    page2.is_closed.return_value = False
    page1.context.pages = [page1, page2]

    browser_helpers.dismiss_cookie_banners(page1)

    assert mock_dismiss.call_count == 2
