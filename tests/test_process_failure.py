from unittest.mock import MagicMock, patch

from odc_core import process_failure as pf
from odc_core.process_failure import ProcessErrorKind as P


class _Page:
    def __init__(self, messages=(), url="https://portal.example.com/bills", closed=False):
        self._messages = list(messages)
        self.url = url
        self.frames = [self]
        self._closed = closed

    def is_closed(self):
        return self._closed

    def evaluate(self, script, arg=None):
        return list(self._messages)


CONFIG = {"env": "dev"}


def test_every_kind_has_a_description():
    for kind in P:
        assert pf.describe_process_error(kind)


@patch("odc_core.browser_helpers.take_error_screenshot", return_value="C:/logs/x.png")
def test_reports_with_screenshot_message_and_url(mock_shot, caplog):
    errors = MagicMock()
    with caplog.at_level("ERROR"):
        details = pf.report_process_error(
            errors, CONFIG, supplier_name="Wave", job_id="12", kind=P.DOWNLOAD_FAILED,
            detail="PDF empty", page=_Page(["Document unavailable"]), account_ref="ACC/1",
            job_details_id=77,
        )
    assert mock_shot.call_args.args[1] == "download_failed_ACC_1"
    assert details["kind"] == "download failed" and details["category"] == "other"
    assert details["portal_message"] == "Document unavailable"
    assert details["page_url"] == "https://portal.example.com/bills"
    assert details["odc_job_id"] == 12 and details["job_details_id"] == 77
    message = errors.add.call_args.args[0]
    assert "Wave download failed on job 12 account ACC/1" in message and "Portal said" in message
    assert "PROCESS_ERROR - Wave job 12 | kind=download failed" in caplog.text


@patch("odc_core.browser_helpers.take_error_screenshot", return_value=None)
def test_page_message_sharpens_a_vague_kind(_shot):
    d = pf.report_process_error(MagicMock(), CONFIG, supplier_name="X", job_id=1, kind=P.NAVIGATION_FAILED,
                                page=_Page(["Your session has expired, please log in again"]))
    assert d["kind"] == "session expired"
    d = pf.report_process_error(MagicMock(), CONFIG, supplier_name="X", job_id=1, kind=P.OTHER,
                                page=_Page(["Service temporarily unavailable"]))
    assert d["kind"] == "portal unavailable"
    # A specific kind the bot chose is kept.
    d = pf.report_process_error(MagicMock(), CONFIG, supplier_name="X", job_id=1, kind=P.DOWNLOAD_FAILED,
                                page=_Page(["Service temporarily unavailable"]))
    assert d["kind"] == "download failed"


@patch("odc_core.browser_helpers.take_error_screenshot")
def test_closed_page_and_browser_crash(mock_shot):
    errors = MagicMock()
    exc = RuntimeError("Target page, context or browser has been closed")
    d = pf.report_process_error(errors, CONFIG, supplier_name="X", job_id=1, kind=P.UNHANDLED_EXCEPTION,
                                page=_Page(closed=True), exception=exc)
    mock_shot.assert_not_called()
    assert d["kind"] == "browser crashed" and d["exception_type"] == "RuntimeError"
    assert errors.add.call_args.kwargs["exception"] is exc


def test_team_alert_false_only_logs_and_never_raises():
    errors = MagicMock()
    errors.add.side_effect = RuntimeError("boom")
    pf.report_process_error(errors, CONFIG, supplier_name="X", job_id=1, kind="nonsense", team_alert=False)
    errors.add.assert_not_called()
    d = pf.report_process_error(errors, CONFIG, supplier_name="X", job_id=1, kind=P.DATABASE_ERROR)
    assert d["kind"] == "database error"


@patch("odc_core.browser_helpers.take_error_screenshot", return_value="C:/logs/given.png")
def test_existing_screenshot_is_not_retaken(mock_shot):
    d = pf.report_process_error(MagicMock(), CONFIG, supplier_name="X", job_id=1, kind=P.SEARCH_FAILED,
                                page=_Page(), screenshot="C:/logs/mine.png")
    mock_shot.assert_not_called()
    assert d["screenshot"].endswith("mine.png")


# --- collect_errors: automation_core's, but errors before sys.exit() still reach the Control Room ---

import json  # noqa: E402
import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402


def _ctx(tmp_path):
    job_file = tmp_path / "job.json"
    job_file.write_text("{}", encoding="utf-8")
    return SimpleNamespace(job_file=job_file, is_production=False, notification_method="email", process_name="test")


def _sidecar(tmp_path):
    path = tmp_path / "cr_errors.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


@patch("automation_core.errors._maybe_dispatch")
def test_collect_errors_reports_errors_before_sys_exit(mock_dispatch, tmp_path):
    with pytest.raises(SystemExit) as raised:
        with pf.collect_errors(_ctx(tmp_path)) as errors:
            errors.add("Drax login failed for job 7 (credentials rejected)", details={"kind": "credentials rejected"})
            sys.exit(1)
    assert raised.value.code == 1
    side = _sidecar(tmp_path)
    assert side["is_critical"] is False
    assert side["error_count"] == 1
    assert side["errors"][0]["details"]["kind"] == "credentials rejected"
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.kwargs["is_critical"] is False


@patch("automation_core.errors._maybe_dispatch")
def test_collect_errors_sys_exit_without_errors_is_unchanged(mock_dispatch, tmp_path):
    with pytest.raises(SystemExit) as raised:
        with pf.collect_errors(_ctx(tmp_path)):
            sys.exit(1)
    assert raised.value.code == 1
    assert _sidecar(tmp_path) is None
    mock_dispatch.assert_not_called()


@patch("automation_core.errors._maybe_dispatch")
def test_collect_errors_keeps_the_same_exit_code_and_object(mock_dispatch, tmp_path):
    exit_exc = SystemExit("stopping")
    with pytest.raises(SystemExit) as raised:
        with pf.collect_errors(_ctx(tmp_path)) as errors:
            errors.add("something failed")
            raise exit_exc
    assert raised.value is exit_exc
    assert _sidecar(tmp_path)["error_count"] == 1


@patch("automation_core.errors._maybe_dispatch")
def test_collect_errors_exception_still_critical(mock_dispatch, tmp_path):
    with pytest.raises(RuntimeError):
        with pf.collect_errors(_ctx(tmp_path)) as errors:
            errors.add("first")
            raise RuntimeError("boom")
    side = _sidecar(tmp_path)
    assert side["is_critical"] is True
    assert "RuntimeError: boom" in side["traceback"]
    assert mock_dispatch.call_args.kwargs["is_critical"] is True


@patch("automation_core.errors._maybe_dispatch")
def test_collect_errors_clean_exit_unchanged(mock_dispatch, tmp_path):
    with pf.collect_errors(_ctx(tmp_path)) as errors:
        errors.add("one")
    assert _sidecar(tmp_path)["is_critical"] is False
    mock_dispatch.assert_called_once()
