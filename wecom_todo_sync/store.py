"""SQLite 归档与同步状态存储。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import Todo, utc_now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    source_id     TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open',
    due_ms        INTEGER,
    created_ms    INTEGER,
    updated_ms    INTEGER,
    owner         TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL DEFAULT '',
    raw_json      TEXT NOT NULL DEFAULT '{}',
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status);
CREATE INDEX IF NOT EXISTS idx_todos_last_seen ON todos(last_seen_ms);

CREATE TABLE IF NOT EXISTS sync_map (
    source_id       TEXT PRIMARY KEY,
    feishu_task_guid TEXT,
    synced_hash     TEXT,
    completed_synced INTEGER NOT NULL DEFAULT 0,
    synced_at_ms    INTEGER,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_ms  INTEGER NOT NULL,
    finished_at_ms INTEGER,
    command      TEXT NOT NULL DEFAULT '',
    collected    INTEGER NOT NULL DEFAULT 0,
    created      INTEGER NOT NULL DEFAULT 0,
    updated      INTEGER NOT NULL DEFAULT 0,
    completed    INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reverse_map (
    feishu_task_guid TEXT PRIMARY KEY,
    wecom_todo_id    TEXT,
    pushed_hash      TEXT,
    finished_synced  INTEGER NOT NULL DEFAULT 0,
    synced_at_ms     INTEGER,
    last_error       TEXT
);

CREATE TABLE IF NOT EXISTS meeting_map (
    source_id    TEXT PRIMARY KEY,
    meeting_id   TEXT,
    subject      TEXT,
    todo_id      TEXT,
    pushed_hash  TEXT,
    synced_at_ms INTEGER,
    last_error   TEXT
);
"""


class Store:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """老库补列（SQLite 的 ADD COLUMN 是幂等的，这里先检查再执行）。"""
        columns = [row["name"] for row in self.conn.execute("PRAGMA table_info(reverse_map)")]
        if columns and "finished_synced" not in columns:
            self.conn.execute(
                "ALTER TABLE reverse_map ADD COLUMN finished_synced INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -------------------------------------------------------------- todos

    def upsert_todo(self, todo: Todo) -> str:
        """写入/更新一条待办，返回 'created' 或 'updated'。"""
        now = utc_now_ms()
        digest = todo.content_hash()
        row = self.conn.execute(
            "SELECT content_hash FROM todos WHERE source_id = ?", (todo.source_id,)
        ).fetchone()

        if row is None:
            with self._tx() as conn:
                conn.execute(
                    """
                    INSERT INTO todos (source_id, title, description, status, due_ms,
                                       created_ms, updated_ms, owner, content_hash,
                                       raw_json, first_seen_ms, last_seen_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        todo.source_id,
                        todo.title,
                        todo.description,
                        todo.status,
                        todo.due_ms,
                        todo.created_ms,
                        todo.updated_ms,
                        todo.owner,
                        digest,
                        json.dumps(todo.raw, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            return "created"

        changed = row["content_hash"] != digest
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE todos SET title = ?, description = ?, status = ?, due_ms = ?,
                                 created_ms = ?, updated_ms = ?, owner = ?,
                                 content_hash = ?, raw_json = ?, last_seen_ms = ?
                WHERE source_id = ?
                """,
                (
                    todo.title,
                    todo.description,
                    todo.status,
                    todo.due_ms,
                    todo.created_ms,
                    todo.updated_ms,
                    todo.owner,
                    digest,
                    json.dumps(todo.raw, ensure_ascii=False),
                    now,
                    todo.source_id,
                ),
            )
        return "updated" if changed else "unchanged"

    def get_todo(self, source_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM todos WHERE source_id = ?", (source_id,)
        ).fetchone()

    def iter_todos(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM todos"
        params: List[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY COALESCE(due_ms, 9007199254740991), source_id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return list(self.conn.execute(sql, params))

    def known_todo_ids(self, limit: int = 500) -> List[str]:
        """本地已知的待办 id（用于 get 补状态）。

        带 "#" 的是子项展开出来的合成 id（如 T-1001#a1），不是真实 todo_id，要排除。
        """
        rows = self.conn.execute(
            """
            SELECT source_id FROM todos
            WHERE source_id NOT LIKE '%#%'
            ORDER BY last_seen_ms DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [row["source_id"] for row in rows]

    def counts(self) -> Dict[str, int]:
        total = self.conn.execute("SELECT COUNT(*) AS c FROM todos").fetchone()["c"]
        open_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM todos WHERE status = 'open'"
        ).fetchone()["c"]
        synced = self.conn.execute(
            "SELECT COUNT(*) AS c FROM sync_map WHERE feishu_task_guid IS NOT NULL"
        ).fetchone()["c"]
        return {"total": total, "open": open_count, "synced": synced}

    # ----------------------------------------------------------- sync_map

    def get_mapping(self, source_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sync_map WHERE source_id = ?", (source_id,)
        ).fetchone()

    def set_mapping(
        self,
        source_id: str,
        feishu_task_guid: Optional[str],
        synced_hash: Optional[str],
        completed_synced: bool = False,
        error: Optional[str] = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO sync_map (source_id, feishu_task_guid, synced_hash,
                                      completed_synced, synced_at_ms, last_error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    feishu_task_guid = excluded.feishu_task_guid,
                    synced_hash = excluded.synced_hash,
                    completed_synced = excluded.completed_synced,
                    synced_at_ms = excluded.synced_at_ms,
                    last_error = excluded.last_error
                """,
                (
                    source_id,
                    feishu_task_guid,
                    synced_hash,
                    1 if completed_synced else 0,
                    utc_now_ms(),
                    error,
                ),
            )

    def record_sync_error(self, source_id: str, error: str) -> None:
        """只记录错误，不动已同步的 guid/hash。"""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO sync_map (source_id, last_error, synced_at_ms)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_error = excluded.last_error,
                    synced_at_ms = excluded.synced_at_ms
                """,
                (source_id, error, utc_now_ms()),
            )

    def failed_syncs(self, limit: int = 20) -> List[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT source_id, feishu_task_guid, last_error, synced_at_ms
                FROM sync_map WHERE last_error IS NOT NULL AND last_error != ''
                ORDER BY synced_at_ms DESC LIMIT ?
                """,
                (limit,),
            )
        )

    # ---------------------------------------------------------- reverse_map

    def get_mapping_by_guid(self, feishu_task_guid: str) -> Optional[sqlite3.Row]:
        """按飞书任务 guid 查前向映射（用来判断这个任务是"我们建的"）。"""
        return self.conn.execute(
            "SELECT * FROM sync_map WHERE feishu_task_guid = ?", (feishu_task_guid,)
        ).fetchone()

    def is_reverse_created(self, source_id: str) -> bool:
        """这个企业微信待办是不是反向同步（飞书 -> 企业微信）建出来的。"""
        row = self.conn.execute(
            "SELECT 1 FROM reverse_map WHERE wecom_todo_id = ? LIMIT 1", (source_id,)
        ).fetchone()
        return row is not None

    def get_reverse_mapping(self, feishu_task_guid: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reverse_map WHERE feishu_task_guid = ?", (feishu_task_guid,)
        ).fetchone()

    def set_reverse_mapping(
        self,
        feishu_task_guid: str,
        wecom_todo_id: Optional[str],
        pushed_hash: Optional[str],
        finished_synced: bool = False,
        error: Optional[str] = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO reverse_map (feishu_task_guid, wecom_todo_id, pushed_hash,
                                         finished_synced, synced_at_ms, last_error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(feishu_task_guid) DO UPDATE SET
                    wecom_todo_id = excluded.wecom_todo_id,
                    pushed_hash = excluded.pushed_hash,
                    finished_synced = MAX(reverse_map.finished_synced, excluded.finished_synced),
                    synced_at_ms = excluded.synced_at_ms,
                    last_error = excluded.last_error
                """,
                (
                    feishu_task_guid,
                    wecom_todo_id,
                    pushed_hash,
                    1 if finished_synced else 0,
                    utc_now_ms(),
                    error,
                ),
            )

    def reverse_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM reverse_map").fetchone()
        return int(row["c"])

    # ---------------------------------------------------------- meeting_map

    def get_meeting_mapping(self, source_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM meeting_map WHERE source_id = ?", (source_id,)
        ).fetchone()

    def set_meeting_mapping(
        self,
        source_id: str,
        meeting_id: str,
        subject: str,
        todo_id: Optional[str],
        pushed_hash: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO meeting_map (source_id, meeting_id, subject, todo_id,
                                         pushed_hash, synced_at_ms, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    meeting_id = excluded.meeting_id,
                    subject = excluded.subject,
                    todo_id = excluded.todo_id,
                    pushed_hash = excluded.pushed_hash,
                    synced_at_ms = excluded.synced_at_ms,
                    last_error = excluded.last_error
                """,
                (source_id, meeting_id, subject, todo_id, pushed_hash, utc_now_ms(), error),
            )

    def meeting_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM meeting_map").fetchone()
        return int(row["c"])

    # --------------------------------------------------------------- runs

    def start_run(self, command: str) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (started_at_ms, command) VALUES (?, ?)",
                (utc_now_ms(), command),
            )
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, stats: Dict[str, int], note: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE runs SET finished_at_ms = ?, collected = ?, created = ?,
                                updated = ?, completed = ?, skipped = ?, failed = ?,
                                note = ?
                WHERE id = ?
                """,
                (
                    utc_now_ms(),
                    stats.get("collected", 0),
                    stats.get("created", 0),
                    stats.get("updated", 0),
                    stats.get("completed", 0),
                    stats.get("skipped", 0),
                    stats.get("failed", 0),
                    note,
                    run_id,
                ),
            )

    def recent_runs(self, limit: int = 5) -> List[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            )
        )
