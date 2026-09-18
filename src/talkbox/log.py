"""SQLite storage.

- `DailyCounter`: questions per day, for the daily cap. Stores counts only, no text.
- `ExchangeLog`: guardrail layer 7 (storage half), full exchange records. Off by
  default for now (talkbox.toml `[logging] enabled`), see PLAN.md D8.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc           TEXT    NOT NULL,  -- ISO 8601, UTC
    local_date       TEXT    NOT NULL,  -- YYYY-MM-DD in the policy timezone (daily cap)
    question         TEXT    NOT NULL,
    answer           TEXT    NOT NULL,
    answered_by      TEXT    NOT NULL,  -- 'model' | 'canned'
    policy_version   TEXT    NOT NULL,
    provider         TEXT,
    model            TEXT,
    latency_ms       INTEGER NOT NULL,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    steps_json       TEXT    NOT NULL   -- [{"step": ..., "decision": ..., "detail": ...}, ...]
);
CREATE INDEX IF NOT EXISTS idx_exchanges_local_date ON exchanges(local_date);
"""


def _connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


class DailyCounter:
    def __init__(self, path: str | Path) -> None:
        self._conn = _connect(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS daily_counts (local_date TEXT PRIMARY KEY, count INTEGER NOT NULL)"
        )

    def get(self, local_date: str) -> int:
        row = self._conn.execute(
            "SELECT count FROM daily_counts WHERE local_date = ?", (local_date,)
        ).fetchone()
        return row[0] if row else 0

    def increment(self, local_date: str) -> None:
        self._conn.execute(
            """INSERT INTO daily_counts (local_date, count) VALUES (?, 1)
               ON CONFLICT(local_date) DO UPDATE SET count = count + 1""",
            (local_date,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


@dataclass
class ExchangeRecord:
    ts_utc: datetime
    local_date: str
    question: str
    answer: str
    answered_by: str
    policy_version: str
    provider: str | None
    model: str | None
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    steps: list[dict]


class ExchangeLog:
    def __init__(self, path: str | Path) -> None:
        self._conn = _connect(path)
        self._conn.executescript(SCHEMA)

    def record(self, r: ExchangeRecord) -> int:
        cur = self._conn.execute(
            """INSERT INTO exchanges (ts_utc, local_date, question, answer, answered_by,
                   policy_version, provider, model, latency_ms, input_tokens, output_tokens,
                   steps_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r.ts_utc.isoformat(), r.local_date, r.question, r.answer, r.answered_by,
                r.policy_version, r.provider, r.model, r.latency_ms, r.input_tokens,
                r.output_tokens, json.dumps(r.steps),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def recent(self, n: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM exchanges ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()

    def close(self) -> None:
        self._conn.close()
