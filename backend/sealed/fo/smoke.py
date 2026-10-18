"""One controlled email through the live adapter. Dry run unless --yes-send is explicitly supplied.

    cd backend
    .venv/Scripts/python -m sealed.fo.smoke --to teammate@example.com --cc buyer@example.com

After the teammate approves, repeat with --yes-send. Repeating the same run ID never resends;
use a new --run-id only for a deliberately separate test. No job ceiling exists in this test.
"""

import argparse
import json
import os
import re
from email.utils import parseaddr
from pathlib import Path

from sealed.core.guard import Guard
from sealed.core.models import Outbound
from sealed.env import REPO_ROOT, load_env
from sealed.fo import messages


class NoCeiling:
    """This fixed test request has no ceiling and never reads a real job or vault."""

    def contains_secret(self, text: str) -> bool:
        return False


def build_message(to: str, cc: str, run_id: str) -> Outbound:
    for address in (to, cc):
        if (not address or parseaddr(address)[1] != address or "@" not in address
                or any(c in address for c in "\r\n\t ")):
            raise ValueError("Use plain recipient and CC email addresses.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", run_id):
        raise ValueError("Run ID must be 1-40 letters, digits, underscores or hyphens.")
    subject = f"Sealed adapter integration test [SEALED-ADAPTER-{run_id}]"
    body = (
        "Hello,\n\nI'm Fo, an AI assistant writing on behalf of the account owner. "
        "This is a controlled Sealed integration test. Could you reply with a sample written total "
        "including all fees and an availability date? " + messages.SAFETY_PHRASES[0]
        + "\n\nPlease use reply-all so the account owner sees your answer.\n\nThank you,\nFo, AI assistant"
    )
    return Outbound("rfq_01", "teammate", "rfq", "email", 1, to, subject, body,
                    messages.wrap_for_fo(to, cc, subject, body), cc=cc, dealer_name="teammate",
                    job_id="fo-adapter-smoke", run_id=run_id)


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True)
    parser.add_argument("--cc", default=os.getenv("GMAIL_ADDRESS"))
    parser.add_argument("--run-id", default="smoke_01")
    parser.add_argument("--state", type=Path, default=REPO_ROOT / ".sealed" / "fo_state.json")
    parser.add_argument("--yes-send", action="store_true", help="send one email after the teammate approves")
    args = parser.parse_args(argv)
    try:
        msg = build_message(args.to, args.cc, args.run_id)
    except ValueError as e:
        parser.error(str(e))
    guard = Guard(NoCeiling())
    result = guard.check(msg)
    if not result.passed:
        parser.error("Test payload failed the pre-send guard; nothing was sent.")
    print(f"To: {msg.to}\nCC: {msg.cc}\nSubject: {msg.subject}\n\n{msg.body}")
    if not args.yes_send:
        print("\nDry run. Repeat with --yes-send only after the teammate approves.")
        return 0

    # Import after load_env, and only on the explicit live path. No OAuth client exists during dry runs.
    from sealed.fo.adapter import FoTransport
    from sealed.fo.client import LiveFoClient

    client = LiveFoClient()
    try:
        transport = FoTransport(client, args.state)
        sent = transport.send(0, msg, lambda preview: guard.check(msg, preview=preview).passed)
        print(json.dumps({"sent": sent.sent, "reason_code": sent.reason_code, "errors": sent.errors}, indent=2))
        return 0 if sent.sent and sent.reason_code is None else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
