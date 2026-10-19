"""One way for every supplier bot to handle a failed portal login.

Before 0.13.7 each bot's ``main.py`` logged and ``errors.add()``-ed its own
message, and only TGP Mygateway sent anything else (a team email for a forced
password change). ``handle_login_failure()`` replaces all of that:

1. Logs one ERROR line in a fixed format.
2. Raises the team alert through ``errors.add()`` (the ``collect_errors``
   pipeline), worded the same for every supplier - unless the client is
   emailed (step 3) or was within the cooldown, in which case the client
   owns the fix and the team is not alerted.
3. Emails the client, at the portal login address, when **all** of these hold:

   - the failure is a credential problem the client can fix
     (``CLIENT_NOTIFIABLE_KINDS``: credentials rejected, account locked,
     password change required). Our-side faults (portal down, page errors,
     OTP mailbox, human-assist timeout) are ``OTHER`` and go to the team only;
   - the client is not Inspired PLC or Ignite
     (``file_allocation_core.routing.is_inspired_client``);
   - the login username is an email address;
   - this is a production run (``config["env"] == "prod"``). A dev run sends
     to ``login_failure_notification.dev_recipient`` if set, else nothing;
   - the same supplier + login has not been emailed within ``cooldown_hours``
     (default 24), so a job that fails on every scheduled run does not
     email the client every time.

   The email goes from ``noreply_automation@inspiredenergy.co.uk`` with
   Reply-To ``support@systems-link.com``. It never contains the password.

Classifying the failure is the supplier's job, because only the supplier
knows which page text is a portal message and which is an exception string.
``classify_portal_message()`` turns a message the portal displayed into a
``LoginFailureKind``, and ``escalate()`` keeps the most specific kind seen
across retries. Never classify exception text: Playwright errors quote the
selector they were waiting on, which can itself read "password invalid".

Config (all optional; defaults shown)::

    login_failure_notification:
      enabled: true
      sender_address: noreply_automation@inspiredenergy.co.uk
      reply_to: support@systems-link.com
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

from . import graph_client

logger = logging.getLogger(__name__)


class LoginFailureKind(StrEnum):
    """Why a login failed, as far as the bot can tell."""

    CREDENTIALS_REJECTED = "credentials rejected"
    ACCOUNT_LOCKED = "account locked"
    PASSWORD_CHANGE_REQUIRED = "password change required"
    OTHER = "other"


#: Kinds the client can fix themselves, so the only ones they are emailed about.
CLIENT_NOTIFIABLE_KINDS = frozenset({
    LoginFailureKind.CREDENTIALS_REJECTED,
    LoginFailureKind.ACCOUNT_LOCKED,
    LoginFailureKind.PASSWORD_CHANGE_REQUIRED,
})

DEFAULT_SENDER = "noreply_automation@inspiredenergy.co.uk"
DEFAULT_REPLY_TO = "support@systems-link.com"
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
        r"|change your password to continue",
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
    """Decide whether to email the client, and to whom.

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
    login = (username or "").strip()
    if not is_email_address(login):
        return None, "login username is not an email address"
    env = str(config.get("env") or "dev").lower()
    if env in ("prod", "live"):
        return login, "production run, emailing the login address"
    dev_recipient = (settings.get("dev_recipient") or "").strip()
    if dev_recipient:
        return dev_recipient, f"dev run, redirected from {login} to dev_recipient"
    return None, "dev run and no login_failure_notification.dev_recipient set"


def notices_dir(config: dict) -> Path:
    """Folder holding one marker file per supplier+login last emailed."""
    settings = _settings(config)
    if settings.get("state_dir"):
        return Path(settings["state_dir"])
    env = str(config.get("env") or "dev").lower()
    folder = _ENV_FOLDERS.get(env, "DEV")
    base = (config.get("pdf_auto") or {}).get("base_path") or _DEFAULT_BASE
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
        "The portal did not accept the username and password we hold for this account. "
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


def build_client_email(
    *,
    supplier_name: str,
    username: str,
    portal_url: str | None,
    kind: LoginFailureKind | str,
    when: datetime | None = None,
    reply_to: str = DEFAULT_REPLY_TO,
) -> tuple[str, str]:
    """Return ``(subject, body)`` for the client notice. Plain text, no password."""
    supplier = (supplier_name or "your supplier").strip()
    when = when or datetime.now()
    reason = _KIND_SENTENCE.get(_as_kind(kind), _KIND_SENTENCE[LoginFailureKind.CREDENTIALS_REJECTED])
    subject = f"Action required: we were unable to sign in to your {supplier} online account"
    body = (
        "Hello,\n\n"
        f"We attempted to sign in to your {supplier} online account today to collect your latest "
        "bills, but the login was unsuccessful.\n\n"
        f"Account login: {username}\n"
        f"Supplier portal: {portal_url or supplier}\n"
        f"Date of attempt: {when.strftime('%d/%m/%Y')}\n\n"
        f"{reason}\n\n"
        "Action required:\n\n"
        f"1. Please confirm that you are able to sign in to the {supplier} portal using the login above.\n"
        "2. If the password has changed, please contact our support team with the updated details.\n"
        f"3. If the account is locked, please contact {supplier} to have it unlocked, then let us know.\n\n"
        "Until the login has been restored, we will be unable to collect your bills from this "
        "portal automatically.\n\n"
        "Should you have any questions, please reply to this email and it will reach our support "
        f"team at {reply_to}.\n\n"
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
    """Email the client about a failed login if the rules allow. Returns True
    if an email was sent. Raises on a send or state-folder failure; callers
    that must not fail use ``handle_login_failure()``."""
    kind = _as_kind(kind)
    recipient, reason = client_notice_recipient(
        config, client_name=client_name, username=username, kind=kind,
    )
    if recipient is None:
        logger.info("LOGIN_FAILURE - no client email for job %s: %s", job_id, reason)
        return False

    settings = _settings(config)
    now = now or datetime.now(timezone.utc)
    cooldown = timedelta(hours=float(settings.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS)))
    login = (username or "").strip()
    marker = _marker_path(config, supplier_name, login)
    # Fail closed: if the state folder cannot be read, do not email - a
    # client getting the same notice on every scheduled run is worse than
    # one missed notice (the team alert still goes out regardless).
    if _recently_notified(marker, cooldown, now):
        logger.info(
            "LOGIN_FAILURE - client already emailed about %s / %s within %s; not emailing again (job %s)",
            supplier_name, login, cooldown, job_id,
        )
        return False

    reply_to = settings.get("reply_to") or DEFAULT_REPLY_TO
    subject, body = build_client_email(
        supplier_name=supplier_name, username=login, portal_url=portal_url,
        kind=kind, when=now.astimezone(), reply_to=reply_to,
    )
    graph_client.send_mail(
        config, recipient, subject, body,
        reply_to=reply_to,
        sender_address=settings.get("sender_address") or DEFAULT_SENDER,
    )
    try:
        _write_marker(marker, supplier_name=supplier_name, username=login, job_id=job_id, kind=kind, now=now)
    except OSError:
        logger.warning(
            "LOGIN_FAILURE - client emailed but could not record it in %s; the next "
            "failed run may email them again", marker, exc_info=True,
        )
    logger.info(
        "LOGIN_FAILURE - client emailed at %s about %s login failure (%s), job %s (%s)",
        recipient, supplier_name, kind, job_id, reason,
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

    Logs, and emails the client when ``notify_client()``'s rules allow;
    otherwise raises the team alert through ``errors.add()``. A client that
    is emailed (or was, within the cooldown) gets no team alert. Never
    raises: a failed client email is logged and reported to the team as its
    own error, alongside the team alert.

    ``row`` is the job-level row (any ``get_job_details()`` row); ``username``,
    ``portal_url`` and ``client_name`` override its values - multi-credential
    bots pass the credential that failed. Returns True if the client was emailed.

    ``team_alert=False`` skips the ``errors.add()`` (still logs and still
    emails the client). For a multi-credential job, where one credential
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
        logger.exception("LOGIN_FAILURE - could not email the client about job %s", job_id)
        email_error = exc

    # The client owns the fix once they are emailed (now, or within the
    # cooldown), so the team is not alerted. Everything else - Inspired PLC /
    # Ignite, our-side faults, a non-email login, a failed send - still is.
    client_handles_it = recipient is not None and email_error is None
    if team_alert and not client_handles_it:
        errors.add(f"{supplier_name} login failed for job {job_id} ({kind}): {text}")
    if email_error is not None:
        errors.add(f"{supplier_name} login-failure email to the client failed for job {job_id}", exception=email_error)
    return sent


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off", "")
    return bool(value)
