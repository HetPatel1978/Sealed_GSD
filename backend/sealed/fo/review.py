"""Human-reviewed account release. Never construct a Fo client or resume a job."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sealed.env import REPO_ROOT, load_env
from sealed.fo.adapter import Store, _TURN_LOCK
from sealed.service.lock import worker_lock


class ReviewError(Exception):
    pass


def review_call(path, *, job_id, message_id, confirmation_file, call_finished, no_followups):
    """Release only the selected uncertain call after explicit operator attestations.

    The operator verifies Fo's call/task state externally. File presence or natural
    language is not treated as machine-verifiable proof of task cancellation.
    """
    if not call_finished or not no_followups:
        raise ReviewError("Confirm that the call ended and no Fo follow-ups remain before releasing it.")
    try:
        confirmation = confirmation_file.read_bytes()
    except FileNotFoundError:
        raise ReviewError(f"Confirmation file not found: {confirmation_file.resolve()}. "
                          "Save Fo's actual call/follow-up confirmation there before retrying. Nothing changed.") from None
    except OSError:
        raise ReviewError("The confirmation file could not be read. Check its permissions. Nothing changed.") from None
    if not 20 <= len(confirmation) <= 100_000:
        raise ReviewError("Save the reviewed Fo confirmation in a text file (20 to 100000 bytes).")
    # Same lifecycle lock as the live API/CLI. A running worker must never race a review.
    with worker_lock(path.with_suffix(path.suffix + ".worker.lock")), _TURN_LOCK:
        store = Store(path)
        active = store.data.get("active")
        if not active:
            raise ReviewError("No active Fo reservation exists; nothing changed.")
        try:
            parts = json.loads(active)
        except (ValueError, TypeError):
            raise ReviewError("The reservation requires manual state-format review; nothing changed.") from None
        if not isinstance(parts, list) or len(parts) != 3 or parts[0] != job_id or parts[2] != message_id:
            raise ReviewError("The active reservation belongs to a different job or message; nothing changed.")
        record = store.message(active)
        if (not record or record.get("channel") != "call" or record.get("status") != "unclear"
                or record.get("reason_code") not in {"send_unclear", "call_review", "call_result_missing"}
                or not record.get("fingerprint")):
            raise ReviewError("Only an uncertain call outcome can use this release command.")
        dealer_id = record.get("dealer_id")
        cid = store.data["conversations"].get(Store.key(parts[0], parts[1], dealer_id))
        if not dealer_id or not cid or record.get("conversation") != cid:
            raise ReviewError("The call has no matching conversation binding; nothing changed.")
        if record.get("review"):
            raise ReviewError("This record already has review history; nothing changed.")
        # Keep the original status, fingerprint, reply and reason. In particular,
        # never relabel as unsent/pending/sent or make it eligible for a retry.
        store.set_status(active, record["status"], active=False, review={
            "kind": "operator_confirmed_call_closed", "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "call_finished": True, "no_pending_followups": True,
            "confirmation_sha256": hashlib.sha256(confirmation).hexdigest(),
            "job_resumed": False, "outreach_repeated": False,
        })
    return {"released": True, "job_resumed": False, "outreach_repeated": False}


def main(argv=None):
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fo-state", type=Path, default=REPO_ROOT / ".sealed/fo_state.json")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--confirmation-file", type=Path, required=True,
                        help="Local text file with the Fo call/follow-up confirmation you reviewed.")
    parser.add_argument("--call-finished", action="store_true")
    parser.add_argument("--no-pending-followups", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = review_call(args.fo_state, job_id=args.job_id, message_id=args.message_id,
                             confirmation_file=args.confirmation_file, call_finished=args.call_finished,
                             no_followups=args.no_pending_followups)
    except ReviewError as error:
        print(str(error))
        return 1
    except RuntimeError:
        print("Stop the backend/API and live CLI workers before reviewing the Fo reservation.")
        return 1
    except (OSError, ValueError, TypeError):
        print("The Fo state could not be reviewed. No external action was requested.")
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
