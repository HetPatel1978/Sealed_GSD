"""Local inbox staging, durable deduplication and explicit consumption.

Fetching a reply never marks it consumed. A later parser/engine transaction must
commit its own durable result before acknowledging this evidence reference.
"""

import json
import sqlite3
from pathlib import Path

from sealed.ingest.mail import MailMessage, Match, staged_record


class InboxStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS inbox (
            account TEXT NOT NULL, gmail_id TEXT NOT NULL, status TEXT NOT NULL,
            record TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account, gmail_id))""")
        self.db.commit()

    def stage(self, account: str, message: MailMessage, match: Match) -> bool:
        if match.status not in {"ready", "review"}:
            raise ValueError("Only matched or reviewable replies may enter the inbox.")
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO inbox (account, gmail_id, status, record) VALUES (?,?,?,?)",
                                     (account.lower(), message.gmail_id, match.status,
                                      json.dumps(staged_record(message, match))))
        return cursor.rowcount == 1

    def pending(self, account: str, *, status: str = "ready") -> list[dict]:
        rows = self.db.execute("SELECT record FROM inbox WHERE account=? AND status=? ORDER BY created_at, rowid",
                               (account.lower(), status))
        return [json.loads(row[0]) for row in rows]

    def get(self, account: str, gmail_id: str) -> tuple[str, dict] | None:
        row = self.db.execute("SELECT status, record FROM inbox WHERE account=? AND gmail_id=?",
                             (account.lower(), gmail_id)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def acknowledge(self, account: str, gmail_id: str) -> bool:
        with self.db:
            cursor = self.db.execute("UPDATE inbox SET status='consumed' WHERE account=? AND gmail_id=? AND status='ready'",
                                     (account.lower(), gmail_id))
        return cursor.rowcount == 1

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
