"""One way for every supplier bot to handle a failed portal login.

Before 0.13.7 each bot's ``main.py`` logged and ``errors.add()``-ed its own
message, and only TGP Mygateway sent anything else (a team email for a forced
password change). ``handle_login_failure()`` replaces all of that:

1. Logs one ERROR line in a fixed format.
2. Raises the team alert through ``errors.add()`` (the ``collect_errors``
   pipeline), worded the same for every supplier - unless SystemsLink is
   emailed (step 3) or was within the cooldown, in which case SystemsLink
   owns getting the client to fix it and the team is not alerted.
3. Emails SystemsLink (``support@systems-link.com``), who contact the client
   so the client can fix the login, when **all** of these hold:

   - the failure is a credential problem the client can fix
     (``CLIENT_NOTIFIABLE_KINDS``: credentials rejected, account locked,
     password change required, and from 0.14.0 OTP not received - the
     portal sends the code to the login's registered email address, which
     the client must point at our OTP mailbox). Our-side faults (portal
     down, page errors, human-assist timeout) are ``OTHER`` and go to the
     team only;
   - the client is not Inspired PLC or Ignite
     (``file_allocation_core.routing.is_inspired_client``);
   - this is a production run (``config["env"] == "prod"``). A dev run sends
     to ``login_failure_notification.dev_recipient`` if set, else nothing;
   - the same supplier + login has not been notified within ``cooldown_hours``
     (default 24), so a job that fails on every scheduled run does not
     email SystemsLink every time.

   The email goes from ``noreply_automation@inspiredenergy.co.uk`` and names
   the client, supplier, login, portal and ODC job. It never contains the
   password. Before 0.13.11 it went to the client's login address instead.

Classifying the failure is the supplier's job, because only the supplier
knows which page text is a portal message and which is an exception string.
``classify_portal_message()`` turns a message the portal displayed into a
``LoginFailureKind``, and ``escalate()`` keeps the most specific kind seen
across retries. Never classify exception text: Playwright errors quote the
selector they were waiting on, which can itself read "password invalid".

Control Room triage (0.14.1)
----------------------------
The Control Room's failure triage now decides what a failed run was and hands
inse-toolkit the failures a business owner can fix; the toolkit pauses the
service (or switches off one login of several) and tells the owner. So the
bot's job is to give the Control Room a failure it can triage, every time:

- ``handle_login_failure()`` always calls ``errors.add()`` for a job-level
  failure, including when SystemsLink was emailed (0.13.x stayed silent
  then). The message is unchanged; ``details`` carries the fields triage
  needs: ``category`` (``login_error`` / ``otp_not_received`` /
  ``recaptcha_not_completed`` / ``other``, from ``category_for()``), ``kind``,
  ``odc_job_id``, ``supplier``, ``client``, ``username``, ``portal_url``,
  ``screenshot``, ``multi_credential``, ``process_id`` and ``detail``.
- On a multi-credential job, a single failed login is reported too
  (``team_alert=False`` with an owner-fixable kind), naming the login, so the
  toolkit can switch off just that one. 0.14.0 said nothing until every login
  had failed.
- The bot no longer writes to the database itself. ``report_login_failure()``
  and ``report_login_success()`` (0.14.0) are kept so callers do not break,
  but do nothing: with both the bot and the Control Room reporting, every
  failure would be counted twice. The tables keys ``report_login_failure`` /
  ``report_login_success`` are ignored.
- The SystemsLink email is unchanged and still on by default. inse-toolkit
  emails SystemsLink as the service's owner once it is live for a service, so
  turn this off then (``login_failure_notification.enabled: false``) or they
  are told twice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from file_allocation_core.routing import is_inspired_client
from file_allocation_core.unc import to_unc

from . import graph_client, jobstodo

logger = logging.getLogger(__name__)


class LoginFailureKind(StrEnum):
    """Why a login failed, as far as the bot can tell."""

    CREDENTIALS_REJECTED = "credentials rejected"
    ACCOUNT_LOCKED = "account locked"
    PASSWORD_CHANGE_REQUIRED = "password change required"
    #: 0.14.0. The portal accepted the password and sent a one-time code,
    #: but it never reached our OTP mailbox: the login's registered email
    #: address does not deliver or forward there. Classified by the bot
    #: (it knows when it is waiting for an OTP), never by
    #: classify_portal_message().
    OTP_NOT_RECEIVED = "otp not received"
    #: 0.14.1. A human-assisted login (reCAPTCHA) that nobody completed before
    #: the wait ran out. Not a credential problem: the Control Room sends a
    #: notification, nothing is paused. Set by the bot.
    RECAPTCHA_NOT_COMPLETED = "recaptcha not completed"
    #: 0.14.2. Where the login got to before it failed, for a failure that is
    #: not a credential problem, so the Control Room can tell them apart
    #: instead of seeing "other". All are triage category ``other``.
    #: The login page never loaded: navigation timed out or errored, or the
    #: portal showed a maintenance / service-unavailable message.
    PORTAL_UNAVAILABLE = "portal unavailable"
    #: The login page loaded but the username/password form never appeared
    #: (portal redesigned, interstitial page, bot block).
    LOGIN_FORM_NOT_FOUND = "login form not found"
    #: Credentials were submitted, no error message was shown, but the page
    #: the bot expects after a login (dashboard, account list) never loaded.
    LANDING_PAGE_NOT_REACHED = "landing page not reached"
    #: Credentials were submitted on a portal that asks for a one-time
    #: passcode, but the passcode entry page never appeared.
    OTP_PAGE_NOT_REACHED = "otp page not reached"
    #: A one-time passcode arrived and was entered, but the portal did not
    #: accept it (wrong, expired, already used).
    OTP_REJECTED = "otp rejected"
    OTHER = "other"


#: 0.14.2. One line per kind, for the log and the Control Room, saying what
#: the bot saw. The first words are the label triage groups by.
KIND_DESCRIPTIONS: dict[LoginFailureKind, str] = {
    LoginFailureKind.CREDENTIALS_REJECTED:
        "Invalid credentials - the portal showed an invalid username/email or password message",
    LoginFailureKind.ACCOUNT_LOCKED:
        "Account locked - the portal says the login is locked, suspended or has had too many attempts",
    LoginFailureKind.PASSWORD_CHANGE_REQUIRED:
        "Password change required - the portal is forcing a password change before sign-in",
    LoginFailureKind.OTP_NOT_RECEIVED:
        "OTP email not received - the portal asked for a one-time passcode but none reached the OTP mailbox",
    LoginFailureKind.OTP_PAGE_NOT_REACHED:
        "OTP page not reached - after submitting the login the one-time passcode page never appeared",
    LoginFailureKind.OTP_REJECTED:
        "OTP rejected - the one-time passcode was entered but the portal did not accept it",
    LoginFailureKind.RECAPTCHA_NOT_COMPLETED:
        "reCAPTCHA not completed - the human-assisted login was not completed before the wait ran out",
    LoginFailureKind.LANDING_PAGE_NOT_REACHED:
        "Landing page not reached - the login was submitted with no error shown, but the expected "
        "post-login page never loaded",
    LoginFailureKind.LOGIN_FORM_NOT_FOUND:
        "Login form not found - the login page loaded but the username/password fields never appeared",
    LoginFailureKind.PORTAL_UNAVAILABLE:
        "Portal unavailable - the login page did not load or the portal reported it is unavailable",
    LoginFailureKind.OTHER:
        "Unclassified login failure - see detail",
}


def describe(kind: LoginFailureKind | str | None) -> str:
    """The one-line description of a failure kind (0.14.2)."""
    return KIND_DESCRIPTIONS[_as_kind(kind)]


#: Kinds the client can fix themselves, so the only ones SystemsLink is
#: emailed about (to pass on to the client).
CLIENT_NOTIFIABLE_KINDS = frozenset({
    LoginFailureKind.CREDENTIALS_REJECTED,
    LoginFailureKind.ACCOUNT_LOCKED,
    LoginFailureKind.PASSWORD_CHANGE_REQUIRED,
    LoginFailureKind.OTP_NOT_RECEIVED,
})

#: The Control Room's triage categories (0.14.1). inse-toolkit acts on the
#: first two; the Control Room keeps the rest.
CATEGORY_LOGIN_ERROR = "login_error"
CATEGORY_OTP_NOT_RECEIVED = "otp_not_received"
CATEGORY_RECAPTCHA_NOT_COMPLETED = "recaptcha_not_completed"
CATEGORY_OTHER = "other"

#: Kinds a business owner can put right, so the ones worth reporting login by
#: login on a multi-credential job.
OWNER_FIXABLE_KINDS = frozenset({
    LoginFailureKind.CREDENTIALS_REJECTED,
    LoginFailureKind.ACCOUNT_LOCKED,
    LoginFailureKind.PASSWORD_CHANGE_REQUIRED,
    LoginFailureKind.OTP_NOT_RECEIVED,
})

DEFAULT_SENDER = "noreply_automation@inspiredenergy.co.uk"
DEFAULT_RECIPIENT = "support@systems-link.com"
DEFAULT_COOLDOWN_HOURS = 24
NOTICES_FOLDER = "LOGIN_FAILURE_NOTICES"
_DEFAULT_BASE = r"\\inspiredenergysolutions.local\DFS\Public\!IES\BPI\Automation Team\Automated Processes\ODC"
_ENV_FOLDERS = {"prod": "LIVE", "live": "LIVE", "dev": "DEV"}

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")

# Checked in this order, so a "locked" or "change password" message wins over
# a generic "invalid" one. Phrases, not single words, so ordinary page text
# ("Forgotten your password?") does not match.
_PORTAL_PATTERNS: tuple[tuple[LoginFailureKind, re.Pattern[str]], ...] = (
    (LoginFailureKind.PASSWORD_CHANGE_REQUIRED, re.compile(
        r"password (has )?expired"
        r"|(must|need to|required to|please) (change|reset|update) (your )?password"
        r"|password (change|reset) (is )?required"
        r"|change your password to continue"
        r"|password (must|needs to) be (changed|reset|updated)"
        r"|(set|create|choose) a new password",
        re.I,
    )),
    (LoginFailureKind.ACCOUNT_LOCKED, re.compile(
        r"account (is |has been |was )?(temporarily )?(locked|suspended|disabled|blocked|deactivated)"
        r"|too many (failed |unsuccessful |incorrect )?(login |log in |log-in |sign[ -]?in |password )?attempts"
        r"|locked out",
        re.I,
    )),
    (LoginFailureKind.CREDENTIALS_REJECTED, re.compile(
        r"(username|user name|user id|email( address)?|password|login|log in|sign[ -]?in|credentials|details)"
        r"((( and| or) |/)(password|username|user name|email( address)?))?"
        r" (is |are |was |were |you entered |you have entered |entered )*"
        r"(invalid|incorrect|not recogni[sz]ed|not valid|wrong|not correct|did not match|do not match|don't match)"
        r"|(invalid|incorrect|wrong|unrecogni[sz]ed) (username|user name|user id|email|password|login|credentials|details)"
        r"|(login|log in|sign[ -]?in|authentication) (failed|unsuccessful)"
        r"|(could not|couldn't|unable to) (log|sign) you in"
        r"|(don't|do not|didn't|did not|can't|cannot) recogni[sz]e (the |your |those |these )?"
        r"(details|username|user name|email( address)?|password|login|credentials)"
        r"|not (been )?able to (log|sign) you in",
        re.I,
    )),
    # 0.14.2. Last, so any credential message on the same page wins.
    (LoginFailureKind.OTP_REJECTED, re.compile(
        r"(one[ -]?time )?(pass ?code|verification code|security code|otp|code you entered)"
        r" (is |was |has |have |you entered |entered )*(invalid|incorrect|wrong|expired|not valid|not recogni[sz]ed)"
        r"|(invalid|incorrect|wrong|expired) (one[ -]?time )?(pass ?code|verification code|security code|otp)",
        re.I,
    )),
    (LoginFailureKind.PORTAL_UNAVAILABLE, re.compile(
        r"(service|site|website|portal|system|page) (is )?(temporarily )?(unavailable|down|offline)"
        r"|(scheduled |planned )?maintenance (is )?(in progress|underway|work)"
        r"|(undergoing|under) (scheduled |planned )?maintenance"
        r"|\b50[234] (bad gateway|service unavailable|gateway time-?out)",
        re.I,
    )),
)

# Higher wins in escalate(). The credential kinds are top: once the portal
# has said the password is wrong, nothing later hides it. Below them, the
# further the login got the more specific the failure, so a later retry that
# only hits a slow page cannot hide an earlier "OTP page not reached".
_KIND_RANK = {
    LoginFailureKind.OTHER: 0,
    LoginFailureKind.RECAPTCHA_NOT_COMPLETED: 0,
    LoginFailureKind.PORTAL_UNAVAILABLE: 1,
    LoginFailureKind.LOGIN_FORM_NOT_FOUND: 2,
    LoginFailureKind.LANDING_PAGE_NOT_REACHED: 3,
    LoginFailureKind.OTP_PAGE_NOT_REACHED: 4,
    # Below the password kinds: an OTP is only asked for once the password
    # was accepted, so a "password incorrect" seen on any attempt wins.
    LoginFailureKind.OTP_NOT_RECEIVED: 5,
    LoginFailureKind.OTP_REJECTED: 5,
    LoginFailureKind.CREDENTIALS_REJECTED: 6,
    LoginFailureKind.ACCOUNT_LOCKED: 7,
    LoginFailureKind.PASSWORD_CHANGE_REQUIRED: 8,
}


def category_for(kind: LoginFailureKind | str | None) -> str:
    """The Control Room triage category for a failure kind (0.14.1)."""
    kind = _as_kind(kind)
    if kind == LoginFailureKind.OTP_NOT_RECEIVED:
        return CATEGORY_OTP_NOT_RECEIVED
    if kind == LoginFailureKind.RECAPTCHA_NOT_COMPLETED:
        return CATEGORY_RECAPTCHA_NOT_COMPLETED
    if kind in OWNER_FIXABLE_KINDS:
        return CATEGORY_LOGIN_ERROR
    return CATEGORY_OTHER


def classify_portal_message(text: str | None) -> LoginFailureKind:
    """Map a message the portal showed on its login page to a LoginFailureKind.

    Pass only text read from the page (an error banner, a field message),
    never an exception string. Unrecognised or empty text is ``OTHER``.
    """
    message = (text or "").strip()
    if not message:
        return LoginFailureKind.OTHER
    for kind, pattern in _PORTAL_PATTERNS:
        if pattern.search(message):
            return kind
    return LoginFailureKind.OTHER


def password_change_phrase(text: str | None) -> str | None:
    """Return the first phrase in ``text`` saying a password must be changed
    ("Your password has expired", "required to change your password", ...),
    or None. The same phrases ``classify_portal_message()`` maps to
    ``PASSWORD_CHANGE_REQUIRED``. Used by ``password_change.detect_password_change()``."""
    match = _PORTAL_PATTERNS[0][1].search(text or "")
    return match.group(0) if match else None


def escalate(current: LoginFailureKind | str | None, new: LoginFailureKind | str | None) -> LoginFailureKind:
    """Return the more specific of two kinds, so a later retry that only
    hits a page error cannot hide an earlier "password incorrect"."""
    a = _as_kind(current)
    b = _as_kind(new)
    return b if _KIND_RANK[b] > _KIND_RANK[a] else a


def _as_kind(value: LoginFailureKind | str | None) -> LoginFailureKind:
    if isinstance(value, LoginFailureKind):
        return value
    try:
        return LoginFailureKind(value) if value else LoginFailureKind.OTHER
    except ValueError:
        return LoginFailureKind.OTHER


#: Visible error/alert elements a portal uses to say why a login failed.
_MESSAGE_SELECTORS = (
    "[role=alert]", "[role=alertdialog]", "[aria-live=assertive]", "[aria-live=polite]",
    ".alert", ".alert-danger", ".alert-error", ".error", ".errors", ".error-message",
    ".errorMessage", ".error-text", ".validation-summary-errors", ".field-validation-error",
    ".invalid-feedback", ".text-danger", ".help-block", ".message-error", ".notification-error",
    ".mat-error", "mat-error", ".mat-mdc-form-field-error", ".toast-error", ".snackbar",
    "[class*='error' i]", "[class*='alert' i]", "[id*='error' i]",
)

# Visible text of the elements above, de-duplicated, child elements whose
# text is already part of a parent skipped. Runs in the page; never throws.
_MESSAGE_JS = """(selectors) => {
  const seen = new Set(); const out = [];
  const visible = (el) => {
    const s = window.getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0
      && r.width > 0 && r.height > 0;
  };
  for (const sel of selectors) {
    let els = []; try { els = document.querySelectorAll(sel); } catch (e) { continue; }
    for (const el of els) {
      if (['INPUT','SELECT','TEXTAREA','FORM','BODY','HTML','MAIN'].includes(el.tagName)) continue;
      if (!visible(el)) continue;
      const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
      if (!t || t.length > 400 || seen.has(t)) continue;
      if (out.some((o) => o.includes(t))) continue;
      seen.add(t); out.push(t);
      if (out.length >= 8) return out;
    }
  }
  return out;
}"""


@dataclass(frozen=True)
class PageDiagnosis:
    """What ``diagnose_page()`` read off the page a login ended on (0.14.2)."""

    kind: LoginFailureKind
    portal_message: str | None
    page_url: str | None


def read_portal_messages(page: Any, extra_selectors: tuple[str, ...] | list[str] = ()) -> list[str]:
    """Visible error/alert texts on ``page`` (and its frames), most specific
    selectors first. ``extra_selectors`` are the supplier's own error
    elements, checked first. Never raises; returns [] when nothing is shown
    or the page cannot be read."""
    selectors = list(extra_selectors) + list(_MESSAGE_SELECTORS)
    messages: list[str] = []
    try:
        frames = list(getattr(page, "frames", None) or [page])
    except Exception:
        frames = [page]
    for frame in frames:
        try:
            found = frame.evaluate(_MESSAGE_JS, selectors) or []
        except Exception:
            continue
        for text in found:
            if isinstance(text, str) and text not in messages:
                messages.append(text)
    return messages


def diagnose_page(
    page: Any,
    *,
    extra_selectors: tuple[str, ...] | list[str] = (),
    check_password_change: bool = True,
) -> PageDiagnosis:
    """Read why a login failed off the page it ended on (0.14.2).

    Looks, in order, for a forced password-change page
    (``password_change.detect_password_change``), then for a visible
    error/alert message the portal displayed and classifies it with
    ``classify_portal_message()`` - so an "invalid email or password" banner
    is always ``CREDENTIALS_REJECTED``. Returns the kind (``OTHER`` when the
    page says nothing recognisable), the message text (even when
    unrecognised, so triage can read it) and the page URL.

    Only error/alert elements are read, never the whole page body: a login
    page's own help text ("Account locked? Contact us") would otherwise be
    taken for a portal message. Never raises.
    """
    if page is None:
        return PageDiagnosis(LoginFailureKind.OTHER, None, None)
    try:
        url = str(page.url) if getattr(page, "url", None) else None
    except Exception:
        url = None

    if check_password_change:
        try:
            from . import password_change  # imports this module, so not at top level
            reason = password_change.detect_password_change(page)
        except Exception:
            logger.debug("LOGIN_FAILURE - password-change check failed", exc_info=True)
            reason = None
        if reason:
            return PageDiagnosis(LoginFailureKind.PASSWORD_CHANGE_REQUIRED, _clip(reason, 500), url)

    messages = read_portal_messages(page, extra_selectors)
    kind = LoginFailureKind.OTHER
    for text in messages:
        kind = escalate(kind, classify_portal_message(text))
    # The message that decided the kind first, then the rest, for the log.
    if kind != LoginFailureKind.OTHER:
        messages.sort(key=lambda m: classify_portal_message(m) != kind)
    message = _clip(" | ".join(messages), 1000)
    return PageDiagnosis(kind, message, url)


def _settings(config: dict) -> dict:
    return config.get("login_failure_notification") or {}


def is_email_address(value: str | None) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))


def client_notice_recipient(
    config: dict,
    *,
    client_name: str | None,
    username: str | None,
    kind: LoginFailureKind | str | None,
) -> tuple[str | None, str]:
    """Decide whether to send a login-failure notice for this client, and to whom.

    The notice goes to SystemsLink (``login_failure_notification.recipient``,
    default ``support@systems-link.com``), who contact the client. ``username``
    is accepted for compatibility and is not used to pick the recipient.

    Returns ``(recipient, reason)``: recipient is None when no email should go,
    and reason says why (for the log). Does not look at the cooldown.
    """
    settings = _settings(config)
    if not _truthy(settings.get("enabled", True)):
        return None, "login_failure_notification.enabled is false"
    if _as_kind(kind) not in CLIENT_NOTIFIABLE_KINDS:
        return None, f"failure kind '{_as_kind(kind)}' is not one the client can fix"
    if not (client_name or "").strip():
        return None, "job has no client_name"
    if is_inspired_client(client_name):
        return None, f"client '{client_name}' is an Inspired-process client"
    recipient = (settings.get("recipient") or "").strip() or DEFAULT_RECIPIENT
    env = str(config.get("env") or "dev").lower()
    if env in ("prod", "live"):
        return recipient, "production run, emailing SystemsLink"
    dev_recipient = (settings.get("dev_recipient") or "").strip()
    if dev_recipient:
        return dev_recipient, f"dev run, redirected from {recipient} to dev_recipient"
    return None, "dev run and no login_failure_notification.dev_recipient set"


def notices_dir(config: dict) -> Path:
    """Folder holding one marker file per supplier+login last emailed."""
    settings = _settings(config)
    if settings.get("state_dir"):
        return Path(to_unc(settings["state_dir"]))
    env = str(config.get("env") or "dev").lower()
    folder = _ENV_FOLDERS.get(env, "DEV")
    base = to_unc((config.get("pdf_auto") or {}).get("base_path") or _DEFAULT_BASE)
    return Path(base) / folder / NOTICES_FOLDER


def _marker_path(config: dict, supplier_name: str, username: str) -> Path:
    key = f"{(supplier_name or '').strip().lower()}|{(username or '').strip().lower()}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return notices_dir(config) / f"{digest}.json"


def _recently_notified(marker: Path, cooldown: timedelta, now: datetime) -> bool:
    if not marker.exists():
        return False
    data = json.loads(marker.read_text(encoding="utf-8"))
    last = datetime.fromisoformat(data["last_sent_utc"])
    return now - last < cooldown


def _write_marker(marker: Path, *, supplier_name: str, username: str, job_id: Any, kind: LoginFailureKind, now: datetime) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "last_sent_utc": now.isoformat(),
        "supplier": supplier_name,
        "username": username,
        "job_id": str(job_id),
        "kind": str(kind),
    }, indent=2), encoding="utf-8")


_KIND_SENTENCE = {
    LoginFailureKind.CREDENTIALS_REJECTED: (
        "The portal did not accept the username and password we hold for this login. "
        "This usually means the password has been changed or has expired."
    ),
    LoginFailureKind.ACCOUNT_LOCKED: (
        "The portal has locked or temporarily blocked this login, usually after "
        "several unsuccessful sign-in attempts."
    ),
    LoginFailureKind.PASSWORD_CHANGE_REQUIRED: (
        "The portal is asking for the password on this login to be changed before it can be used."
    ),
    LoginFailureKind.OTP_NOT_RECEIVED: (
        "The portal accepted the password and sent a one-time passcode, but the passcode never "
        "reached us. The portal sends it to the email address registered on this login."
    ),
}

_KIND_ACTION = {
    LoginFailureKind.CREDENTIALS_REJECTED: (
        "Please ask the client to confirm they can sign in to the portal with the login above "
        "and, if the password has changed, to supply the current password."
    ),
    LoginFailureKind.ACCOUNT_LOCKED: (
        "Please ask the client to contact the supplier to have the login unlocked, and to "
        "confirm the current password once it is."
    ),
    LoginFailureKind.PASSWORD_CHANGE_REQUIRED: (
        "Please let the client know that the password on this login needs to be changed "
        "on the supplier portal."
    ),
    LoginFailureKind.OTP_NOT_RECEIVED: (
        "Please ask the client to make sure the email address registered on this login delivers "
        "or forwards the portal's one-time passcode emails to "
        "noreply_automation@inspiredenergy.co.uk."
    ),
}


def build_client_email(
    *,
    supplier_name: str,
    username: str,
    portal_url: str | None,
    kind: LoginFailureKind | str,
    when: datetime | None = None,
    client_name: str | None = None,
    job_id: Any = None,
    screenshot_attached: bool = False,
) -> tuple[str, str]:
    """Return ``(subject, body)`` for the SystemsLink notice about a client's
    failed login. Plain text, never the password."""
    supplier = (supplier_name or "the supplier").strip()
    client = (client_name or "").strip() or "(unknown client)"
    when = when or datetime.now()
    kind = _as_kind(kind)
    fallback = LoginFailureKind.CREDENTIALS_REJECTED
    reason = _KIND_SENTENCE.get(kind, _KIND_SENTENCE[fallback])
    action = _KIND_ACTION.get(kind, _KIND_ACTION[fallback])
    attached = "A screenshot of the page the portal showed is attached.\n\n" if screenshot_attached else ""
    subject = f"Action required: {client} - {supplier} portal login needs updating ({kind})"
    body = (
        "Hello,\n\n"
        f"We attempted to sign in to {client}'s {supplier} online account today to collect their "
        "latest bills, but the login was unsuccessful.\n\n"
        f"Client: {client}\n"
        f"Supplier: {supplier}\n"
        f"Account login: {username or '(none)'}\n"
        f"Supplier portal: {portal_url or supplier}\n"
        f"ODC job: {job_id if job_id is not None else '(unknown)'}\n"
        f"Date of attempt: {when.strftime('%d/%m/%Y')}\n\n"
        f"{reason}\n\n"
        f"Action required: {action}\n\n"
        f"{attached}"
        "Until the login has been restored, we will be unable to collect this client's bills "
        "from this portal automatically.\n\n"
        "This is an automated message from the Inspired automation team; please do not reply "
        "to this address.\n\n"
        "Many Thanks\n"
    )
    return subject, body


def notify_client(
    config: dict,
    *,
    supplier_name: str,
    job_id: Any,
    client_name: str | None,
    username: str | None,
    portal_url: str | None,
    kind: LoginFailureKind | str | None,
    now: datetime | None = None,
    screenshot: str | Path | None = None,
) -> bool:
    """Email SystemsLink about a client's failed login if the rules allow, so
    they can get the client to fix it. Returns True if an email was sent.
    ``screenshot`` (0.13.12), when it is an existing file, is attached.
    Raises on a send or state-folder failure; callers that must not fail use
    ``handle_login_failure()``."""
    kind = _as_kind(kind)
    recipient, reason = client_notice_recipient(
        config, client_name=client_name, username=username, kind=kind,
    )
    if recipient is None:
        logger.info("LOGIN_FAILURE - no SystemsLink notice for job %s: %s", job_id, reason)
        return False

    settings = _settings(config)
    now = now or datetime.now(timezone.utc)
    cooldown = timedelta(hours=float(settings.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS)))
    login = (username or "").strip()
    marker = _marker_path(config, supplier_name, login)
    # Fail closed: if the state folder cannot be read, do not email - the
    # same notice on every scheduled run is worse than one missed notice
    # (the team alert still goes out regardless).
    if _recently_notified(marker, cooldown, now):
        logger.info(
            "LOGIN_FAILURE - SystemsLink already notified about %s / %s within %s; not emailing again (job %s)",
            supplier_name, login, cooldown, job_id,
        )
        return False

    attachments = [Path(screenshot)] if screenshot and Path(screenshot).is_file() else []
    subject, body = build_client_email(
        supplier_name=supplier_name, username=login, portal_url=portal_url,
        kind=kind, when=now.astimezone(), client_name=client_name, job_id=job_id,
        screenshot_attached=bool(attachments),
    )
    graph_client.send_mail(
        config, recipient, subject, body,
        reply_to=(settings.get("reply_to") or "").strip() or None,
        sender_address=settings.get("sender_address") or DEFAULT_SENDER,
        attachments=attachments,
    )
    try:
        _write_marker(marker, supplier_name=supplier_name, username=login, job_id=job_id, kind=kind, now=now)
    except OSError:
        logger.warning(
            "LOGIN_FAILURE - SystemsLink notified but could not record it in %s; the next "
            "failed run may notify them again", marker, exc_info=True,
        )
    logger.info(
        "LOGIN_FAILURE - notice emailed to %s about client %s's %s login %s (%s), job %s (%s)",
        recipient, client_name, supplier_name, login, kind, job_id, reason,
    )
    return True


#: Optional keys in the per-env tables dict naming the two toolkit procedures.
#: Tables keys 0.14.0 read to find the inse-toolkit procedures. Ignored since
#: 0.14.1 (the Control Room reports for the bot); named so a config that still
#: has them is recognisably harmless.
REPORT_FAILURE_KEY = "report_login_failure"
REPORT_SUCCESS_KEY = "report_login_success"


@dataclass(frozen=True)
class LoginFailureReport:
    """What ``spODC_ReportLoginFailure`` said about a recorded failure (0.14.0).
    Nothing returns one since 0.14.1; kept for callers that import the name."""

    failure_id: int | None
    paused: bool
    has_owner: bool


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clip(value: Any, length: int) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text[:length] if text else None


def report_login_failure(
    config: dict,
    *,
    job_id: Any,
    kind: LoginFailureKind | str | None,
    detail: str | None = None,
    username: str | None = None,
    portal_url: str | None = None,
    screenshot: str | Path | None = None,
    process_id: Any = None,
    multi_credential: bool = False,
    tables: dict | None = None,
    dsn: str | None = None,
) -> LoginFailureReport | None:
    """Does nothing since 0.14.1; kept so 0.14.0 callers do not break.

    0.14.0 recorded the failure for inse-toolkit by calling
    ``spODC_ReportLoginFailure``. The Control Room's failure triage does that
    now, so a bot that also did it would count every failure twice. Always
    returns None, with no database call, whatever the tables key says.
    """
    return None


def report_login_success(
    config: dict,
    *,
    job_id: Any,
    process_id: Any = None,
    tables: dict | None = None,
    dsn: str | None = None,
) -> bool:
    """Does nothing since 0.14.1; kept so 0.14.0 callers do not break.

    0.14.0 told inse-toolkit a login succeeded by calling
    ``spODC_ReportLoginSuccess``. That now goes through the Control Room, like
    failures. Always returns False, with no database call.
    """
    return False


def failure_details(
    *,
    supplier_name: str,
    job_id: Any,
    kind: LoginFailureKind | str | None,
    detail: str | None = None,
    username: str | None = None,
    portal_url: str | None = None,
    client_name: str | None = None,
    screenshot: str | Path | None = None,
    process_id: Any = None,
    multi_credential: bool = False,
    portal_message: str | None = None,
    page_url: str | None = None,
) -> dict:
    """The ``details`` block sent to the Control Room with a login failure
    (0.14.1): everything its triage needs as fields, never the password.
    ``username`` is the login that failed, which is what lets inse-toolkit
    switch off one login of a multi-credential service.

    0.14.2 adds ``kind_description`` (``describe()``), ``portal_message``
    (the error text the portal displayed, if any) and ``page_url`` (where
    the login ended). ``category`` keeps its four values: inse-toolkit
    rejects any other, and the new kinds are all ``other``."""
    kind = _as_kind(kind)
    job = _int_or_none(job_id)
    return {
        "source": "odc_login_failure",
        "category": category_for(kind),
        "kind": str(kind),
        "kind_description": describe(kind),
        "portal_message": _clip(portal_message, 1000),
        "page_url": _clip(page_url, 500),
        "odc_job_id": job if job is not None else (str(job_id) if job_id is not None else None),
        "supplier": supplier_name,
        "client": _clip(client_name, 300),
        "username": _clip(username, 100),
        "portal_url": _clip(portal_url, 300),
        "screenshot": _clip(to_unc(str(screenshot)), 500) if screenshot else None,
        "multi_credential": bool(multi_credential),
        "process_id": _int_or_none(process_id),
        "detail": _clip(detail, 1000),
    }


def _add_error(errors: Any, message: str, *, details: dict | None = None, exception: Exception | None = None) -> None:
    """``errors.add()``, falling back to a call without ``details`` for an
    automation_core too old to take it. A login failure must still be reported."""
    kwargs: dict = {}
    if exception is not None:
        kwargs["exception"] = exception
    try:
        errors.add(message, details=details, **kwargs)
    except TypeError:
        errors.add(message, **kwargs)


def handle_login_failure(
    errors: Any,
    config: dict,
    *,
    supplier_name: str,
    job_id: Any,
    row: dict | None = None,
    detail: str | None = None,
    kind: LoginFailureKind | str | None = None,
    username: str | None = None,
    portal_url: str | None = None,
    client_name: str | None = None,
    team_alert: bool = True,
    screenshot: str | Path | None = None,
    process_id: Any = None,
    multi_credential: bool | None = None,
    tables: dict | None = None,
    dsn: str | None = None,
    portal_message: str | None = None,
    page_url: str | None = None,
    page: Any = None,
) -> bool:
    """The single login-failure path for every supplier bot.

    0.14.2: ``portal_message`` (the error the portal displayed) and
    ``page_url`` (where the login ended) are logged and sent to the Control
    Room. Pass ``page`` - the Playwright page the login failed on, still
    open - and the library reads both off it with ``diagnose_page()``,
    filling whichever the caller left out, and raises ``kind`` to what the
    page shows (an "invalid password" banner makes an ``OTHER`` or a
    ``LANDING_PAGE_NOT_REACHED`` into ``CREDENTIALS_REJECTED``). It never
    lowers a kind the bot set. With ``page`` and no ``screenshot``, it
    takes one (named ``login_<kind>``) before reporting (0.14.2).

    Logs one ERROR line, emails SystemsLink when ``notify_client()``'s rules
    allow, and reports the failure to the Control Room through
    ``errors.add()`` with a ``details`` block (``failure_details()``) its
    failure triage reads. Never raises: a failed notice email is logged and
    reported as its own error.

    0.14.1: the Control Room is always told about a job-level failure, even
    when SystemsLink was emailed (0.13.x stayed silent then), because its
    triage is what hands the failure to inse-toolkit. The bot no longer
    records anything in the database itself; ``tables`` and ``dsn`` are
    accepted and ignored.

    ``row`` is the job-level row (any ``get_job_details()`` row); ``username``,
    ``portal_url`` and ``client_name`` override its values - multi-credential
    bots pass the credential that failed. ``screenshot`` (0.13.12) is the path
    ``browser_helpers.take_error_screenshot()`` returned; it is attached to
    the SystemsLink notice when the file exists and its path is sent to the
    Control Room. Returns True if SystemsLink was emailed.

    ``team_alert=False`` is for a multi-credential job, where one login
    failing is not the job failing: call it per failed login with
    ``team_alert=False``, then once with the default when every login has
    failed. 0.14.1: a per-login call with an owner-fixable kind is still
    reported to the Control Room, naming the login, so that one login can be
    switched off; a per-login ``OTHER`` stays quiet as before.
    ``multi_credential`` None (the default) means ``not team_alert``, or a
    ``row`` whose job-level login is the ``multi_credential`` sentinel.
    """
    return _handle_login_failure(
        errors, config, supplier_name=supplier_name, job_id=job_id, row=row, detail=detail,
        kind=kind, username=username, portal_url=portal_url, client_name=client_name,
        team_alert=team_alert, screenshot=screenshot, process_id=process_id,
        multi_credential=multi_credential, tables=tables, dsn=dsn,
        portal_message=portal_message, page_url=page_url, page=page,
    ).sent


def _handle_login_failure(
    errors: Any,
    config: dict,
    *,
    supplier_name: str,
    job_id: Any,
    row: dict | None = None,
    detail: str | None = None,
    kind: LoginFailureKind | str | None = None,
    username: str | None = None,
    portal_url: str | None = None,
    client_name: str | None = None,
    team_alert: bool = True,
    screenshot: str | Path | None = None,
    process_id: Any = None,
    multi_credential: bool | None = None,
    tables: dict | None = None,
    dsn: str | None = None,
    portal_message: str | None = None,
    page_url: str | None = None,
    page: Any = None,
) -> "LoginFailureOutcome":
    row = row or {}
    kind = _as_kind(kind)
    if page is not None:
        try:
            diagnosis = diagnose_page(page, check_password_change=kind != LoginFailureKind.PASSWORD_CHANGE_REQUIRED)
            kind = escalate(kind, diagnosis.kind)
            portal_message = portal_message or diagnosis.portal_message
            page_url = page_url or diagnosis.page_url
        except Exception:
            logger.debug("LOGIN_FAILURE - could not diagnose the page for job %s", job_id, exc_info=True)
    if screenshot is None and page is not None:
        # 0.14.2: always a picture of the failed login before the bot ends.
        try:
            from .process_failure import screenshot_for  # imports this module
            screenshot = screenshot_for(page, config, f"login_{kind}")
        except Exception:
            logger.debug("LOGIN_FAILURE - could not screenshot job %s", job_id, exc_info=True)
    login = username if username is not None else (row.get("username") or "")
    url = portal_url if portal_url is not None else row.get("url")
    client = client_name if client_name is not None else row.get("client_name")
    text = (detail or "").strip() or "(no detail)"
    shown = (portal_message or "").strip()

    logger.error(
        "LOGIN_FAILURE - %s login failed for job %s | kind=%s | %s | portal_message=%s | page_url=%s"
        " | screenshot=%s | client=%s | user=%s | detail=%s",
        supplier_name, job_id, kind, describe(kind), f"'{shown}'" if shown else "(none shown)",
        page_url or "(unknown)", screenshot or "(none)", client or "(none)", login or "(none)", text,
    )
    if multi_credential is None:
        multi = (not team_alert) or jobstodo.is_multi_credential_job(row.get("username"), row.get("password"))
    else:
        multi = bool(multi_credential)
    sent = False
    email_error: Exception | None = None
    try:
        sent = notify_client(
            config,
            supplier_name=supplier_name,
            job_id=job_id,
            client_name=client,
            username=login,
            portal_url=url,
            kind=kind,
            screenshot=screenshot,
        )
    except Exception as exc:
        logger.exception("LOGIN_FAILURE - could not email the SystemsLink notice about job %s", job_id)
        email_error = exc

    # The Control Room triages every failure, so it is always told: the job
    # failing, and on a multi-credential job each login an owner can fix.
    if team_alert or (multi and kind in OWNER_FIXABLE_KINDS):
        details = failure_details(
            supplier_name=supplier_name, job_id=job_id, kind=kind, detail=detail,
            username=login, portal_url=url, client_name=client, screenshot=screenshot,
            process_id=process_id, multi_credential=multi,
            portal_message=portal_message, page_url=page_url,
        )
        message = f"{supplier_name} login failed for job {job_id} ({kind}): {describe(kind)}. Detail: {text}"
        if shown:
            message += f" | Portal said: '{_clip(shown, 300)}'"
        if page_url:
            message += f" | Page: {_clip(page_url, 300)}"
        _add_error(errors, message, details=details)
    if email_error is not None:
        _add_error(
            errors, f"{supplier_name} login-failure notice to SystemsLink failed for job {job_id}",
            exception=email_error,
        )
    return LoginFailureOutcome(sent, kind, portal_message, page_url, screenshot, detail)


# --- Multi-credential jobs (0.14.2) ------------------------------------------
# Every multi-credential bot reports a failed login, and "every login failed",
# the same way and with the same detail as a single-credential job.

@dataclass(frozen=True)
class LoginFailureOutcome:
    """What one login failure was reported as."""

    sent: bool
    kind: LoginFailureKind
    portal_message: str | None
    page_url: str | None
    screenshot: str | Path | None
    detail: str | None


@dataclass(frozen=True)
class CredentialFailure:
    """One failed login of a multi-credential job, kept for the job-level report."""

    username: str
    kind: LoginFailureKind
    detail: str | None
    portal_message: str | None
    page_url: str | None
    screenshot: str | None
    portal_url: str | None = None


_SUPPLIER_ATTRS = {
    "_login_failure_detail": "",
    "_login_failure_kind": LoginFailureKind.OTHER,
    "_login_failure_screenshot": None,
    "_login_failure_portal_message": None,
    "_login_failure_page_url": None,
}


def reset_supplier_login_failure(supplier: Any) -> None:
    """Clear a supplier object's ``_login_failure_*`` attributes before the next login."""
    for name, value in _SUPPLIER_ATTRS.items():
        try:
            setattr(supplier, name, value)
        except Exception:
            pass


def handle_credential_login_failure(
    errors: Any,
    config: dict,
    supplier: Any,
    *,
    supplier_name: str,
    job_id: Any,
    row: dict | None,
    username: str,
    portal_url: str | None = None,
    page: Any = None,
    process_id: Any = None,
    credential_index: int | None = None,
    credential_total: int | None = None,
) -> CredentialFailure:
    """Report one failed login of a multi-credential job (0.14.2).

    Reads the supplier's ``_login_failure_*`` attributes (detail, kind,
    screenshot, portal message, page URL; missing ones are fine), reports
    them through ``handle_login_failure(team_alert=False,
    multi_credential=True)`` naming the login - with ``page`` diagnosed and
    screenshotted exactly as for a single login - resets the attributes for
    the next credential, and returns the failure for
    ``report_all_credentials_failed()``. Never raises.
    """
    detail = (getattr(supplier, "_login_failure_detail", "") or "").strip() or f"Login failed for user {username}"
    if credential_index is not None and credential_total is not None:
        detail = f"[credential {credential_index}/{credential_total}] {detail}"
    try:
        outcome = _handle_login_failure(
            errors, config,
            supplier_name=supplier_name, job_id=job_id, row=row, detail=detail,
            kind=getattr(supplier, "_login_failure_kind", None),
            username=username, portal_url=portal_url, team_alert=False,
            screenshot=getattr(supplier, "_login_failure_screenshot", None),
            process_id=process_id, multi_credential=True,
            portal_message=getattr(supplier, "_login_failure_portal_message", None),
            page_url=getattr(supplier, "_login_failure_page_url", None),
            page=page,
        )
        failure = CredentialFailure(
            username=username, kind=outcome.kind, detail=detail,
            portal_message=outcome.portal_message, page_url=outcome.page_url,
            screenshot=str(outcome.screenshot) if outcome.screenshot else None, portal_url=portal_url,
        )
    except Exception:
        logger.exception("LOGIN_FAILURE - could not report the failed login %s on job %s", username, job_id)
        failure = CredentialFailure(
            username, _as_kind(getattr(supplier, "_login_failure_kind", None)), detail,
            None, None, None, portal_url,
        )
    reset_supplier_login_failure(supplier)
    return failure


def report_all_credentials_failed(
    errors: Any,
    *,
    supplier_name: str,
    job_id: Any,
    failures: list[CredentialFailure],
    row: dict | None = None,
    pending_accounts: int | None = None,
    process_id: Any = None,
    credential_total: int | None = None,
) -> LoginFailureKind:
    """The job-level report when every login of a multi-credential job failed
    (0.14.2), replacing each bot's own ``errors.add("... all credentials ...")``.

    Logs one ERROR line per login and a summary, and ``errors.add()``s one
    job-level failure whose ``details`` carry ``all_credentials_failed``,
    ``credentials`` (username, kind, description, portal message, page URL,
    screenshot and detail per login) and ``kind`` - the most specific kind
    across the logins. ``category`` is ``other``: each owner-fixable login
    was already reported on its own, naming it, which is what inse-toolkit
    acts on. Returns that kind. Never raises.

    ``credential_total`` is how many logins the job has. When some of them
    logged in (fewer failures than logins), the report says "failed on N of
    M" instead of "all", and ``details["all_credentials_failed"]`` is False:
    the job still stops because the last login failed with accounts pending.
    """
    row = row or {}
    total = credential_total if credential_total is not None else len(failures)
    every = len(failures) >= total
    kind = LoginFailureKind.OTHER
    for f in failures:
        kind = escalate(kind, f.kind)
    for i, f in enumerate(failures, start=1):
        shown = f"'{f.portal_message}'" if f.portal_message else "(none shown)"
        logger.error(
            "LOGIN_FAILURE - %s job %s credential %d/%d user=%s | kind=%s | %s | portal_message=%s"
            " | page_url=%s | screenshot=%s | detail=%s",
            supplier_name, job_id, i, len(failures), f.username or "(none)", f.kind, describe(f.kind),
            shown, f.page_url or "(unknown)", f.screenshot or "(none)", f.detail or "(no detail)",
        )
    summary = "; ".join(f"{f.username or '(none)'}: {f.kind}" for f in failures) or "(no logins tried)"
    pending = f", {pending_accounts} account(s) still pending" if pending_accounts is not None else ""
    scope = f"all {total}" if every else f"{len(failures)} of {total}"
    logger.error(
        "LOGIN_FAILURE - %s login failed on %s credential(s) for job %s%s | kind=%s | %s",
        supplier_name, scope, job_id, pending, kind, summary,
    )
    job = _int_or_none(job_id)
    details = {
        "source": "odc_login_failure",
        "category": CATEGORY_OTHER,
        "kind": str(kind),
        "kind_description": describe(kind),
        "all_credentials_failed": every,
        "failed_credentials": len(failures),
        "credential_total": total,
        "multi_credential": True,
        "odc_job_id": job if job is not None else (str(job_id) if job_id is not None else None),
        "supplier": supplier_name,
        "client": _clip(row.get("client_name"), 300),
        "pending_accounts": pending_accounts,
        "process_id": _int_or_none(process_id),
        "credentials": [
            {
                "username": _clip(f.username, 100),
                "kind": str(f.kind),
                "kind_description": describe(f.kind),
                "portal_message": _clip(f.portal_message, 500),
                "page_url": _clip(f.page_url, 300),
                "screenshot": _clip(to_unc(f.screenshot), 500) if f.screenshot else None,
                "detail": _clip(f.detail, 500),
            }
            for f in failures
        ],
        "detail": _clip(summary, 1000),
    }
    try:
        _add_error(
            errors,
            f"{supplier_name} login failed on {scope} credential(s) for job {job_id}{pending}: {summary}",
            details=details,
        )
    except Exception:
        logger.exception("LOGIN_FAILURE - could not report the all-credentials failure for job %s", job_id)
    return kind


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off", "")
    return bool(value)
