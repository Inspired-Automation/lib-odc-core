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

import ctypes
import logging
import os
import socket
import subprocess
import time
import winreg
from collections.abc import Callable
from typing import Any

from . import browser_helpers, jobstodo

logger = logging.getLogger(__name__)


def get_current_hostname() -> str:
    """Return this run node's own hostname, for the `rdp_host` argument.

    Convenience for building the `rdp_host` argument to
    `wait_for_human_login()`/`jobstodo.set_human_wait()`: the toolkit's VNC
    session needs to reach whichever machine is actually running this
    process, wherever it was deployed - a config value would just be one
    more thing to keep in sync with reality, and would drift the moment
    machines are assigned dynamically per run rather than fixed (see
    `rdp_host`'s own note in lib-odc-core-spec.md §4.2). Thin wrapper
    around `socket.gethostname()`.

    Added in 0.8.7, when this mechanism was briefly built around RDP rather
    than VNC (see `jobstodo`'s module-level comment and the 0.8.8 Change Log
    entry) - kept unchanged by the 0.8.8 revert back to VNC, since a
    hostname is exactly what a VNC session needs too, and this function's
    own job (read `socket.gethostname()` instead of trusting a static,
    driftable config value) never depended on which transport it fed.
    """
    return socket.gethostname()


def get_current_session_id() -> int:
    """Return this run node's own Windows session id, for the `session_id`
    argument (written to the `rdp_sessionID` column - see `jobstodo`'s
    module-level comment).

    The toolkit builds the VNC join URL for a job's RDS host from
    `5900 + session_id`, since a single host can run multiple sessions - it
    has no way to know which one this bot's browser is actually running in
    unless the bot tells it. Reads it live from the OS via
    `kernel32.ProcessIdToSessionId()` (this process's own pid), the same
    reasoning as `get_current_hostname()`/the pre-0.8.8
    `get_current_windows_username()`: a config value would just be one more
    thing to keep in sync, and would drift the moment a run lands in a
    different session across runs (which it can - session ids are assigned
    per logon, not fixed per machine).

    `ctypes` rather than `pywin32`/`win32ts.ProcessIdToSessionId()` - this
    is a single stdlib call, not worth a new dependency for.

    Raises `OSError` if the OS call itself fails (`ProcessIdToSessionId`
    returns 0) - not expected on a real Windows run node, but this function
    makes no attempt to guess a default if it happens.
    """
    session_id = ctypes.c_ulong()
    pid = os.getpid()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(ctypes.c_ulong(pid), ctypes.byref(session_id)):
        raise OSError(f"ProcessIdToSessionId failed for pid {pid}")
    return session_id.value


#: Base VNC port - the toolkit's own join-URL convention is 5900 + session_id,
#: since a single machine can run more than one Windows session (and so more
#: than one tvnserver instance) at once. See `jobstodo`'s module-level
#: comment on `rdp_sessionID`.
VNC_PORT_BASE = 5900

#: Default budget for start_vnc_server()'s post-launch port poll.
VNC_PORT_POLL_TIMEOUT_S = 10.0

#: Seconds between port-open attempts while polling.
VNC_PORT_POLL_INTERVAL_S = 0.5

#: App Paths registry key TightVNC's own installer registers - the same
#: place Explorer/`start`/ShellExecute resolve a bare "tvnserver" from.
#: `subprocess.Popen`, unlike those, does NOT consult this - it only
#: searches the current directory and PATH (confirmed live: a run node
#: with TightVNC installed still raised FileNotFoundError from a bare
#: `Popen(["tvnserver", "-run"])`), so _resolve_tvnserver_path() below
#: reads it directly instead of assuming PATH is enough.
_TVNSERVER_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\tvnserver.exe"

#: Default install locations TightVNC's own installer offers - tried only
#: after the registry and env var lookups below come up empty.
_TVNSERVER_DEFAULT_PATHS = (
    r"C:\Program Files\TightVNC\tvnserver.exe",
    r"C:\Program Files (x86)\TightVNC\tvnserver.exe",
)


def _resolve_tvnserver_path() -> str:
    """Best-effort resolution of tvnserver.exe's actual location on this
    run node.

    A bare `"tvnserver"` only resolves via `subprocess`/`CreateProcess`'s
    own search (cwd, then `PATH`) - it does not consult the Windows "App
    Paths" registry key the way `ShellExecute`/`start`/Explorer would, and
    TightVNC's installer does not add itself to `PATH`. This was found live
    on a run node with TightVNC genuinely installed: `start_vnc_server()`
    raised `FileNotFoundError` from exactly that bare-name `Popen()` call.

    Tries, in order:
      1. the `ODC_TVNSERVER_PATH` environment variable - an explicit
         override for a non-standard install location, same pattern as
         `ODC_RDP_PASSWORD`'s env-var precedent elsewhere in this module's
         history (a secret/path scoped to one machine, not team-wide
         config).
      2. the same App Paths registry key (`_TVNSERVER_APP_PATHS_KEY`)
         Explorer/`start` would use - the mechanism TightVNC's installer is
         *documented* to register itself under.
      3. `_TVNSERVER_DEFAULT_PATHS`, TightVNC's own default install
         locations.

    Step 3 is not just a defensive fallback for "the registry key is
    missing for some reason": confirmed live on a real dev machine with
    TightVNC genuinely installed that the App Paths key was simply absent
    (`winreg.OpenKey()` raised `FileNotFoundError`) while the default
    install path resolved it correctly - so this step is doing real, load-
    bearing work, not covering a hypothetical edge case.

    Falls back to the bare `"tvnserver"` string if none of these resolve
    to a real file - `subprocess.Popen()` then raises `FileNotFoundError`
    exactly as before this existed, so a genuinely-not-installed node's
    behaviour is unchanged.
    """
    env_path = os.environ.get("ODC_TVNSERVER_PATH", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _TVNSERVER_APP_PATHS_KEY) as key:
            registry_path, _ = winreg.QueryValueEx(key, "")
        if registry_path and os.path.isfile(registry_path):
            return registry_path
    except OSError:
        pass

    for candidate in _TVNSERVER_DEFAULT_PATHS:
        if os.path.isfile(candidate):
            return candidate

    return "tvnserver"


def start_vnc_server(session_id: int, timeout_s: float = VNC_PORT_POLL_TIMEOUT_S) -> bool:
    """Launch the TightVNC server for this session (`tvnserver -run`), then
    poll its VNC port until it actually accepts a connection or timeout_s
    elapses.

    Call this before launching the browser for a human_in_loop=1 supplier -
    the toolkit's join URL (`rdp_host`/`rdp_sessionID`, see `jobstodo`'s
    module-level comment) is useless if nothing is actually listening on
    that session's VNC port (`VNC_PORT_BASE + session_id`) yet.

    `-run` starts tvnserver attached to this interactive session (as
    opposed to `-install`, which registers it as a machine-wide service) -
    the right mode here, since a VNC server needs to run inside the same
    desktop session as the browser it's meant to expose, and this bot
    already assumes an interactive session for that same reason (see
    `main.py`'s own launch-time log message).

    The executable itself is resolved via `_resolve_tvnserver_path()`, not
    a bare `"tvnserver"` string - `subprocess.Popen()` only searches the
    current directory and `PATH`, not the Windows "App Paths" registry key
    TightVNC's installer actually registers itself under, which is why a
    bare name reliably raised `FileNotFoundError` on a run node with
    TightVNC genuinely installed (see that function's docstring for the
    full resolution order).

    Launching itself is fire-and-forget: `Popen`, not `run`, since
    `tvnserver -run` is a long-lived process that keeps serving VNC
    connections for the rest of this session - waiting for it to exit (what
    `run()` does) would hang here indefinitely. What this function does
    wait for, briefly, is the port actually coming up: `-run` returns
    control to this process before tvnserver has necessarily finished
    initialising and started listening, so polling closes that gap rather
    than assuming the port is immediately ready the instant `Popen()`
    returns.

    Best-effort and non-fatal throughout, like every other infra-readiness
    step in this flow (e.g. `browser_helpers`'s own screenshot/banner
    failures, or a `set_human_wait()` DB write failing): a launch failure
    (TightVNC not installed, `tvnserver` not resolvable) or a port that
    never opens within timeout_s is logged, never raised, so the browser
    launch still proceeds - the human-assisted wait will simply have no
    VNC session for a human to join yet.

    Returns True if the port was confirmed open within timeout_s, False
    otherwise (launch failure, or the port simply never came up in time) -
    a caller may use this to log more loudly, but should not treat False as
    fatal.
    """
    port = VNC_PORT_BASE + session_id
    tvnserver_path = _resolve_tvnserver_path()

    try:
        subprocess.Popen([tvnserver_path, "-run"])
    except Exception:
        logger.exception(
            "HUMAN_IN_LOOP - could not start tvnserver (resolved path: %r) - "
            "continuing without it (the human-assisted wait will have no VNC "
            "session to join)",
            tvnserver_path,
        )
        return False

    logger.info(
        "HUMAN_IN_LOOP - started tvnserver -run for this session, waiting up to "
        "%ss for port %d to open",
        timeout_s, port,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=VNC_PORT_POLL_INTERVAL_S):
                logger.info("HUMAN_IN_LOOP - VNC port %d is open", port)
                return True
        except OSError:
            time.sleep(VNC_PORT_POLL_INTERVAL_S)

    logger.warning(
        "HUMAN_IN_LOOP - VNC port %d did not open within %ss of starting tvnserver - "
        "continuing without a confirmed VNC session",
        port, timeout_s,
    )
    return False


def prepare_vnc_session(
    human_in_loop_flag: object,
    timeout_s: float = VNC_PORT_POLL_TIMEOUT_S,
) -> tuple[str, int] | None:
    """Generic pre-browser-launch step for any human_in_loop=1 supplier -
    the one call a supplier project needs instead of hand-rolling the
    `human_in_loop` check and the `get_current_hostname()`/
    `get_current_session_id()`/`start_vnc_server()` sequence itself.

    `human_in_loop_flag` is the caller's job row's own `human_in_loop`
    value (e.g. `row["human_in_loop"]`, from `jobstodo.get_job_details()` -
    see that function's docstring and spec §4.3) - a nullable `tinyint` as
    it comes out of `ODC_suppliers`, so this accepts anything truthy/falsy
    rather than requiring a `bool` specifically.

    Falsy: returns `None` immediately - this job's supplier doesn't need a
    human-assisted session, so there's nothing to prepare.

    Truthy: resolves this run node's own `rdp_host`/`session_id`
    (`get_current_hostname()`/`get_current_session_id()`) and starts the
    VNC server for this session (`start_vnc_server()`, which polls its own
    port before returning), then returns `(rdp_host, session_id)` - hold
    onto both and pass them to `wait_for_human_login()` later, once the
    caller's own browser has actually launched.

    **Must be called before launching the browser** - that ordering is the
    entire point: the VNC session needs to be confirmed listening first,
    not started in a race with (or after) the browser.
    """
    if not human_in_loop_flag:
        return None

    rdp_host = get_current_hostname()
    session_id = get_current_session_id()
    start_vnc_server(session_id, timeout_s=timeout_s)
    return rdp_host, session_id


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
#: disconnect the VNC session before the signal disappears entirely.
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
    timeout_s: int,
    rdp_host: str,
    session_id: int,
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

    0.8.8: dropped the `rdp_username`/`rdp_password` parameters this
    function briefly took (0.8.1-0.8.7) - this mechanism reverted from RDP
    back to VNC, which needs only `rdp_host` to connect. See `jobstodo`'s
    module-level comment and the 0.8.8 Change Log entry for why.

    0.8.9: added `session_id` - the toolkit builds a job's VNC join URL as
    `5900 + session_id`, since a single `rdp_host` can run more than one
    Windows session at once and has no other way to tell which one this
    bot's browser is actually in. See `human_in_loop.get_current_session_id()`
    and `jobstodo`'s module-level comment for where this is stored.

    Sequence:
      1. jobstodo.set_human_wait() - writes human_wait_status=PENDING_HUMAN
         plus the VNC host and session id, the cue for a poller's toolkit
         to open the VNC session.
      2. Injects the "I'm logged in - continue automation" button and the
         on-page banner, then polls every POLL_INTERVAL_S (up to timeout_s)
         until the button is clicked - the sole trigger for success. See
         _poll_until_logged_in()'s docstring for why `is_logged_in(page)` is
         diagnostic-only here rather than an equal, independent trigger.
      3. On success: shows a "taking over" banner, clears the confirm
         button, calls jobstodo.set_human_wait_complete() (writes
         human_wait_status=COMPLETE - the cue for the toolkit to disconnect
         - and touches nothing else: rdp_host/session_id are left exactly as
         set_human_wait() wrote them), then sleeps TAKEOVER_PAUSE_S before
         returning True, giving the human a moment to read the banner and
         stop interacting. jobstodo.clear_human_wait() is deliberately NOT
         called on this path (see step 4), so a poller can observe COMPLETE
         - and the VNC host/session a completed job used - for as long as
         it needs rather than racing a fixed window.
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
        jobstodo.set_human_wait(job_id, timeout_s, rdp_host, session_id, tables, dsn)
    except Exception:
        logger.exception(
            "HUMAN_IN_LOOP - could not set human_wait_status for job %s; continuing "
            "without the Control Room/VNC signal", job_id,
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
