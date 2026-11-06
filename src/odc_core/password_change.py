"""Spot a supplier portal's forced password-change page during login (0.13.11).

Portals that make a login change its password show a page instead of the
logged-in landing page, often with no error text, so a bot that only checks
"did I reach the dashboard?" reports it as kind ``other`` and only the team
hears about it (TGP Mygateway job 5448, 2026-11-06). Every supplier bot calls
the same check here so the failure is reported as
``LoginFailureKind.PASSWORD_CHANGE_REQUIRED`` and SystemsLink is told.

Call it only while logging in, after the bot's own logged-in check has failed:
a logged-in page may carry a "your password will expire, please change your
password" banner that is not a forced change.

``detect_password_change(page)`` returns a short reason when any one of these
is seen on the page or in any of its frames, else None:

1. URL: the path or fragment (not the query string) names a password change
   (``#/change-expired-password``, ``/ChangePassword``, ``/password-expired``,
   ``/update-password``). "forgot password" routes never count.
2. Form: two or more visible password fields, and either an old/current
   password field beside a new/confirm one, or a "new password" field. Read
   from the fields' name, id, placeholder, aria-label, autocomplete and
   label, and from the page's visible text. A normal sign-in form has one
   password field; a sign-in page with a register form next to it has
   "confirm password" but no old/current or "new password", so neither counts.
3. Message: visible text with a password-change phrase
   (``login_failure.password_change_phrase()``), but only when the page does
   not have exactly one password field, so help text on the sign-in form
   itself ("If your password has expired...") does not count.
4. Supplier extras: any ``extra_selectors`` visible, or any
   ``extra_url_patterns`` (regex) matching the URL.

A bare "Change Password" link or heading is deliberately not a signal: the
top nav of a logged-in portal usually has one.

``wait_for_password_change(page, done=...)`` polls for up to ``timeout_s``
after a submit, because single-page apps route a few seconds after
``networkidle`` returns. It stops early when ``done()`` (the bot's own
logged-in check) is true.

Neither function raises: a page or frame that cannot be read is skipped.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from .login_failure import password_change_phrase

logger = logging.getLogger(__name__)

DEFAULT_WAIT_S = 10.0
_POLL_S = 0.5
_REASON_MAX = 120

_URL_RE = re.compile(
    r"(change|expired?|update|renew|reset|new)[-_ ]?(your[-_ ]?)?(pass[-_ ]?word|pwd)"
    r"|(pass[-_ ]?word|pwd)[-_ ]?(change|expired?|update|renew|reset)",
    re.I,
)
_FORGOT_RE = re.compile(r"forgot|forgotten|recover", re.I)

_OLD_RE = re.compile(r"\b(old|current|existing|previous|temporary|temp) password\b")
_NEW_RE = re.compile(r"\b(new|confirm|confirmed|re ?enter|re ?type|repeat|verify) (new )?password\b")
_STRICT_NEW_RE = re.compile(r"\bnew password\b")

# Runs in each frame. Visible password fields, what describes them, and the
# frame's visible text (capped, so a huge page cannot stall the check).
_FRAME_JS = """() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const fields = Array.from(document.querySelectorAll('input[type=password]')).filter(visible);
  const desc = fields.map(el => {
    const parts = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                   el.getAttribute('autocomplete')];
    if (el.labels) { for (const l of el.labels) parts.push(l.innerText); }
    return parts.filter(Boolean).join(' ');
  }).join(' | ');
  const text = document.body ? (document.body.innerText || '').slice(0, 20000) : '';
  return {count: fields.length, desc: desc, text: text};
}"""


def _normalise(value: str) -> str:
    """'oldPassword' / 'old-password' / 'OLD_PASSWORD' -> 'old password'."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", value or "")
    spaced = re.sub(r"[-_]+", " ", spaced)
    return re.sub(r"\s+", " ", spaced).lower()


def _short(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= _REASON_MAX else text[: _REASON_MAX - 3] + "..."


def url_signal(url: str | None, extra_url_patterns: Iterable[str] = ()) -> str | None:
    """Reason if the URL's path or fragment names a password change, else None."""
    if not url:
        return None
    parts = urlsplit(url)
    target = f"{parts.path}#{parts.fragment}"
    for pattern in extra_url_patterns:
        if re.search(pattern, url, re.I):
            return f"url matched supplier pattern {pattern!r}: {_short(url)}"
    if _FORGOT_RE.search(target):
        return None
    if _URL_RE.search(target):
        return f"url is a password-change route: {_short(url)}"
    return None


def form_signal(count: int, desc: str, text: str) -> str | None:
    """Reason if the visible fields and text make a password-change form, else None."""
    if count < 2:
        return None
    combined = _normalise(f"{desc} {text}")
    if _OLD_RE.search(combined) and _NEW_RE.search(combined):
        return f"password-change form: old and new password fields ({count} password fields)"
    if _STRICT_NEW_RE.search(combined):
        return f"password-change form: new password fields ({count} password fields)"
    return None


def message_signal(count: int, text: str) -> str | None:
    """Reason if the visible text says a password must be changed, else None.
    Ignored on a page with exactly one password field (the sign-in form)."""
    if count == 1:
        return None
    phrase = password_change_phrase(text)
    return f"page says: {_short(phrase)!r}" if phrase else None


def _frames(page: Any) -> list[Any]:
    try:
        frames = list(page.frames)
    except Exception:
        return [page]
    return frames or [page]


def detect_password_change(
    page: Any,
    *,
    extra_selectors: Iterable[str] = (),
    extra_url_patterns: Iterable[str] = (),
) -> str | None:
    """Return why the page looks like a forced password change, or None.

    Call only during login, after the bot's own logged-in check failed.
    Never raises.
    """
    try:
        reason = url_signal(page.url, extra_url_patterns)
    except Exception:
        reason = None
    if reason:
        return reason

    for selector in extra_selectors:
        try:
            if page.locator(selector).first.is_visible():
                return f"supplier marker visible: {selector}"
        except Exception:
            logger.debug("PASSWORD_CHANGE - could not check selector %s", selector, exc_info=True)

    for frame in _frames(page):
        try:
            info = frame.evaluate(_FRAME_JS)
        except Exception:
            logger.debug("PASSWORD_CHANGE - could not read a frame", exc_info=True)
            continue
        count = int(info.get("count") or 0)
        desc = info.get("desc") or ""
        text = info.get("text") or ""
        reason = form_signal(count, desc, text) or message_signal(count, text)
        if reason:
            return reason
        try:
            reason = url_signal(frame.url, extra_url_patterns) if frame is not page else None
        except Exception:
            reason = None
        if reason:
            return f"frame {reason}"
    return None


def wait_for_password_change(
    page: Any,
    *,
    done: Callable[[], bool] | None = None,
    timeout_s: float = DEFAULT_WAIT_S,
    extra_selectors: Iterable[str] = (),
    extra_url_patterns: Iterable[str] = (),
) -> str | None:
    """Poll after a login submit until a password-change page shows (returns
    the reason), ``done()`` is true (returns None), or ``timeout_s`` passes
    (returns None after one last check). Never raises."""
    extra_selectors = tuple(extra_selectors)
    extra_url_patterns = tuple(extra_url_patterns)
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        if done is not None:
            try:
                if done():
                    return None
            except Exception:
                logger.debug("PASSWORD_CHANGE - done() raised; ignoring", exc_info=True)
        reason = detect_password_change(
            page, extra_selectors=extra_selectors, extra_url_patterns=extra_url_patterns,
        )
        if reason or time.monotonic() >= deadline:
            return reason
        time.sleep(_POLL_S)
