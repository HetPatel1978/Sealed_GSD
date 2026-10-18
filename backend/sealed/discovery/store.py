"""Short SQLite transactions; no database connection is held across Fo calls."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from sealed.discovery.models import DiscoveryError


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

    def create(self, request):
        encoded = request.model_dump_json()
        digest = hashlib.sha256(("fo-discovery-v1:" + self.mode + ":" + encoded).encode()).hexdigest()
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM discovery_jobs WHERE scope=? AND fingerprint=? ORDER BY created DESC LIMIT 1", (self.scope, digest)).fetchone()
            if row and (row["status"] != "complete" or now-row["updated"] < 3600):
                return row["id"], True
            id_ = "search_" + uuid4().hex[:16]
            db.execute("INSERT INTO discovery_jobs(id,scope,fingerprint,created,updated,request,status,phase) VALUES (?,?,?,?,?,?,?,?)",
                       (id_, self.scope, digest, now, now, encoded, "queued", "new"))
            return id_, False

    def get(self, id_):
        with self.connection() as db:
            row = db.execute("SELECT * FROM discovery_jobs WHERE id=? AND scope=?", (id_, self.scope)).fetchone()
        if not row:
            raise DiscoveryError("discovery_not_found")
        return dict(row)

    def update(self, id_, **fields):
        allowed = {"status", "phase", "conversation_id", "raw_reply", "result", "error", "polls"}
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
        result = json.loads(row["result"]) if row["result"] else {"dealers": [], "dropped": 0}
        for index, dealer in enumerate(result["dealers"]):
            dealer["source"].update(discovery_id=id_, candidate_id=f"candidate_{index+1}")
        return {"id": id_, "mode": self.mode, "status": row["status"], "progress": messages[row["status"]],
                "zip": req["zip"], "radius_miles": req["radius_miles"], "created_at": row["created"],
                "updated_at": row["updated"], "error": row["error"], **result}
