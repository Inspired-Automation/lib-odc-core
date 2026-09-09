"""Generic human-assisted-login mechanism for ODC_suppliers.human_in_loop
suppliers: an on-page banner, an injected "I'm logged in" confirm button,
and the full ODC_jobs signal lifecycle (jobstodo.set_human_wait() /
set_human_wait_complete() / clear_human_wait()) wrapped around a poll loop.

Generalised out of automation-odc-energia's Phase 3 (the first, and so far
only, human_in_loop=1 supplier - see jobstodo.get_job_details()'s
human_in_loop column, joined from ODC_suppliers). Detecting that a human has
actually finished logging in is inherently supplier-specific - each portal
has its own post-login marker - so that stays the caller's responsibility,
supplied as `is_logged_in`. Everything else here (the banner, the button,
the poll loop, and the DB signal around it) is supplier-agnostic. See
lib-odc-core-spec.md §4.2/§4.3.
"""

from __future__ import annotations

import getpass
import logging
import time
from collections.abc import Callable
from typing import Any

from . import browser_helpers, jobstodo

logger = logging.getLogger(__name__)


def get_current_windows_username() -> str:
    """Return the Windows username the current process is signed in as.

    Convenience for building the `rdp_username` argument to
    `wait_for_human_login()`/`jobstodo.set_human_wait()`: the RDS machine's
    human-assisted RDP session needs to connect as the same Windows account
    the bot's own browser session is already running under, so a poller's
    toolkit reaches the actual desktop the bot is driving rather than a
    different session. A per-supplier config value can drift from the
    actual machine's account, especially once RDS machines are assigned
    dynamically per run rather than fixed - this reads it directly from the
    OS instead. Thin wrapper around `getpass.getuser()`, which raises
    `OSError` if no username can be determined - not expected in this
    setup, since the wait already requires an interactive desktop session
    (see `automation-odc-energia`'s own `main.py` comment on that).
    """
    return getpass.getuser()

#: Seconds between poll ticks while waiting for the human.
POLL_INTERVAL_S = 1.5

#: Seconds between heartbeat log lines while waiting, so a stalled wait is
#: visible in the log rather than indistinguishable from "still waiting
#: normally" until the final timeout.
HEARTBEAT_INTERVAL_S = 20

#: Pause after the wait ends successfully, before this function returns.
#: Doubles as the human's moment to read the "taking over" banner and stop
#: interacting, and as the window during which human_wait_status reads
#: COMPLETE (see jobstodo.set_human_wait_complete()) before clear_human_wait()
#: nulls it - long enough for a poller's toolkit to observe COMPLETE and
#: disconnect the RDP session before the signal disappears entirely.
TAKEOVER_PAUSE_S = 5

_BANNER_COLOR_STARTED = "#2980b9"   # blue - informational, process beginning
_BANNER_COLOR_TAKEOVER = "#c0392b"  # red - stop interacting, bot has control
_BANNER_COLOR_TIMEOUT = "#e67e22"   # amber - warning, no human response

#: Default "started" banner text - generic across every human_in_loop
#: supplier. A caller with a more specific instruction to give the human
#: (e.g. "resolve the reCAPTCHA challenge") can override it via
#: wait_for_human_login()'s started_message argument rather than this
#: library guessing at portal-specific wording.
DEFAULT_STARTED_MESSAGE = (
    "Automation started - please log in, then click 'I'm logged in' (bottom right)"
)

# Fixed-position, max z-index, reuses one self-identifying element so a
# later call replaces the message/color instead of stacking banners.
_BANNER_JS = """
(args) => {
    let banner = document.getElementById('odc-human-loop-banner');
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'odc-human-loop-banner';
        banner.style.cssText = [
            'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
            'color:#ffffff', 'font-family:sans-serif', 'font-size:16px',
            'font-weight:bold', 'text-align:center', 'padding:10px',
            'box-shadow:0 2px 6px rgba(0,0,0,0.4)',
        ].join(';');
        document.body.prepend(banner);
    }
    banner.textContent = args.message;
    banner.style.background = args.color;
}
"""

# Explicit "I'm logged in" button so the human has a way to end the wait
# immediately rather than relying solely on the caller's is_logged_in()
# check. Installed via page.add_init_script() (so it reappears on whatever
# page follows the login submit) and evaluated once immediately for the
# document already loaded when it's installed (add_init_script only covers
# documents created after it is called). Self-identifying element id so a
# re-run of the script is a no-op.
#
# page.add_init_script() runs in every frame, not just the top-level page -
# the window.top guard keeps the button off any iframe (e.g. a login
# challenge widget), so there is exactly one, on the top document only.
#
# Deliberately does NOT use page.expose_function()/a Python callback - a
# plain flag read back via page.evaluate() each poll tick has no async
# round-trip to desync from the page's own navigation. See
# automation-odc-energia's CLAUDE.md Change Log for the live-run failure
# mode (a stuck "Confirming..." button) that ruled out expose_function().
#
# The flag itself is a DOM attribute (btn.dataset.confirmed), not a
# window.* global - found necessary in 0.8.3 after live testing against
# automation-odc-energia (which drives the browser via patchright, a
# stealth-patched Playwright fork used specifically to avoid tripping this
# portal's reCAPTCHA bot detection). Stealth patches like this commonly
# route injected scripts through an isolated JS world specifically to hide
# automation fingerprints from page-level detection scripts: the DOM is
# shared across worlds (so the button rendered and its click handler ran,
# updating its own textContent - visible on screen), but window.* globals
# are NOT shared across worlds, so a flag set there by the click handler
# was invisible to page.evaluate() reading it back, and the wait never
# ended even after a genuine click. DOM state has no such isolation - it is
# the one thing guaranteed consistent regardless of which world touches it.
_LOGIN_CONFIRM_BUTTON_JS = """
(() => {
    if (window.top !== window.self) { return; }
    // sessionStorage persists across navigation within this origin, which
    // is what makes _clear_login_confirm_button() effective at all:
    // add_init_script() itself cannot be un-registered, so without this
    // check the button would keep reappearing on every page for the rest
    // of the run, well past the point it stopped meaning anything.
    if (sessionStorage.getItem('odcHumanLoopTakenOver')) { return; }
    function addButton() {
        if (document.getElementById('odc-human-loop-confirm-btn')) { return; }
        if (!document.body) { requestAnimationFrame(addButton); return; }
        const btn = document.createElement('button');
        btn.id = 'odc-human-loop-confirm-btn';
        btn.dataset.confirmed = 'false';
        btn.textContent = "I'm logged in - continue automation";
        btn.style.cssText = [
            'position:fixed', 'bottom:20px', 'right:20px', 'z-index:2147483647',
            'background:#27ae60', 'color:#ffffff', 'border:none', 'border-radius:4px',
            'padding:12px 20px', 'font-family:sans-serif', 'font-size:14px',
            'font-weight:bold', 'cursor:pointer', 'box-shadow:0 2px 6px rgba(0,0,0,0.4)',
        ].join(';');
        btn.addEventListener('click', () => {
            btn.disabled = true;
            btn.textContent = 'Confirmed - automation taking over...';
            btn.dataset.confirmed = 'true';
        });
        document.body.appendChild(btn);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', addButton);
    } else {
        addButton();
    }
})();
"""


def _show_banner(page: Any, message: str, color: str) -> None:
    """Show or update the on-page banner so a human watching over the RDP
    session gets a visible signal at each stage of the login handoff.

    Best-effort - a failed injection (e.g. mid-navigation) must never block
    or fail the login flow this is called from, since the banner is purely
    a visual signal, not a functional step.
    """
    try:
        page.evaluate(_BANNER_JS, {"message": message, "color": color})
    except Exception:
        logger.debug("HUMAN_IN_LOOP - could not show banner %r", message, exc_info=True)


def _inject_login_confirm_button(page: Any) -> None:
    """Inject the "I'm logged in" button the human can click to end the wait.

    Best-effort: if injecting the script fails, the button just won't
    appear and the poll loop falls back to is_logged_in(), which still runs
    unconditionally either way.
    """
    try:
        # add_init_script only covers documents created from here on (e.g.
        # the page the login submit navigates to) - evaluate it once more
        # directly so the button also appears on the already-loaded page.
        page.add_init_script(_LOGIN_CONFIRM_BUTTON_JS)
        page.evaluate(_LOGIN_CONFIRM_BUTTON_JS)
    except Exception:
        logger.warning(
            "HUMAN_IN_LOOP - could not inject login-confirm button", exc_info=True,
        )


def _login_confirmed_by_button(page: Any) -> bool:
    """Read the confirm button's flag live - see _LOGIN_CONFIRM_BUTTON_JS.

    Reads a DOM attribute (element.dataset.confirmed), not a window.*
    global - the DOM is the one thing guaranteed shared across isolated JS
    worlds, which some stealth browser-automation patches (e.g. patchright)
    use to hide injected-script fingerprints from the page. A window.*
    global set by the click handler is not reliably visible to a separate
    page.evaluate() call under those patches; see this module's comment
    above _LOGIN_CONFIRM_BUTTON_JS for the live failure this was found from.
    """
    try:
        return bool(page.evaluate(
            "() => document.getElementById('odc-human-loop-confirm-btn')"
            "?.dataset.confirmed === 'true'",
        ))
    except Exception:
        logger.debug(
            "HUMAN_IN_LOOP - could not read login-confirm button state", exc_info=True,
        )
        return False


def _clear_login_confirm_button(page: Any) -> None:
    """Remove the confirm button and stop it reappearing on later pages.

    Called once the bot has genuinely taken over. Best-effort tidy-up, must
    never fail the login flow it's called from.
    """
    try:
        page.evaluate(
            "() => { try { sessionStorage.setItem('odcHumanLoopTakenOver', 'true'); } "
            "catch (e) {} "
            "const btn = document.getElementById('odc-human-loop-confirm-btn'); "
            "if (btn) { btn.remove(); } }",
        )
    except Exception:
        logger.debug("HUMAN_IN_LOOP - could not clear login-confirm button", exc_info=True)


def _poll_until_logged_in(
    page: Any,
    is_logged_in: Callable[[Any], bool],
    timeout_s: int,
    started_message: str,
) -> bool:
    """Show the started banner, inject the confirm button, then poll until
    the button is clicked or timeout_s elapses.

    The button click is the sole trigger for success - is_logged_in(page)
    is called every tick too, but only for diagnostic logging (see the
    heartbeat below), never to end the wait on its own. A URL/DOM-based
    "did the login succeed" check cannot reliably distinguish a genuine
    finished login from an intermediate page in the middle of one (a
    redirect during a reCAPTCHA challenge, for instance), so treating it as
    an equal, independent trigger risks the bot taking over control while
    the human is still mid-task. The explicit button click has no such
    ambiguity.
    """
    _inject_login_confirm_button(page)
    _show_banner(page, started_message, _BANNER_COLOR_STARTED)

    logger.info("HUMAN_IN_LOOP - waiting up to %ss for a human to complete login", timeout_s)
    last_heartbeat = time.monotonic()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        button_confirmed = _login_confirmed_by_button(page)
        logged_in = is_logged_in(page)
        if button_confirmed:
            return True
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
            logger.info(
                "HUMAN_IN_LOOP - still waiting for human login (confirm_button_clicked=%s, "
                "is_logged_in=%s - diagnostic only, does not end the wait)",
                button_confirmed, logged_in,
            )
            last_heartbeat = now
        time.sleep(POLL_INTERVAL_S)

    return False


def wait_for_human_login(
    page: Any,
    is_logged_in: Callable[[Any], bool],
    job_id: str,
    timeout_s: int,
    rdp_host: str,
    rdp_username: str,
    rdp_password: str,
    tables: dict,
    dsn: str,
    config: dict,
    started_message: str = DEFAULT_STARTED_MESSAGE,
) -> bool:
    """Run a human-assisted login: DB signal, on-page banner/button, poll loop.

    Assumes `page` is already sitting on (or navigating to) the login page a
    human needs to complete by hand - this function does not navigate there
    itself, since portal navigation and cookie-banner dismissal are the
    caller's own concern (each supplier's portal differs).

    `started_message` is the banner text shown while waiting (default:
    DEFAULT_STARTED_MESSAGE, generic across every human_in_loop supplier).
    Override it with something specific to what the human actually needs to
    do on this portal - e.g. "Please resolve the reCAPTCHA challenge, then
    click 'I'm logged in'" - rather than this library guessing at
    portal-specific wording. Added as a trailing, defaulted argument so
    existing positional call sites keep working unchanged.

    Sequence:
      1. jobstodo.set_human_wait() - writes human_wait_status=PENDING_HUMAN
         plus the RDP connection details, the cue for a poller's toolkit to
         open the RDP session.
      2. Injects the "I'm logged in - continue automation" button and the
         on-page banner, then polls every POLL_INTERVAL_S (up to timeout_s)
         until the button is clicked - the sole trigger for success. See
         _poll_until_logged_in()'s docstring for why `is_logged_in(page)` is
         diagnostic-only here rather than an equal, independent trigger.
      3. On success: shows a "taking over" banner, clears the confirm
         button, calls jobstodo.set_human_wait_complete() (nulls the RDP
         columns - the cue for the toolkit to disconnect), then sleeps
         TAKEOVER_PAUSE_S before returning True, giving the human a moment
         to read the banner and stop interacting. human_wait_status is left
         at COMPLETE - jobstodo.clear_human_wait() is deliberately NOT
         called on this path (see step 4), so a poller can observe COMPLETE
         for as long as it needs rather than racing a fixed window.
      4. jobstodo.clear_human_wait() runs in a finally, but only fires when
         `success` is False - a genuine failure, a timeout, or an exception
         raised from `is_logged_in()`/the poll loop. Those paths have
         nothing worth keeping, so the row is fully reset to NULL rather
         than left at PENDING_HUMAN.
      5. On timeout instead of success: shows a "no response" banner, logs,
         and takes an error screenshot (browser_helpers.take_error_screenshot),
         then returns False. set_human_wait_complete() is never called on
         this path - a poller must never read COMPLETE for a login that
         didn't actually succeed.

    `is_logged_in(page) -> bool` is the caller's own, portal-specific check
    (e.g. a post-login URL/DOM marker) - this function has no way to know
    what a successful login looks like on any given portal. It does **not**
    end the wait on its own (only the confirm button does - see
    _poll_until_logged_in()); it is called every poll tick and logged at
    each heartbeat purely as a diagnostic, so a supplier team can validate a
    candidate marker's timing against the button click before ever trusting
    it further. It must still be defensive: an exception it raises
    propagates out of this function (past the button/banner logic, though
    clear_human_wait() in the finally still runs) and ends the wait early.
    See automation-odc-energia's `_post_login_reached()` for a reference
    implementation.

    Failures writing the ODC_jobs signal (e.g. the human_wait_status/rdp_*
    columns don't exist yet on this database) are logged and swallowed, not
    fatal to the run - the browser-side wait still proceeds either way. See
    lib-odc-core-spec.md §4.2.
    """
    try:
        jobstodo.set_human_wait(
            job_id, timeout_s, rdp_host, rdp_username, rdp_password, tables, dsn,
        )
    except Exception:
        logger.exception(
            "HUMAN_IN_LOOP - could not set human_wait_status for job %s; continuing "
            "without the Control Room/RDP signal", job_id,
        )

    success = False
    try:
        success = _poll_until_logged_in(page, is_logged_in, timeout_s, started_message)

        if success:
            logger.info(
                "HUMAN_IN_LOOP - human login detected for job %s, pausing %ss before "
                "taking over", job_id, TAKEOVER_PAUSE_S,
            )
            _show_banner(
                page,
                "Automation taking over - please stop interacting with this window",
                _BANNER_COLOR_TAKEOVER,
            )
            _clear_login_confirm_button(page)
            try:
                jobstodo.set_human_wait_complete(job_id, tables, dsn)
            except Exception:
                logger.exception(
                    "HUMAN_IN_LOOP - could not set human_wait_status=COMPLETE for "
                    "job %s", job_id,
                )
            time.sleep(TAKEOVER_PAUSE_S)
            logger.info("HUMAN_IN_LOOP - resuming automated control for job %s", job_id)
        else:
            logger.error(
                "HUMAN_IN_LOOP - no human completed login for job %s within %ss",
                job_id, timeout_s,
            )
            browser_helpers.take_error_screenshot(page, "human_login_timeout", config)
            _show_banner(
                page,
                "No response from a human within the wait window - automation stopping",
                _BANNER_COLOR_TIMEOUT,
            )
    finally:
        # Only a non-success exit (failure, timeout, or an exception from
        # is_logged_in()/the poll loop) clears the row back to NULL. On
        # success, human_wait_status is left at COMPLETE deliberately - see
        # set_human_wait_complete()'s docstring for why it persists rather
        # than being cleared moments later.
        if not success:
            try:
                jobstodo.clear_human_wait(job_id, tables, dsn)
            except Exception:
                logger.exception(
                    "HUMAN_IN_LOOP - could not clear human_wait_status for job %s", job_id,
                )

    return success
