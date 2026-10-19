"""Send one test login-failure email, to check the Graph send path end to end.

Sends the real client email (odc_core.login_failure.build_client_email) from
noreply_automation@inspiredenergy.co.uk with Reply-To support@systems-link.com,
to the address you give (default: jane.hong@inspiredenergy.co.uk). It does not
touch any database, job or client, and writes no cooldown marker.

Run it from any ODC bot repo whose venv has automation_core installed, so the
shared team.yaml Graph credentials load the same way a bot's do, e.g.:

    cd "C:\\git projects\\automation-odc-edf-energy"
    .venv\\Scripts\\python "..\\lib-odc-core\\tools\\send_test_login_failure_email.py"

Options: --to someone@example.com   --kind "account locked"   --supplier "EDF Energy"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Use this checkout's odc_core (with login_failure), not whatever wheel the venv pins.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from automation_core.config import load_config  # noqa: E402
from odc_core import graph_client  # noqa: E402
from odc_core.login_failure import (  # noqa: E402
    DEFAULT_REPLY_TO,
    DEFAULT_SENDER,
    LoginFailureKind,
    build_client_email,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", default="jane.hong@inspiredenergy.co.uk")
    parser.add_argument("--supplier", default="EDF Energy")
    parser.add_argument("--login", default="billing@example-client.co.uk",
                        help="login shown in the email body (not emailed)")
    parser.add_argument("--portal", default="https://www.edfenergy.com/ebs-portal/login")
    parser.add_argument("--kind", default=str(LoginFailureKind.CREDENTIALS_REJECTED),
                        choices=[str(k) for k in LoginFailureKind if k is not LoginFailureKind.OTHER])
    args = parser.parse_args()

    config = load_config()
    settings = config.get("login_failure_notification") or {}
    sender = settings.get("sender_address") or DEFAULT_SENDER
    reply_to = settings.get("reply_to") or DEFAULT_REPLY_TO

    subject, body = build_client_email(
        supplier_name=args.supplier, username=args.login,
        portal_url=args.portal, kind=args.kind, reply_to=reply_to,
    )
    subject = f"[TEST] {subject}"

    print(f"From:     {sender}\nTo:       {args.to}\nReply-To: {reply_to}\nSubject:  {subject}\n")
    try:
        graph_client.send_mail(config, args.to, subject, body,
                               reply_to=reply_to, sender_address=sender)
    except Exception as exc:  # report the Graph error plainly
        print(f"FAILED: {exc}")
        if "403" in str(exc) or "ErrorAccessDenied" in str(exc):
            print("\nThe Graph app is not allowed to send as this mailbox. It needs the "
                  "Mail.Send application permission, and any application access policy "
                  f"must include {sender}.")
        return 1
    print("Sent. Check the inbox, then hit Reply and confirm it addresses "
          f"{reply_to}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
