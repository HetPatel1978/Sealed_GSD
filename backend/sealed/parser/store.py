"""Durable parse cache. It never acknowledges an inbox item or invokes an engine."""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from sealed.parser.quote import ParseResult, ParserError

RESULT = TypeAdapter(ParseResult)


class ParseStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS quote_parses (
            account TEXT NOT NULL, gmail_id TEXT NOT NULL, input_digest TEXT NOT NULL,
            result TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account, gmail_id, input_digest))""")
        self.db.commit()

    def get(self, account: str, gmail_id: str, digest: str) -> ParseResult | None:
        row = self.db.execute("SELECT result FROM quote_parses WHERE account=? AND gmail_id=? AND input_digest=?",
            (account.casefold(), gmail_id, digest)).fetchone()
        if row is None:
            return None
        try:
            return RESULT.validate_json(row[0], strict=True)
        except (ValidationError, ValueError, TypeError):
            raise ParserError("parse_cache_invalid") from None

    def put(self, account: str, gmail_id: str, digest: str, result: ParseResult):
        encoded = json.dumps(asdict(result), allow_nan=False, sort_keys=True)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO quote_parses (account, gmail_id, input_digest, result) VALUES (?,?,?,?)",
                (account.casefold(), gmail_id, digest, encoded))

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
