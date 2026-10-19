"""Short SQLite transactions; no database connection is held across Fo calls."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from sealed.discovery.models import MIN_DEALERS, DiscoveryError

TERMINAL = ("complete", "failed", "needs_review")


class Store:
    def __init__(self, path, scope, mode):
        self.path, self.scope, self.mode = path, scope, mode
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS discovery_jobs (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, request TEXT NOT NULL,
                status TEXT NOT NULL, phase TEXT NOT NULL, conversation_id TEXT,
                raw_reply TEXT, result TEXT, error TEXT, polls INTEGER NOT NULL DEFAULT 0)""")
            # Claude backup (added later): the user's separate permission, and what the backup did as JSON
            # {"reason", "fo_status", "error"}. NULL fallback means the backup never ran.
            columns = {row[1] for row in db.execute("PRAGMA table_info(discovery_jobs)")}
            if "allow_claude" not in columns:
                db.execute("ALTER TABLE discovery_jobs ADD COLUMN allow_claude INTEGER NOT NULL DEFAULT 0")
            if "fallback" not in columns:
                db.execute("ALTER TABLE discovery_jobs ADD COLUMN fallback TEXT")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, request, *, retry_unattempted=False, allow_claude=False):
        encoded = request.model_dump_json()
        digest = hashlib.sha256(("fo-discovery-v1:" + self.mode + ":" + encoded).encode()).hexdigest()
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM discovery_jobs WHERE scope=? AND fingerprint=? ORDER BY created DESC LIMIT 1", (self.scope, digest)).fetchone()
            if row and (row["status"] != "complete" or now-row["updated"] < 3600):
                requeued = False
                if (retry_unattempted and row["status"] == "needs_review" and row["phase"] == "new"
                        and row["error"] == "fo_account_requires_review" and row["polls"] == 0
                        and not row["conversation_id"] and not row["raw_reply"] and not row["result"]):
                    # This search never reached Fo. Explicit resubmission may queue
                    # it once the account reservation has been reviewed and cleared.
                    # A failed Claude backup from the blocked attempt is cleared so it can run again.
                    db.execute("UPDATE discovery_jobs SET status='queued',error=NULL,fallback=NULL,updated=? WHERE id=? AND scope=?",
                               (now, row["id"], self.scope))
                    requeued = True
                if allow_claude and not row["allow_claude"]:
                    # Permission given later: never re-ask Fo, but a finished, thin search gets the backup
                    # (unless Fo itself was just re-queued above; then the backup waits for Fo's outcome).
                    db.execute("UPDATE discovery_jobs SET allow_claude=1 WHERE id=?", (row["id"],))
                    if not requeued and row["status"] in TERMINAL and row["fallback"] is None and thin(dict(row)):
                        fallback = {"reason": row["error"] or "too_few", "fo_status": row["status"],
                                    "fo_phase": row["phase"], "error": None}
                        db.execute("UPDATE discovery_jobs SET status='queued', phase='claude', fallback=?, updated=? WHERE id=?",
                                   (json.dumps(fallback), now, row["id"]))
                return row["id"], True
            id_ = "search_" + uuid4().hex[:16]
            db.execute("INSERT INTO discovery_jobs(id,scope,fingerprint,created,updated,request,status,phase,allow_claude) VALUES (?,?,?,?,?,?,?,?,?)",
                       (id_, self.scope, digest, now, now, encoded, "queued", "new", int(bool(allow_claude))))
            return id_, False

    def get(self, id_):
        with self.connection() as db:
            row = db.execute("SELECT * FROM discovery_jobs WHERE id=? AND scope=?", (id_, self.scope)).fetchone()
        if not row:
            raise DiscoveryError("discovery_not_found")
        return dict(row)

    def update(self, id_, **fields):
        allowed = {"status", "phase", "conversation_id", "raw_reply", "result", "error", "polls", "fallback"}
        if set(fields)-allowed:
            raise ValueError("Invalid discovery fields")
        fields["updated"] = time.time()
        with self.connection() as db:
            db.execute("UPDATE discovery_jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=? AND scope=?",
                       (*fields.values(), id_, self.scope))

    def pending(self):
        with self.connection() as db:
            return [row[0] for row in db.execute("SELECT id FROM discovery_jobs WHERE scope=? AND status IN ('queued','researching','validating') ORDER BY created", (self.scope,))]

    def view(self, id_):
        row = self.get(id_)
        req = json.loads(row["request"])
        messages = {"queued": "Waiting for the Fo account.", "researching": "Fo is researching official dealer pages. This can take several minutes.",
                    "validating": "Checking published contacts, inventory links and distance.", "complete": "Research complete. Review and select dealers before outreach.",
                    "needs_review": "Research needs review. No request was automatically resent.", "failed": "Research is unavailable. You can add sellers manually."}
        if row["phase"] == "claude":
            messages |= {"queued": "Waiting to start the Claude backup research.",
                         "researching": "Fo's research was unusable or thin. Claude is researching official dealer pages as the backup.",
                         "validating": "Checking the backup's published contacts, inventory links and distance."}
        result = json.loads(row["result"]) if row["result"] else {"dealers": [], "dropped": 0}
        for index, dealer in enumerate(result["dealers"]):
            dealer["source"].update(discovery_id=id_, candidate_id=f"candidate_{index+1}")
            dealer.setdefault("found_by", "fo")
        fallback = json.loads(row["fallback"]) if row["fallback"] else None
        search = {"primary": "fo", "fallback_allowed": bool(row["allow_claude"]), "fallback_used": fallback is not None,
                  "fallback_reason": fallback and fallback["reason"], "fallback_error": fallback and fallback["error"],
                  "fo_status": fallback["fo_status"] if fallback else row["status"]}
        return {"id": id_, "mode": self.mode, "status": row["status"], "progress": messages[row["status"]],
                "zip": req["zip"], "radius_miles": req["radius_miles"], "created_at": row["created"],
                "updated_at": row["updated"], "error": row["error"], "search": search, **result}


def thin(row):
    """True when a finished Fo search left fewer than MIN_DEALERS verified dealers (or none at all)."""
    if row["status"] != "complete":
        return True
    return len(json.loads(row["result"] or '{"dealers": []}')["dealers"]) < MIN_DEALERS
