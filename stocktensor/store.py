"""SQLite state for a validator: tasks through their lifecycle and the bundle chain.

Lifecycle: ``pending`` (queried, waiting for the horizon) → ``resolved``
(realised price known or void, scored) → ``bundled`` (published in an epoch).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .bundle import ZERO_HASH, TaskRecord
from .protocol import HORIZONS
from .scoring import WINDOW_SECONDS, TaskResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id    TEXT PRIMARY KEY,
    asset      TEXT NOT NULL,
    horizon    TEXT NOT NULL,
    as_of      INTEGER NOT NULL,
    session    TEXT NOT NULL,
    feed       TEXT NOT NULL,
    reference  TEXT NOT NULL,
    responses  TEXT NOT NULL,
    realised   TEXT,
    void       TEXT,
    scores     TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
    epoch      INTEGER
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, as_of);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- tasks -------------------------------------------------------------

    def add_task(self, record: TaskRecord) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO tasks"
            " (task_id, asset, horizon, as_of, session, feed, reference, responses)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.task_id,
                record.asset,
                record.horizon,
                record.as_of,
                record.session,
                record.feed,
                json.dumps(record.reference),
                json.dumps(record.responses),
            ),
        )
        self.db.commit()

    def due_tasks(self, now: int, grace: int = 0) -> list[TaskRecord]:
        """Pending tasks whose horizon (plus ``grace`` seconds) has passed."""
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status = 'pending' ORDER BY as_of, task_id"
        ).fetchall()
        return [self._record(r) for r in rows if r["as_of"] + HORIZONS[r["horizon"]] + grace <= now]

    def resolve(self, task_id: str, realised: dict[str, Any] | None, void: str | None, scores: dict) -> None:
        self.db.execute(
            "UPDATE tasks SET realised = ?, void = ?, scores = ?, status = 'resolved' WHERE task_id = ?",
            (json.dumps(realised) if realised is not None else None, void, json.dumps(scores), task_id),
        )
        self.db.commit()

    def resolved_tasks(self) -> list[TaskRecord]:
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status = 'resolved' ORDER BY as_of, task_id"
        ).fetchall()
        return [self._record(r) for r in rows]

    def mark_bundled(self, task_ids: list[str], epoch: int) -> None:
        self.db.executemany(
            "UPDATE tasks SET status = 'bundled', epoch = ? WHERE task_id = ?", [(epoch, t) for t in task_ids]
        )
        self.db.commit()

    def history(self, now: int) -> list[TaskResult]:
        """Scored, bundled tasks inside the rolling window."""
        rows = self.db.execute(
            "SELECT task_id, as_of, scores FROM tasks WHERE status = 'bundled' AND void IS NULL"
            " AND realised IS NOT NULL AND as_of >= ? ORDER BY as_of, task_id",
            (now - WINDOW_SECONDS,),
        ).fetchall()
        return [TaskResult(r["task_id"], r["as_of"], json.loads(r["scores"])) for r in rows if r["scores"]]

    def count(self, status: str) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (status,)).fetchone()[0])

    @staticmethod
    def _record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            asset=row["asset"],
            horizon=row["horizon"],
            as_of=row["as_of"],
            session=row["session"],
            feed=row["feed"],
            reference=json.loads(row["reference"]),
            realised=json.loads(row["realised"]) if row["realised"] else None,
            void=row["void"],
            responses=json.loads(row["responses"]),
            scores=json.loads(row["scores"]) if row["scores"] else {},
        )

    # --- bundle chain ------------------------------------------------------

    def _get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
            (key, value, value),
        )
        self.db.commit()

    def chain_head(self) -> tuple[int, str]:
        """(last epoch, last bundle hash); (0, zeros) before the first bundle."""
        return int(self._get("epoch") or 0), self._get("hash") or ZERO_HASH

    def set_chain_head(self, epoch: int, digest: str) -> None:
        self._set("epoch", str(epoch))
        self._set("hash", digest)
