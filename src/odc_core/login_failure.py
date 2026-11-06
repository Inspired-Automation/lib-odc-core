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
     password change required). Our-side faults (portal down, page errors,
     OTP mailbox, human-assist timeout) are ``OTHER`` and go to the team only;
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

Config (all optional; defaults shown)::

    login_failure_notification:
      enabled: true
      recipient: support@systems-link.com
      sender_address: noreply_automation@inspiredenergy.co.uk
      reply_to: ""               # optional Reply-To; none by default
      cooldown_hours: 24
      dev_recipient: ""          # dev runs email only this address, if set
      state_dir: ""              # default {pdf_auto.base_path}\\{LIVE|DEV}\\LOGIN_FAILURE_NOTICES
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from file_allocation_core.routing import is_inspired_client
from file_allocation_core.unc import to_unc

from . import graph_client

logger = logging.getLogger(__name__)


class LoginFailureKind(StrEnum):
    """Why a login failed, as far as the bot can tell."""

    CREDENTIALS_REJECTED = "credentials rejected"
    ACCOUNT_LOCKED = "account locked"
    PASSWORD_CHANGE_REQUIRED = "password change required"
    OTHER = "other"


#: Kinds the client can fix themselves, so the only ones SystemsLink is
#: emailed about (to pass on to the client).
CLIENT_NOTIFIABLE_KINDS = frozenset({
    LoginFailureKind.CREDENTIALS_REJECTED,
    LoginFailureKind.ACCOUNT_LOCKED,
    LoginFailureKind.PASSWORD_CHANGE_REQUIRED,
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
)

_KIND_RANK = {
    LoginFailureKind.OTHER: 0,
    LoginFailureKind.CREDENTIALS_REJECTED: 1,
    LoginFailureKind.ACCOUNT_LOCKED: 2,
    LoginFailureKind.PASSWORD_CHANGE_REQUIRED: 3,
}


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
        "Please ask the client to sign in to the portal, set a new password, and supply the "
        "new password."
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
) -> bool:
    """Email SystemsLink about a client's failed login if the rules allow, so
    they can get the client to fix it. Returns True if an email was sent.
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

    subject, body = build_client_email(
        supplier_name=supplier_name, username=login, portal_url=portal_url,
        kind=kind, when=now.astimezone(), client_name=client_name, job_id=job_id,
    )
    graph_client.send_mail(
        config, recipient, subject, body,
        reply_to=(settings.get("reply_to") or "").strip() or None,
        sender_address=settings.get("sender_address") or DEFAULT_SENDER,
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
) -> bool:
    """The single login-failure path for every supplier bot.

    Logs, and emails SystemsLink when ``notify_client()``'s rules allow;
    otherwise raises the team alert through ``errors.add()``. A failure
    SystemsLink is notified about (or was, within the cooldown) gets no team
    alert. Never raises: a failed notice email is logged and reported to the
    team as its own error, alongside the team alert.

    ``row`` is the job-level row (any ``get_job_details()`` row); ``username``,
    ``portal_url`` and ``client_name`` override its values - multi-credential
    bots pass the credential that failed. Returns True if SystemsLink was emailed.

    ``team_alert=False`` skips the ``errors.add()`` (still logs and still
    emails SystemsLink). For a multi-credential job, where one credential
    failing is not a job failure: call it per failed credential with
    ``team_alert=False``, then once with the default when every credential
    has failed.
    """
    row = row or {}
    kind = _as_kind(kind)
    login = username if username is not None else (row.get("username") or "")
    url = portal_url if portal_url is not None else row.get("url")
    client = client_name if client_name is not None else row.get("client_name")
    text = (detail or "").strip() or "(no detail)"

    logger.error(
        "LOGIN_FAILURE - %s login failed for job %s | kind=%s | client=%s | user=%s | %s",
        supplier_name, job_id, kind, client or "(none)", login or "(none)", text,
    )
    recipient = None
    sent = False
    email_error: Exception | None = None
    try:
        recipient, _ = client_notice_recipient(config, client_name=client, username=login, kind=kind)
        sent = notify_client(
            config,
            supplier_name=supplier_name,
            job_id=job_id,
            client_name=client,
            username=login,
            portal_url=url,
            kind=kind,
        )
    except Exception as exc:
        logger.exception("LOGIN_FAILURE - could not email the SystemsLink notice about job %s", job_id)
        email_error = exc

    # SystemsLink own getting the client to fix it once they are notified
    # (now, or within the cooldown), so the team is not alerted. Everything
    # else - Inspired PLC / Ignite, our-side faults, a failed send - still is.
    client_handles_it = recipient is not None and email_error is None
    if team_alert and not client_handles_it:
        errors.add(f"{supplier_name} login failed for job {job_id} ({kind}): {text}")
    if email_error is not None:
        errors.add(f"{supplier_name} login-failure notice to SystemsLink failed for job {job_id}", exception=email_error)
    return sent


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off", "")
    return bool(value)
