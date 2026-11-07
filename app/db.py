"""SQLite persistence.

Lesson content lives in an append-only `versions` table: nothing overwrites
an earlier version, so a failed step or a retry cannot destroy instructor work.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import get_settings

STEPS = ["research", "generate", "qc_factual", "qc_instructional"]
_artifact_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    lesson_id INTEGER NOT NULL REFERENCES lessons(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    detail TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (lesson_id, name)
);
CREATE TABLE IF NOT EXISTS versions (
    lesson_id INTEGER NOT NULL REFERENCES lessons(id),
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    note TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (lesson_id, version)
);
CREATE TABLE IF NOT EXISTS artifacts (
    lesson_id INTEGER NOT NULL REFERENCES lessons(id),
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (lesson_id, name)
);
"""


class VersionConflict(Exception):
    def __init__(self, latest: int):
        super().__init__(f"Lesson has moved on to version {latest}")
        self.latest = latest


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # A step still "running" at startup died with the previous process.
        conn.execute(
            "UPDATE steps SET status='failed', error=?, updated_at=? WHERE status='running'",
            ("Interrupted because the server stopped. Saved work is intact; retry this step.", now()),
        )


# ---- lessons -----------------------------------------------------------
def create_lesson(title: str, objective: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO lessons (title, objective, created_at) VALUES (?,?,?)",
            (title, objective, now()),
        )
        lesson_id = int(cur.lastrowid)
        for name in STEPS:
            conn.execute("INSERT INTO steps (lesson_id, name) VALUES (?,?)", (lesson_id, name))
    return lesson_id


def get_lesson(lesson_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    return dict(row) if row else None


def list_lessons() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT l.*, COALESCE(MAX(v.version), 0) AS latest_version
               FROM lessons l LEFT JOIN versions v ON v.lesson_id = l.id
               GROUP BY l.id ORDER BY l.id DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


# ---- steps -------------------------------------------------------------
def get_steps(lesson_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM steps WHERE lesson_id=?", (lesson_id,)).fetchall()
    by_name = {r["name"]: dict(r) for r in rows}
    return [by_name[name] for name in STEPS if name in by_name]


def set_step(lesson_id: int, name: str, status: str, *, error: str | None = None,
             detail: str | None = None, bump: bool = False) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE steps SET status=?, error=?, detail=COALESCE(?, detail),
               attempts=attempts+?, updated_at=? WHERE lesson_id=? AND name=?""",
            (status, error, detail, 1 if bump else 0, now(), lesson_id, name),
        )


# ---- versions ----------------------------------------------------------
def latest_version(lesson_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM versions WHERE lesson_id=? ORDER BY version DESC LIMIT 1", (lesson_id,)
        ).fetchone()
    return _version(row)


def get_version(lesson_id: int, version: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM versions WHERE lesson_id=? AND version=?", (lesson_id, version)
        ).fetchone()
    return _version(row)


def list_versions(lesson_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT version, note, author, created_at FROM versions WHERE lesson_id=? ORDER BY version DESC",
            (lesson_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_version(lesson_id: int, content: dict, note: str, author: str,
                 base_version: int | None = None) -> int:
    """Append a version. With base_version set, refuse if the lesson moved on."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM versions WHERE lesson_id=?", (lesson_id,)
            ).fetchone()
            latest = int(row["v"])
            if base_version is not None and base_version != latest:
                raise VersionConflict(latest)
            conn.execute(
                "INSERT INTO versions (lesson_id, version, content, note, author, created_at) VALUES (?,?,?,?,?,?)",
                (lesson_id, latest + 1, json.dumps(content), note, author, now()),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return latest + 1


def has_instructor_edits(lesson_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM versions WHERE lesson_id=? AND author='instructor' LIMIT 1", (lesson_id,)
        ).fetchone()
    return row is not None


def _version(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    data["content"] = json.loads(data["content"])
    return data


# ---- artifacts (research output, QC reports) ----------------------------
def put_artifact(lesson_id: int, name: str, content: Any) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO artifacts (lesson_id, name, content, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(lesson_id, name) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
            (lesson_id, name, json.dumps(content), now()),
        )


def get_artifact(lesson_id: int, name: str) -> Any | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT content FROM artifacts WHERE lesson_id=? AND name=?", (lesson_id, name)
        ).fetchone()
    return json.loads(row["content"]) if row else None


@contextmanager
def edit_artifact(lesson_id: int, name: str) -> Iterator[Any]:
    """Read-modify-write an artifact without losing a concurrent update."""
    with _artifact_lock:
        data = get_artifact(lesson_id, name)
        yield data
        if data is not None:
            put_artifact(lesson_id, name, data)
