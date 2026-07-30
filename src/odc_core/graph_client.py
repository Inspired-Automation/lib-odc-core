"""Microsoft Graph helpers: send mail and read an OTP code from a mailbox.

Both functions read Graph app credentials from ``config["graph"]``:
    client_id, client_secret, tenant_id, sender_address

Never hardcode these values in source. ``sender_address`` is only used by
``send_mail``; ``read_otp_code`` takes the mailbox to poll explicitly.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import msal
import requests

logger = logging.getLogger(__name__)


def _acquire_token(config: dict) -> str:
    graph_config = config.get("graph", {})
    tenant_id = graph_config.get("tenant_id")
    client_id = graph_config.get("client_id")
    client_secret = graph_config.get("client_secret")
    if not tenant_id or not client_id or not client_secret:
        raise ValueError(
            "GRAPH_CLIENT - config['graph'] must supply tenant_id, client_id "
            "and client_secret"
        )

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=authority,
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(
            f"Failed to acquire Graph token: {result.get('error_description', result)}"
        )
    return result["access_token"]


def send_mail(config: dict, recipient: str, subject: str, body: str) -> None:
    """Send an email via Microsoft Graph as config['graph']['sender_address']."""
    graph_config = config.get("graph", {})
    sender_address = graph_config.get("sender_address")
    if not sender_address:
        raise ValueError("GRAPH_CLIENT - config['graph']['sender_address'] is required")

    token = _acquire_token(config)
    url = f"https://graph.microsoft.com/v1.0/users/{sender_address}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        }
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    if response.status_code >= 300:
        raise RuntimeError(
            f"GRAPH_CLIENT - sendMail failed: {response.status_code} {response.text[:200]}"
        )
    logger.debug("GRAPH_CLIENT - mail sent to %s (subject=%s)", recipient, subject)


def _parse_otp_from_body(body: str, *, body_start: str, body_end: str) -> str:
    if body_start not in body:
        return ""
    after = body.split(body_start, 1)[1]
    if body_end in after:
        after = after.split(body_end, 1)[0]
    code = re.sub(r"\s+", "", after.strip())
    match = re.search(r"\b(\d{4,8})\b", code)
    # No digit group means this is not the OTP mail (or the format changed).
    # Return "" so read_otp_code keeps polling rather than accepting body text
    # as if it were a code.
    return match.group(1) if match else ""


def _list_recent_messages(
    token: str,
    mailbox: str,
    since: datetime,
    subject_contains: str,
) -> list[dict]:
    since_utc = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_subject = subject_contains.replace("'", "''")
    filt = (
        f"receivedDateTime ge {since_utc} and "
        f"contains(subject,'{safe_subject}')"
    )
    url = (
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages"
        f"?$filter={quote(filt)}&$orderby=receivedDateTime desc&$top=10"
        f"&$select=subject,body,receivedDateTime"
    )
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers, timeout=30)
    if response.status_code != 200:
        logger.debug(
            "GRAPH_CLIENT - list messages failed: %s %s",
            response.status_code, response.text[:200],
        )
        return []
    return response.json().get("value", [])


def read_otp_code(
    config: dict,
    mailbox: str,
    login_time: datetime,
    *,
    subject_contains: str,
    body_start: str,
    body_end: str,
    timeout_s: int = 3600,
    poll_interval_s: int = 10,
) -> str:
    """
    Poll mailbox for an OTP email received after login_time.

    Returns the OTP code string, or raises TimeoutError if not found in time.
    """
    deadline = time.time() + timeout_s
    logger.debug(
        "GRAPH_CLIENT - polling %s for OTP (since %s, timeout %ds)",
        mailbox, login_time.isoformat(), timeout_s,
    )

    while time.time() < deadline:
        try:
            token = _acquire_token(config)
            messages = _list_recent_messages(token, mailbox, login_time, subject_contains)
            for msg in messages:
                body = msg.get("body", {}).get("content", "") or ""
                code = _parse_otp_from_body(body, body_start=body_start, body_end=body_end)
                if code:
                    logger.debug(
                        "GRAPH_CLIENT - OTP found in message: %s", msg.get("subject", "")
                    )
                    return code
        except Exception:
            logger.exception("GRAPH_CLIENT - poll error")

        time.sleep(poll_interval_s)

    raise TimeoutError(
        f"OTP not received in {mailbox} within {timeout_s}s "
        f"(subject contains '{subject_contains}')"
    )
