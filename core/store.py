"""Tiny SQLite state store for restart-safe autopilot: a key-value table and an append-only decisions log."""
import json
import sqlite3
import time
from pathlib import Path


class Store:
    """One SQLite file holding autopilot's resumable state and its decision history."""

    def __init__(self, path: str | Path = "state.db"):
        self._conn = sqlite3.connect(path)
        self._conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS decisions (ts REAL, job TEXT, data TEXT)")
        self._conn.commit()

    def get(self, key: str, default=None):
        """Reads one JSON-decoded value by key."""
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        """Writes one JSON-encoded value by key, overwriting any existing one."""
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self._conn.commit()

    def log_decision(self, job: str, data: dict, ts: float | None = None):
        """Appends one row to the decisions log; existing rows are never changed or deleted."""
        self._conn.execute("INSERT INTO decisions (ts, job, data) VALUES (?, ?, ?)", (ts if ts is not None else time.time(), job, json.dumps(data)))
        self._conn.commit()

    def decisions(self, job: str | None = None, limit: int = 100) -> list[dict]:
        """The most recent logged decisions, newest first."""
        if job:
            rows = self._conn.execute("SELECT ts, job, data FROM decisions WHERE job = ? ORDER BY ts DESC LIMIT ?", (job, limit)).fetchall()
        else:
            rows = self._conn.execute("SELECT ts, job, data FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": row_ts, "job": row_job, "data": json.loads(data)} for row_ts, row_job, data in rows]

    def close(self):
        """Closes the underlying SQLite connection."""
        self._conn.close()
