from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from nocturne.models import Task, TaskStatus

_SCHEMA_SQL = Path(__file__).with_name("_schema.sql").read_text(encoding="utf-8")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path) -> None:
        db_path = str(path)
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        _ = self._conn.execute("PRAGMA busy_timeout = 5000")
        if db_path != ":memory:":
            _ = self._conn.execute("PRAGMA journal_mode = WAL")
        _ = self._conn.execute("PRAGMA foreign_keys = ON")
        _ = self._conn.executescript(_SCHEMA_SQL)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def insert_task(self, task: Task) -> None:
        row = task.model_dump(mode="json")
        with self._conn:
            _ = self._conn.execute(
                "INSERT INTO tasks ("
                + "id, status, created_at, updated_at, repo_slug, checkout_path, "
                + "issue_number, title, body, base, verify_cmd, require_new_test, "
                + "coding_model, branch, attempts, pr_url, question, answer, opencode_pid"
                + ") VALUES ("
                + ":id, :status, :created_at, :updated_at, :repo_slug, :checkout_path, "
                + ":issue_number, :title, :body, :base, :verify_cmd, :require_new_test, "
                + ":coding_model, :branch, :attempts, :pr_url, :question, :answer, :opencode_pid"
                + ")",
                row,
            )

    def get_task(self, task_id: str) -> Task | None:
        db_row = cast(sqlite3.Row | None, self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        if db_row is None:
            return None
        row = db_row
        data: dict[str, object] = {key: row[key] for key in row.keys()}
        return Task.model_validate(data)

    def update_status(self, task_id: str, status: TaskStatus) -> None:
        with self._conn:
            _ = self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), task_id),
            )

    def update_pid(self, task_id: str, pid: int | None) -> None:
        with self._conn:
            _ = self._conn.execute(
                "UPDATE tasks SET opencode_pid = ?, updated_at = ? WHERE id = ?",
                (pid, _now(), task_id),
            )

    def update_pr_url(self, task_id: str, pr_url: str | None) -> None:
        with self._conn:
            _ = self._conn.execute(
                "UPDATE tasks SET pr_url = ?, updated_at = ? WHERE id = ?",
                (pr_url, _now(), task_id),
            )

    def increment_attempts(self, task_id: str) -> None:
        with self._conn:
            _ = self._conn.execute(
                "UPDATE tasks SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (_now(), task_id),
            )

    def list_by_status(self, status: TaskStatus) -> list[Task]:
        db_rows = cast(list[sqlite3.Row], self._conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at, id",
            (status,),
        ).fetchall())
        rows = db_rows
        return [Task.model_validate({key: row[key] for key in row.keys()}) for row in rows]

    def park_task(self, task_id: str, question: str) -> None:
        parked_at = _now()
        with self._conn:
            _ = self._conn.execute(
                "UPDATE tasks SET status = 'parked', question = ?, answer = NULL, updated_at = ? WHERE id = ?",
                (question, parked_at, task_id),
            )
            _ = self._conn.execute(
                "INSERT INTO parked_questions(task_id, question, parked_at) VALUES (?, ?, ?)",
                (task_id, question, parked_at),
            )

    def resume_task(self, task_id: str, answer: str) -> None:
        answered_at = _now()
        with self._conn:
            _ = self._conn.execute(
                "UPDATE tasks SET status = 'selected', answer = ?, updated_at = ? WHERE id = ?",
                (answer, answered_at, task_id),
            )
            _ = self._conn.execute(
                "UPDATE parked_questions SET answer = ?, answered_at = ? "
                + "WHERE id = (SELECT id FROM parked_questions WHERE task_id = ? "
                + "ORDER BY parked_at DESC, id DESC LIMIT 1)",
                (answer, answered_at, task_id),
            )

    def add_column_if_not_exists(self, table: str, column: str, sql_type: str) -> None:
        table_sql = _quote_identifier(table)
        column_sql = _quote_identifier(column)
        columns: list[sqlite3.Row] = self._conn.execute(f"PRAGMA table_info({table_sql})").fetchall()
        if any(row[1] == column for row in columns):
            return
        with self._conn:
            _ = self._conn.execute(f"ALTER TABLE {table_sql} ADD COLUMN {column_sql} {sql_type}")

    def start_run(self) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO runs(started_at) VALUES (?)",
                (_now(),),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("run insert did not return a rowid")
            return int(cursor.lastrowid)

    def end_run(self, run_id: int, summary: str, tokens: int) -> None:
        with self._conn:
            _ = self._conn.execute(
                "UPDATE runs SET ended_at = ?, summary = ?, tokens_used = ? WHERE id = ?",
                (_now(), summary, tokens, run_id),
            )

    def set_daemon_flag(self, key: str, value: str) -> None:
        with self._conn:
            _ = self._conn.execute(
                "INSERT OR REPLACE INTO daemon_state(key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, _now()),
            )

    def get_daemon_flag(self, key: str) -> str | None:
        db_row = cast(sqlite3.Row | None, self._conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone())
        if db_row is None:
            return None
        row = db_row
        return cast(str, row[0])

    def add_discord_message(self, msg_id: int, task_id: str) -> None:
        """Persist a Discord message ID → task ID mapping for reply correlation."""
        with self._conn:
            _ = self._conn.execute(
                "INSERT OR REPLACE INTO discord_messages (msg_id, task_id, created_at) VALUES (?, ?, ?)",
                (msg_id, task_id, _now()),
            )

    def get_discord_message_task(self, msg_id: int) -> str | None:
        """Look up the task ID associated with a Discord message ID. Returns None if not found."""
        db_row = cast(sqlite3.Row | None, self._conn.execute(
            "SELECT task_id FROM discord_messages WHERE msg_id = ?", (msg_id,)
        ).fetchone())
        if db_row is None:
            return None
        return cast(str, db_row[0])

    def close(self) -> None:
        self._conn.close()
