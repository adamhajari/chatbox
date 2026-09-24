"""SQLite storage.

- `DailyCounter`: questions per day, for the daily cap. Stores counts only, no text.
- `Controls`: switches a parent flips from the settings page (pause). Kept in the database
  so a restart doesn't quietly undo them.
- `ExchangeLog`: guardrail layer 7 (storage half), full exchange records. Off by
  default for now (talkbox.toml `[logging] enabled`), see PLAN.md D8.
"""

from __future__ import annotations

import json
import sqlite3
import threading
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
    # The settings page reads and writes from its own thread; each class serializes
    # access with a lock.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class DailyCounter:
    def __init__(self, path: str | Path) -> None:
        self._conn = _connect(path)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS daily_counts (local_date TEXT PRIMARY KEY, count INTEGER NOT NULL)"
        )

    def get(self, local_date: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM daily_counts WHERE local_date = ?", (local_date,)
            ).fetchone()
        return row[0] if row else 0

    def increment(self, local_date: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO daily_counts (local_date, count) VALUES (?, 1)
                   ON CONFLICT(local_date) DO UPDATE SET count = count + 1""",
                (local_date,),
            )
            self._conn.commit()

    def reset(self, local_date: str) -> None:
        """Start the day's count over (a parent's "reset" button)."""
        with self._lock:
            self._conn.execute("DELETE FROM daily_counts WHERE local_date = ?", (local_date,))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class Controls:
    def __init__(self, path: str | Path) -> None:
        self._conn = _connect(path)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS controls (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    @property
    def paused(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT value FROM controls WHERE name = 'paused'").fetchone()
        return bool(row and row[0] == "1")

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO controls (name, value) VALUES ('paused', ?)
                   ON CONFLICT(name) DO UPDATE SET value = excluded.value""",
                ("1" if paused else "0",),
            )
            self._conn.commit()

    @property
    def volume(self) -> int:
        """0-100. Device state, not policy: it belongs to this box in this room, not to
        the rules a parent sets, so it lives here rather than in the policy file."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM controls WHERE name = 'volume'").fetchone()
        if row is None:
            return 100
        try:
            return max(0, min(100, int(row[0])))
        except ValueError:
            return 100

    def set_volume(self, percent: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO controls (name, value) VALUES ('volume', ?)
                   ON CONFLICT(name) DO UPDATE SET value = excluded.value""",
                (str(max(0, min(100, int(percent)))),),
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
        self._lock = threading.Lock()
        self._conn.executescript(SCHEMA)

    def record(self, r: ExchangeRecord) -> int:
        with self._lock:
            return self._record(r)

    def _record(self, r: ExchangeRecord) -> int:
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
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM exchanges ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()

    def for_date(self, local_date: str) -> list[sqlite3.Row]:
        """One day's exchanges (in the policy's timezone), newest first."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM exchanges WHERE local_date = ? ORDER BY id DESC", (local_date,)
            ).fetchall()

    def close(self) -> None:
        self._conn.close()
