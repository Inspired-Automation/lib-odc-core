"""Generic human-assisted-login mechanism for ODC_suppliers.human_in_loop
suppliers: an on-page banner, an injected "I'm logged in" confirm button,
a poll loop, and a file-based signal to Control Room's own node agent
(`request_assist()` / `release_assist()`), which now owns VNC/websockify
session provisioning entirely - lib-odc-core no longer starts a VNC server
or touches ODC_jobs for any of this (see the 0.8.12 Change Log entry and
automation-odc-energia's docs/lib-odc-core-requirements.md).

Generalised out of automation-odc-energia's Phase 3 (the first, and so far
only, human_in_loop=1 supplier - see jobstodo.get_job_details()'s
human_in_loop column, joined from ODC_suppliers). Detecting that a human has
actually finished logging in is inherently supplier-specific - each portal
has its own post-login marker - so that stays the caller's responsibility,
supplied as `is_logged_in`. Everything else here (the banner, the button,
the poll loop, and the assist file signal around it) is supplier-agnostic.

0.8.12 replaced the previous ODC_jobs.human_wait_status/rdp_host/
rdp_sessionID DB signal, and the tvnserver-launching machinery that used to
sit in this module (get_current_hostname(), get_current_session_id(),
start_vnc_server(), prepare_vnc_session(), and their registry/App-Paths
helpers), with a file-based request/release protocol Control Room's own
node agent watches directly: `request_assist()` (non-blocking, call before
`page.goto()`) writes `assist.request`; `wait_for_human_login()` (unchanged
otherwise: banner, confirm button, poll loop) now calls `release_assist()`
in its own `finally` instead of touching the database. See
automation-odc-energia's docs/lib-odc-core-requirements.md for the full
rationale and the items still unverified against Control Room's own docs
(exact file schema/atomicity, crash-path teardown guarantee).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import browser_helpers

logger = logging.getLogger(__name__)

#: Filenames for the assist.request/assist.release file protocol - Control
#: Room's node agent watches the bot's own per-run job directory (the
#: folder containing the job.json Control Room already told the bot about
#: at launch, e.g. `ctx.job_file.parent` where `ctx = automation_core.setup(...)`
#: resolved it from `--job-file`/`CR_JOB_FILE` - lib-odc-core does not
#: resolve or guess this path itself, see request_assist()'s docstring) for
#: these two files.
#:
#: Best-effort implementation of a protocol flagged as unverified against
#: Control Room's own docs - see automation-odc-energia's
#: docs/lib-odc-core-requirements.md items 1 and 6. Confirm the exact
#: filenames/schema/atomicity expectation before relying on this in
#: production; update here (and bump the version) once confirmed.
ASSIST_REQUEST_FILENAME = "assist.request"
ASSIST_RELEASE_FILENAME = "assist.release"


def _atomic_write(path: Path, content: str) -> None:
    """Write `content` to `path` via a temp-file-then-rename, so a reader
    (Control Room's node agent) never observes a partially-written file.

    The rename (`Path.replace()`) is atomic on the same filesystem - the
    same guarantee `os.replace()` gives (`MoveFileEx` with
    `MOVEFILE_REPLACE_EXISTING` on Windows, a single `rename()` syscall on
    POSIX). This is a best-effort implementation of the atomicity
    requirement flagged as unverified in
    automation-odc-energia's docs/lib-odc-core-requirements.md item 6 - the
    node agent's actual expectation (does it need this at all, or read a
    direct write just fine) has not been confirmed.
    """
    tmp_path = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def request_assist(job_id: str, reason: str, job_dir: Path | str) -> None:
    """Signal Control Room's node agent that this job needs a human-assisted
    login, by writing `assist.request` into `job_dir`. Non-blocking -
    returns immediately once the file is written (or the write fails).

    Split out of the old single `wait_for_human_login()` call (which used
    to both trigger the assist session and poll for login completion) so a
    caller can invoke this *before* `page.goto()` navigates anywhere - the
    node agent's own VNC/websockify provisioning is an unknown, possibly
    non-trivial delay, so triggering it before the browser starts
    navigating lets both overlap instead of happening strictly one after
    the other. `wait_for_human_login()` below is the second half of this
    handoff (unchanged otherwise: banner, confirm button, poll loop) - it
    no longer triggers assist itself, this call already did. See
    automation-odc-energia's docs/lib-odc-core-requirements.md item 1.

    `job_dir` is the same per-run directory Control Room's own agent
    already told this bot about at launch (e.g. `ctx.job_file.parent`,
    where `ctx = automation_core.setup(...)` resolved it from
    `--job-file`/`CR_JOB_FILE`) - lib-odc-core does not know or guess this
    location itself, since Control Room decides it per run and could put
    it anywhere; the caller supplies it explicitly, the same way every
    other function here takes `dsn`/`tables` explicitly rather than
    assuming a fixed location.

    Writes `{"reason": reason}` to a temp file in `job_dir`, then renames
    it into place as `assist.request` (see `_atomic_write()`) - a
    best-effort implementation of a protocol flagged as unverified against
    Control Room's own docs (exact filename/schema/atomicity - see
    docs/lib-odc-core-requirements.md item 6). Confirm before depending on
    this in production.

    Best-effort like every other infra-signal write this mechanism used to
    make to the database: a failure (job_dir doesn't exist, permissions,
    disk full) is logged, never raised - the browser-side flow still
    proceeds either way, just without an assist session for a human to
    join.
    """
    request_path = Path(job_dir) / ASSIST_REQUEST_FILENAME
    try:
        _atomic_write(request_path, json.dumps({"reason": reason}))
        logger.info(
            "HUMAN_IN_LOOP - wrote %s for job %s (reason: %r)",
            request_path, job_id, reason,
        )
    except OSError:
        logger.exception(
            "HUMAN_IN_LOOP - could not write %s for job %s; continuing without "
            "the Control Room assist signal", request_path, job_id,
        )


def release_assist(job_id: str, job_dir: Path | str) -> None:
    """Signal Control Room's node agent that the human-assisted session for
    this job is done, by writing `assist.release` (empty file) into
    `job_dir`.

    Called from `wait_for_human_login()`'s own `finally` block on every
    exit - success, failure, timeout, or an exception raised from
    `is_logged_in()` - the equivalent guarantee the old design got from a
    Python `try`/`finally` around `jobstodo.clear_human_wait()`. Note the
    limit that guarantee always had and still has: it only covers this
    process exiting through its own Python call stack. A hard kill (crash,
    SIGKILL, power loss) before this line runs writes nothing - closing
    that gap needs Control Room's node agent to independently detect a
    dead bot process, which is outside lib-odc-core's own reach. See
    automation-odc-energia's docs/lib-odc-core-requirements.md item 4 -
    confirm which side actually provides that guarantee before relying on
    it in production.

    Best-effort: a failure to write is logged, never raised.
    """
    release_path = Path(job_dir) / ASSIST_RELEASE_FILENAME
    try:
        _atomic_write(release_path, "")
        logger.info("HUMAN_IN_LOOP - wrote %s for job %s", release_path, job_id)
    except OSError:
        logger.exception(
            "HUMAN_IN_LOOP - could not write %s for job %s", release_path, job_id,
        )


#: Seconds between poll ticks while waiting for the human.
POLL_INTERVAL_S = 1.5

#: Seconds between heartbeat log lines while waiting, so a stalled wait is
#: visible in the log rather than indistinguishable from "still waiting
#: normally" until the final timeout.
HEARTBEAT_INTERVAL_S = 20

#: Pause after the wait ends successfully, before this function returns.
#: The human's moment to read the "taking over" banner and stop
#: interacting, while the assist session is still live - release_assist()
#: (in the finally below) only runs after this sleep, so the VNC session
#: isn't torn out from under a human still reading the banner.
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
    """Show or update the on-page banner so a human watching over the VNC
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
    job_dir: Path | str,
    timeout_s: int,
    config: dict,
    started_message: str = DEFAULT_STARTED_MESSAGE,
) -> bool:
    """Run the blocking half of a human-assisted login: on-page banner,
    confirm button, poll loop - then release the assist session on every
    exit path via `release_assist()`.

    Assumes the caller already called `request_assist()` earlier (before
    `page.goto()` - see that function's docstring for why) and that `page`
    is already sitting on (or navigating to) the login page a human needs
    to complete by hand - this function does not navigate there itself,
    since portal navigation and cookie-banner dismissal are the caller's
    own concern (each supplier's portal differs). This function no longer
    triggers assist itself. See automation-odc-energia's
    docs/lib-odc-core-requirements.md item 1 for why the old single call
    was split into these two.

    0.8.12: dropped the `rdp_host`/`session_id`/`tables`/`dsn` parameters
    this function used to take - the ODC_jobs human_wait_status/rdp_host/
    rdp_sessionID DB signal this function used to write via
    `jobstodo.set_human_wait()`/`set_human_wait_complete()`/
    `clear_human_wait()` is gone entirely (inse-toolkit no longer reads
    those columns - it polls Control Room's own job record instead, fed by
    the assist.request/assist.release files `request_assist()`/
    `release_assist()` now write). Added `job_dir`, needed by
    `release_assist()`. See `jobstodo.py`'s module-level comment and
    automation-odc-energia's docs/lib-odc-core-requirements.md item 2.

    `started_message` is the banner text shown while waiting (default:
    DEFAULT_STARTED_MESSAGE, generic across every human_in_loop supplier).
    Override it with something specific to what the human actually needs to
    do on this portal - e.g. "Please resolve the reCAPTCHA challenge, then
    click 'I'm logged in'" - rather than this library guessing at
    portal-specific wording.

    Sequence:
      1. Injects the "I'm logged in - continue automation" button and the
         on-page banner, then polls every POLL_INTERVAL_S (up to timeout_s)
         until the button is clicked - the sole trigger for success. See
         _poll_until_logged_in()'s docstring for why `is_logged_in(page)` is
         diagnostic-only here rather than an equal, independent trigger.
      2. On success: shows a "taking over" banner, clears the confirm
         button, sleeps TAKEOVER_PAUSE_S (giving the human a moment to read
         the banner and stop interacting, while the assist session is
         still live), then returns True.
      3. On timeout instead of success: shows a "no response" banner, logs,
         and takes an error screenshot (browser_helpers.take_error_screenshot),
         then returns False.
      4. `release_assist(job_id, job_dir)` runs in a `finally`, regardless
         of which path was taken above - including an exception propagating
         from `is_logged_in()`/the poll loop. See that function's docstring
         for the limits of this guarantee (it cannot cover a hard kill of
         this process).

    `is_logged_in(page) -> bool` is the caller's own, portal-specific check
    (e.g. a post-login URL/DOM marker) - this function has no way to know
    what a successful login looks like on any given portal. It does **not**
    end the wait on its own (only the confirm button does - see
    _poll_until_logged_in()); it is called every poll tick and logged at
    each heartbeat purely as a diagnostic, so a supplier team can validate a
    candidate marker's timing against the button click before ever trusting
    it further. It must still be defensive: an exception it raises
    propagates out of this function (past the button/banner logic, though
    release_assist() in the finally still runs) and ends the wait early.
    See automation-odc-energia's `_post_login_reached()` for a reference
    implementation.
    """
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
        release_assist(job_id, job_dir)

    return success
