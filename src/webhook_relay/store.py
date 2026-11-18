"""SQLite storage for deliveries and their per-target jobs.

A *delivery* is one webhook request that passed verification. For every target
of its route a *job* row tracks attempts, the next retry time and the outcome:
``pending``, ``succeeded`` or ``dead`` (the dead-letter list).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    route TEXT NOT NULL,
    received_at REAL NOT NULL,
    headers TEXT NOT NULL,
    body BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS deliveries_route_received ON deliveries (route, received_at);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL REFERENCES deliveries (id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL,
    last_status_code INTEGER,
    last_error TEXT,
    replays INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    UNIQUE (delivery_id, target)
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs (status, next_attempt_at);
"""

# Headers that are never stored (credentials of the sender or hop-by-hop).
DROP_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "upgrade",
        "content-length",
        "host",
    }
)

STATUS_SQL = """
CASE
    WHEN SUM(j.status = 'pending') > 0 THEN 'pending'
    WHEN SUM(j.status = 'dead') > 0 THEN 'dead'
    ELSE 'delivered'
END
"""


@dataclass(frozen=True)
class Job:
    id: int
    delivery_id: str
    target: str
    url: str
    status: str
    attempts: int
    next_attempt_at: float | None
    last_status_code: int | None
    last_error: str | None
    replays: int
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Delivery:
    id: str
    route: str
    received_at: float
    headers: dict[str, str]
    body: bytes
    status: str

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "route": self.route,
            "received_at": self.received_at,
            "status": self.status,
            "size": len(self.body),
        }


def filter_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers:
        k = key.lower()
        if k not in DROP_HEADERS:
            out[k] = value
    return out


class Store:
    """A small thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def ping(self) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1").fetchone()[0] == 1

    # -- writes -----------------------------------------------------------

    def add_delivery(
        self,
        route: str,
        headers: Mapping[str, str],
        body: bytes,
        targets: Iterable[tuple[str, str]],
        now: float,
    ) -> str:
        delivery_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute(
                    "INSERT INTO deliveries (id, route, received_at, headers, body) VALUES (?, ?, ?, ?, ?)",
                    (delivery_id, route, now, json.dumps(dict(headers)), body),
                )
                self._db.executemany(
                    "INSERT INTO jobs (delivery_id, target, url, status, next_attempt_at, updated_at)"
                    " VALUES (?, ?, ?, 'pending', ?, ?)",
                    [(delivery_id, name, url, now, now) for name, url in targets],
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return delivery_id

    def record_success(self, job_id: int, status_code: int, now: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = 'succeeded', attempts = attempts + 1, next_attempt_at = NULL,"
                " last_status_code = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (status_code, now, job_id),
            )

    def record_failure(
        self,
        job_id: int,
        status_code: int | None,
        error: str,
        now: float,
        next_attempt_at: float | None,
    ) -> None:
        """Count a failed attempt; ``next_attempt_at=None`` moves the job to the dead-letter list."""
        status = "pending" if next_attempt_at is not None else "dead"
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = ?, attempts = attempts + 1, next_attempt_at = ?,"
                " last_status_code = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, next_attempt_at, status_code, error[:500], now, job_id),
            )

    def replay(self, delivery_id: str, now: float, target: str | None = None) -> int:
        """Queue a delivery again (all targets or one). Returns the number of jobs queued."""
        sql = (
            "UPDATE jobs SET status = 'pending', attempts = 0, next_attempt_at = ?, last_error = NULL,"
            " replays = replays + 1, updated_at = ? WHERE delivery_id = ? AND status != 'pending'"
        )
        params: list[Any] = [now, now, delivery_id]
        if target is not None:
            sql += " AND target = ?"
            params.append(target)
        with self._lock:
            return self._db.execute(sql, params).rowcount

    def replay_dead(self, now: float, route: str | None = None) -> int:
        sql = (
            "UPDATE jobs SET status = 'pending', attempts = 0, next_attempt_at = ?, last_error = NULL,"
            " replays = replays + 1, updated_at = ? WHERE status = 'dead'"
        )
        params: list[Any] = [now, now]
        if route is not None:
            sql += " AND delivery_id IN (SELECT id FROM deliveries WHERE route = ?)"
            params.append(route)
        with self._lock:
            return self._db.execute(sql, params).rowcount

    # -- reads ------------------------------------------------------------

    def due_jobs(self, now: float, limit: int = 50) -> list[Job]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE status = 'pending' AND next_attempt_at <= ?"
                " ORDER BY next_attempt_at, id LIMIT ?",
                (now, limit),
            ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def next_due_at(self) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT MIN(next_attempt_at) FROM jobs WHERE status = 'pending'").fetchone()
        return row[0]

    def get_delivery(self, delivery_id: str) -> Delivery | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT d.*, {STATUS_SQL} AS status FROM deliveries d JOIN jobs j ON j.delivery_id = d.id"
                " WHERE d.id = ? GROUP BY d.id",
                (delivery_id,),
            ).fetchone()
        if row is None:
            return None
        return Delivery(
            id=row["id"],
            route=row["route"],
            received_at=row["received_at"],
            headers=json.loads(row["headers"]),
            body=bytes(row["body"]),
            status=row["status"],
        )

    def jobs_for(self, delivery_id: str) -> list[Job]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs WHERE delivery_id = ? ORDER BY id", (delivery_id,)).fetchall()
        return [Job(**dict(r)) for r in rows]

    def get_job(self, job_id: int) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job(**dict(row)) if row else None

    def list_deliveries(
        self,
        route: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if route:
            where.append("d.route = ?")
            params.append(route)
        sql = (
            f"SELECT d.id, d.route, d.received_at, LENGTH(d.body) AS size, {STATUS_SQL} AS status"
            " FROM deliveries d JOIN jobs j ON j.delivery_id = d.id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY d.id"
        if status:
            sql += " HAVING status = ?"
            params.append(status)
        sql += " ORDER BY d.received_at DESC, d.id LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def dead_letters(self, route: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        sql = (
            "SELECT j.*, d.route, d.received_at FROM jobs j JOIN deliveries d ON d.id = j.delivery_id"
            " WHERE j.status = 'dead'"
        )
        params: list[Any] = []
        if route:
            sql += " AND d.route = ?"
            params.append(route)
        sql += " ORDER BY j.updated_at DESC, j.id LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        out = {"pending": 0, "succeeded": 0, "dead": 0}
        out.update({r[0]: r[1] for r in rows})
        return out
