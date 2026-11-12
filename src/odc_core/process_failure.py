"""One way for every supplier bot to report a failure that is not a login (0.14.2).

``login_failure`` gives the Control Room a specific kind, the portal's own
message, the page URL and a screenshot for a failed login. Everything else a
bot runs into - the portal going down mid-run, the session expiring, a menu
or page that never loads, a search or download that fails, a file that will
not save, a database error, an unhandled exception - used to reach the
Control Room as a bare ``errors.add("Search failed for account X")``.

``report_process_error()`` gives those the same level of detail:

1. Takes a screenshot of the page as it is now (unless one was taken
   already), named after the kind, so there is a picture of every kind of
   error before the bot moves on or ends.
2. Reads the portal's visible error/alert text and the page URL
   (``login_failure.read_portal_messages``), and raises ``OTHER`` /
   ``NAVIGATION_FAILED`` to ``PORTAL_UNAVAILABLE`` or ``SESSION_EXPIRED``
   when the page says so.
3. Logs one ``PROCESS_ERROR`` ERROR line in a fixed format.
4. ``errors.add()``s it with a ``details`` block (``source:
   odc_process_error``, ``category: other`` - inse-toolkit acts on no
   process errors - kind, description, account, portal message, page URL,
   screenshot, exception type) unless ``team_alert=False``.

Like ``login_failure``, it never raises and never classifies exception text.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator

from file_allocation_core.unc import to_unc

from . import browser_helpers
from .login_failure import LoginFailureKind, _add_error, _clip, _int_or_none, classify_portal_message, read_portal_messages

logger = logging.getLogger(__name__)


class ProcessErrorKind(StrEnum):
    """What went wrong after (or around) the login."""

    #: The portal stopped responding or showed a maintenance/unavailable page mid-run.
    PORTAL_UNAVAILABLE = "portal unavailable"
    #: The portal logged the bot out or said the session expired mid-run.
    SESSION_EXPIRED = "session expired"
    #: A page, menu or tab the bot navigates to never loaded.
    NAVIGATION_FAILED = "navigation failed"
    #: The account search could not be run or its results never loaded
    #: (not "account not found": that is the NOT FOUND status, a business fact).
    SEARCH_FAILED = "search failed"
    #: The bill list/table for an account could not be read.
    DOCUMENT_LIST_FAILED = "document list failed"
    #: A document download failed, timed out or produced an empty file.
    DOWNLOAD_FAILED = "download failed"
    #: The downloaded file could not be saved, allocated or uploaded.
    FILE_SAVE_FAILED = "file save failed"
    #: A database read or status update failed.
    DATABASE_ERROR = "database error"
    #: Job set-up is wrong: no credentials, invalid username, missing config.
    CONFIGURATION_ERROR = "configuration error"
    #: The browser or page crashed or was closed.
    BROWSER_CRASHED = "browser crashed"
    #: An exception nothing more specific caught.
    UNHANDLED_EXCEPTION = "unhandled exception"
    OTHER = "other"


PROCESS_KIND_DESCRIPTIONS: dict[ProcessErrorKind, str] = {
    ProcessErrorKind.PORTAL_UNAVAILABLE:
        "Portal unavailable - the portal stopped responding or reported it is unavailable during the run",
    ProcessErrorKind.SESSION_EXPIRED:
        "Session expired - the portal logged the bot out or ended the session during the run",
    ProcessErrorKind.NAVIGATION_FAILED:
        "Navigation failed - a page, menu or tab the bot needs never loaded",
    ProcessErrorKind.SEARCH_FAILED:
        "Search failed - the account search could not be run or its results never loaded",
    ProcessErrorKind.DOCUMENT_LIST_FAILED:
        "Document list failed - the bills/invoices list for the account could not be read",
    ProcessErrorKind.DOWNLOAD_FAILED:
        "Download failed - a document download failed, timed out or was empty",
    ProcessErrorKind.FILE_SAVE_FAILED:
        "File save failed - the downloaded file could not be saved, allocated or uploaded",
    ProcessErrorKind.DATABASE_ERROR:
        "Database error - a database read or status update failed",
    ProcessErrorKind.CONFIGURATION_ERROR:
        "Configuration error - the job or its credentials/config are not set up correctly",
    ProcessErrorKind.BROWSER_CRASHED:
        "Browser crashed - the browser or page crashed or was closed",
    ProcessErrorKind.UNHANDLED_EXCEPTION:
        "Unhandled exception - an unexpected error ended this step; see detail and exception",
    ProcessErrorKind.OTHER:
        "Unclassified process error - see detail",
}

_SESSION_RE = re.compile(
    r"session (has )?(expired|timed out|ended)|you have been (logged|signed) out"
    r"|(log|sign) ?in again|your session is no longer valid",
    re.I,
)
# Kinds the page's own message may sharpen; a specific kind the bot chose is kept.
_SHARPENABLE = frozenset({ProcessErrorKind.OTHER, ProcessErrorKind.NAVIGATION_FAILED,
                          ProcessErrorKind.UNHANDLED_EXCEPTION, ProcessErrorKind.SEARCH_FAILED,
                          ProcessErrorKind.DOCUMENT_LIST_FAILED})
_BROWSER_GONE_RE = re.compile(r"target (page, context or browser )?(has been )?closed|browser has been closed", re.I)


def as_process_kind(value: ProcessErrorKind | str | None) -> ProcessErrorKind:
    if isinstance(value, ProcessErrorKind):
        return value
    try:
        return ProcessErrorKind(value) if value else ProcessErrorKind.OTHER
    except ValueError:
        return ProcessErrorKind.OTHER


def describe_process_error(kind: ProcessErrorKind | str | None) -> str:
    return PROCESS_KIND_DESCRIPTIONS[as_process_kind(kind)]


def _slug(text: str, length: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")[:length] or "error"


def _page_open(page: Any) -> bool:
    if page is None:
        return False
    try:
        closed = page.is_closed()
        return not closed
    except Exception:
        return True  # duck-typed page without is_closed(); try anyway


def screenshot_for(page: Any, config: dict, kind: ProcessErrorKind | LoginFailureKind | str, label: str | None = None) -> Path | None:
    """Screenshot ``page`` named ``<kind>[_<label>]``. None if the page is gone. Never raises."""
    if not _page_open(page):
        return None
    name = _slug(str(kind)) + (f"_{_slug(label, 40)}" if label else "")
    return browser_helpers.take_error_screenshot(page, name, config)


def process_error_details(
    *,
    supplier_name: str,
    job_id: Any,
    kind: ProcessErrorKind | str | None,
    detail: str | None = None,
    account_ref: str | None = None,
    job_details_id: Any = None,
    portal_message: str | None = None,
    page_url: str | None = None,
    screenshot: str | Path | None = None,
    exception: BaseException | None = None,
    process_id: Any = None,
    username: str | None = None,
) -> dict:
    """The ``details`` block for a process error. Never the password."""
    kind = as_process_kind(kind)
    job = _int_or_none(job_id)
    return {
        "source": "odc_process_error",
        "category": "other",
        "kind": str(kind),
        "kind_description": describe_process_error(kind),
        "odc_job_id": job if job is not None else (str(job_id) if job_id is not None else None),
        "job_details_id": _int_or_none(job_details_id),
        "supplier": supplier_name,
        "account_ref": _clip(account_ref, 100),
        "username": _clip(username, 100),
        "portal_message": _clip(portal_message, 1000),
        "page_url": _clip(page_url, 500),
        "screenshot": _clip(to_unc(str(screenshot)), 500) if screenshot else None,
        "exception_type": type(exception).__name__ if exception is not None else None,
        "process_id": _int_or_none(process_id),
        "detail": _clip(detail, 1000),
    }


def report_process_error(
    errors: Any,
    config: dict,
    *,
    supplier_name: str,
    job_id: Any,
    kind: ProcessErrorKind | str | None,
    detail: str | None = None,
    page: Any = None,
    account_ref: str | None = None,
    job_details_id: Any = None,
    exception: BaseException | None = None,
    screenshot: str | Path | None = None,
    screenshot_label: str | None = None,
    process_id: Any = None,
    username: str | None = None,
    team_alert: bool = True,
    extra_selectors: tuple[str, ...] | list[str] = (),
) -> dict:
    """Screenshot, diagnose, log and report one non-login failure. Returns
    the details dict (with the final kind and screenshot path). Never raises.

    ``page`` is the page the error happened on, still open; without it (or
    once it is closed) there is no screenshot or portal message, and the
    rest is still logged and reported. ``screenshot`` skips taking a new one
    when the bot already took it. ``team_alert=False`` logs only (for an
    error the bot recovers from but wants on record).
    """
    kind = as_process_kind(kind)
    portal_message: str | None = None
    page_url: str | None = None
    try:
        if exception is not None and _BROWSER_GONE_RE.search(str(exception)):
            # Only the browser-closed wording is matched, to pick the kind; it
            # is Playwright's own message, not portal text.
            kind = ProcessErrorKind.BROWSER_CRASHED
        if _page_open(page):
            try:
                page_url = str(page.url) or None
            except Exception:
                page_url = None
            messages = read_portal_messages(page, extra_selectors)
            portal_message = _clip(" | ".join(messages), 1000)
            if kind in _SHARPENABLE:
                for text in messages:
                    if _SESSION_RE.search(text):
                        kind = ProcessErrorKind.SESSION_EXPIRED
                        break
                    if classify_portal_message(text) == LoginFailureKind.PORTAL_UNAVAILABLE:
                        kind = ProcessErrorKind.PORTAL_UNAVAILABLE
                        break
            if screenshot is None:
                screenshot = screenshot_for(page, config, kind, screenshot_label or account_ref)
    except Exception:
        logger.debug("PROCESS_ERROR - could not read the page for job %s", job_id, exc_info=True)

    text = (detail or "").strip() or "(no detail)"
    exc_text = f"{type(exception).__name__}: {str(exception).splitlines()[0][:300] if str(exception) else ''}" if exception is not None else "(none)"
    logger.error(
        "PROCESS_ERROR - %s job %s | kind=%s | %s | account=%s | job_details_id=%s | portal_message=%s"
        " | page_url=%s | screenshot=%s | exception=%s | detail=%s",
        supplier_name, job_id, kind, describe_process_error(kind), account_ref or "(none)",
        job_details_id if job_details_id is not None else "(none)",
        f"'{portal_message}'" if portal_message else "(none shown)", page_url or "(unknown)",
        screenshot or "(none)", exc_text, text,
    )
    details = process_error_details(
        supplier_name=supplier_name, job_id=job_id, kind=kind, detail=detail,
        account_ref=account_ref, job_details_id=job_details_id, portal_message=portal_message,
        page_url=page_url, screenshot=screenshot, exception=exception, process_id=process_id,
        username=username,
    )
    if team_alert:
        where = f" account {account_ref}" if account_ref else ""
        message = f"{supplier_name} {kind} on job {job_id}{where}: {describe_process_error(kind)}. Detail: {text}"
        if portal_message:
            message += f" | Portal said: '{_clip(portal_message, 300)}'"
        if page_url:
            message += f" | Page: {_clip(page_url, 300)}"
        try:
            _add_error(errors, message, details=details, exception=exception)
        except Exception:
            logger.exception("PROCESS_ERROR - could not report the error for job %s", job_id)
    return details


@contextmanager
def collect_errors(ctx: Any) -> Iterator[Any]:
    """``automation_core.collect_errors`` that survives ``sys.exit()`` (0.14.2).

    Use it exactly like automation_core's::

        from odc_core.process_failure import collect_errors

        with collect_errors(ctx) as errors:
            ...
            login_failure.handle_login_failure(errors, ...)
            sys.exit(1)

    automation_core's ``collect_errors`` only writes the Control Room's
    ``cr_errors.json`` and sends the summary when its block ends normally or
    with an ``Exception``. ``SystemExit`` is neither, so every error a bot
    reported just before ``sys.exit(1)`` - a failed login, all credentials
    failed - never reached the Control Room. This wrapper catches the
    ``SystemExit`` inside automation_core's block, lets that block end
    normally (so the errors are written and sent as usual), then re-raises the
    same ``SystemExit`` so the exit code is unchanged. Any other exception
    passes straight through to automation_core as before, and an exit with no
    errors collected behaves exactly as it did.
    """
    from automation_core import collect_errors as _core_collect_errors  # noqa: PLC0415

    pending_exit: SystemExit | None = None
    with _core_collect_errors(ctx) as errors:
        try:
            yield errors
        except SystemExit as exc:
            pending_exit = exc
            if errors.has_errors:
                logger.info(
                    "Script exiting with code %r after %d reported error(s); "
                    "reporting them to the Control Room first",
                    exc.code, errors.count,
                )
    if pending_exit is not None:
        raise pending_exit
