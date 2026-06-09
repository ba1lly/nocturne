from __future__ import annotations

import importlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, cast

from nocturne.models import Task


class _Store(Protocol):
    conn: sqlite3.Connection

    def insert_task(self, task: Task) -> None: ...
    def get_task(self, task_id: str) -> Task | None: ...
    def update_status(self, task_id: str, status: str) -> None: ...
    def update_pid(self, task_id: str, pid: int | None) -> None: ...
    def update_pr_url(self, task_id: str, pr_url: str | None) -> None: ...
    def increment_attempts(self, task_id: str) -> None: ...
    def list_by_status(self, status: str) -> list[Task]: ...
    def park_task(self, task_id: str, question: str) -> None: ...
    def resume_task(self, task_id: str, answer: str) -> None: ...
    def add_column_if_not_exists(self, table: str, column: str, sql_type: str) -> None: ...
    def start_run(self) -> int: ...
    def end_run(self, run_id: int, summary: str, tokens: int) -> None: ...
    def set_daemon_flag(self, key: str, value: str) -> None: ...
    def get_daemon_flag(self, key: str) -> str | None: ...
    def close(self) -> None: ...


Store = cast(Callable[[str | Path], _Store], importlib.import_module("nocturne.store").Store)


def _now() -> datetime:
    return datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)


def _task(task_id: str, status: str = "selected", **overrides: object) -> Task:
    data: dict[str, object] = {
        "id": task_id,
        "repo_slug": "owner/repo",
        "checkout_path": f"/tmp/{task_id}",
        "issue_number": 1,
        "title": "Title",
        "body": "Body",
        "base": "main",
        "verify_cmd": "pytest -q",
        "require_new_test": True,
        "coding_model": "alibaba-coding-plan/qwen3-coder-plus",
        "branch": f"nocturne/{task_id}",
        "status": status,
        "attempts": 0,
        "pr_url": None,
        "question": None,
        "answer": None,
        "created_at": _now(),
        "updated_at": _now(),
        "opencode_pid": None,
    }
    data.update(overrides)
    return Task.model_validate(data)


def test_schema_creates_all_tables(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        rows = cast(list[sqlite3.Row], store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall())
        assert {row[0] for row in rows} >= {"tasks", "runs", "parked_questions", "daemon_state"}
    finally:
        store.close()


def test_busy_timeout_is_5000_in_memory() -> None:
    store = Store(":memory:")
    try:
        assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        store.close()


def test_foreign_keys_enabled() -> None:
    store = Store(":memory:")
    try:
        assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        store.close()


def test_insert_get_task_round_trips_identically(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        task = _task("r#1", pr_url="https://example.com/pr/1", question="Need input?", answer="Yes", opencode_pid=123)
        store.insert_task(task)

        assert store.get_task(task.id) == task
    finally:
        store.close()


def test_update_status_persists(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        task = _task("r#2")
        store.insert_task(task)

        store.update_status(task.id, "running")

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status == "running"
    finally:
        store.close()


def test_update_pid_persists_and_clears(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        task = _task("r#3")
        store.insert_task(task)

        store.update_pid(task.id, 4321)
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.opencode_pid == 4321

        store.update_pid(task.id, None)
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.opencode_pid is None
    finally:
        store.close()


def test_update_pr_url_persists_and_clears(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        task = _task("r#pr1")
        store.insert_task(task)

        store.update_pr_url(task.id, "https://github.com/owner/repo/pull/7")
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.pr_url == "https://github.com/owner/repo/pull/7"

        store.update_pr_url(task.id, None)
        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.pr_url is None
    finally:
        store.close()


def test_increment_attempts_is_atomic_and_monotonic(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        task = _task("r#att1", attempts=0)
        store.insert_task(task)

        store.increment_attempts(task.id)
        store.increment_attempts(task.id)
        store.increment_attempts(task.id)

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.attempts == 3
    finally:
        store.close()


def test_list_by_status_returns_only_matching_tasks(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        store.insert_task(_task("r#4", status="selected"))
        store.insert_task(_task("r#5", status="running"))
        store.insert_task(_task("r#6", status="selected"))

        tasks = store.list_by_status("selected")

        assert [task.id for task in tasks] == ["r#4", "r#6"]
    finally:
        store.close()


def test_park_task_sets_status_and_creates_parked_question_row(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        task = _task("r#7")
        store.insert_task(task)

        store.park_task(task.id, "Need more info")

        parked_row = cast(
            sqlite3.Row | None,
            store.conn.execute(
                "SELECT task_id, question, parked_at, answer, answered_at FROM parked_questions WHERE task_id = ?",
                (task.id,),
            ).fetchone(),
        )
        assert parked_row is not None
        parked = parked_row

        updated = store.get_task(task.id)
        assert updated is not None
        assert updated.status == "parked"
        assert updated.question == "Need more info"
        assert parked[0] == task.id
        assert parked[1] == "Need more info"
        assert parked[2]
        assert parked[3] is None
        assert parked[4] is None
    finally:
        store.close()


def test_resume_task_sets_selected_and_fills_answer_and_answered_at(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        task = _task("r#8")
        store.insert_task(task)
        store.park_task(task.id, "Need more info")

        store.resume_task(task.id, "Here you go")

        updated = store.get_task(task.id)
        assert updated is not None
        parked_row = cast(
            sqlite3.Row | None,
            store.conn.execute(
                "SELECT answer, answered_at FROM parked_questions WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task.id,),
            ).fetchone(),
        )
        assert parked_row is not None
        parked = parked_row

        assert updated.status == "selected"
        assert updated.answer == "Here you go"
        assert parked[0] == "Here you go"
        assert parked[1]
    finally:
        store.close()


def test_add_column_if_not_exists_is_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        store.add_column_if_not_exists("tasks", "opencode_pid", "INTEGER")
        store.add_column_if_not_exists("tasks", "opencode_pid", "INTEGER")
    finally:
        store.close()


def test_add_column_if_not_exists_adds_missing_column(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        store.add_column_if_not_exists("runs", "notes", "TEXT")

        columns = [row[1] for row in cast(list[sqlite3.Row], store.conn.execute("PRAGMA table_info(runs)").fetchall())]
        assert "notes" in columns
    finally:
        store.close()


def test_start_and_end_run_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        run_id = store.start_run()
        store.end_run(run_id, "done", 123)

        row_data = cast(
            sqlite3.Row | None,
            store.conn.execute(
                "SELECT started_at, ended_at, summary, tokens_used FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone(),
        )
        assert row_data is not None
        row = row_data

        assert row[0]
        assert row[1]
        assert row[2] == "done"
        assert row[3] == 123
    finally:
        store.close()


def test_daemon_flag_round_trip_and_missing_returns_none(tmp_path: Path) -> None:
    store = Store(tmp_path / "n.db")
    try:
        assert store.get_daemon_flag("pause") is None
        store.set_daemon_flag("pause", "1")
        assert store.get_daemon_flag("pause") == "1"
    finally:
        store.close()


def test_concurrent_writes_from_two_connections_do_not_deadlock(tmp_path: Path) -> None:
    db = tmp_path / "n.db"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    store_a = Store(db)
    store_b = Store(db)

    def writer(prefix: str, store: _Store) -> None:
        try:
            _ = barrier.wait(timeout=10)
            for idx in range(50):
                store.insert_task(_task(f"{prefix}-{idx}", checkout_path=f"/tmp/{prefix}-{idx}"))
        except BaseException as exc:
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=writer, args=("a", store_a)),
            threading.Thread(target=writer, args=("b", store_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)

        store = Store(db)
        try:
            ids = {row[0] for row in cast(list[sqlite3.Row], store.conn.execute("SELECT id FROM tasks").fetchall())}
            assert len(ids) == 100
            assert {f"a-{idx}" for idx in range(50)} <= ids
            assert {f"b-{idx}" for idx in range(50)} <= ids
        finally:
            store.close()
    finally:
        store_a.close()
        store_b.close()
