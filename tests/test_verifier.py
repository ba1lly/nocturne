from __future__ import annotations

# pyright: reportMissingImports=false
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from nocturne.models import Task
from nocturne.verifier import diff_includes_test, is_test_file, verify
from tests.fakes import RecordingSubprocess, make_subprocess_result


def make_task(*, verify_cmd: str, require_new_test: bool, base: str = "main", checkout_path: str = "/tmp/worktree") -> Task:
    now = datetime.utcnow()
    return Task(
        id="task-1",
        status="running",
        created_at=now,
        updated_at=now,
        repo_slug="ba1lly/nocturne",
        checkout_path=checkout_path,
        issue_number=1,
        title="x",
        body="y",
        base=base,
        verify_cmd=verify_cmd,
        require_new_test=require_new_test,
        coding_model="alibaba-coding-plan/qwen3-coder-plus",
        branch="main",
        attempts=0,
    )


def git(worktree: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(worktree), *args], check=True)


def commit_file(worktree: Path, relpath: str, content: str, message: str) -> None:
    file_path = worktree / relpath
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    git(worktree, "add", relpath)
    git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@local",
        "commit",
        "-m",
        message,
    )


def test_is_test_file_matches_tests_dir() -> None:
    assert is_test_file("tests/test_foo.py") is True


def test_is_test_file_rejects_source_file() -> None:
    assert is_test_file("src/playground/math.py") is False


def test_is_test_file_matches_top_level_test_file() -> None:
    assert is_test_file("test_top.py") is True


def test_is_test_file_matches_suffix_style() -> None:
    assert is_test_file("foo_test.py") is True


def test_is_test_file_rejects_docs_tests_md() -> None:
    assert is_test_file("docs/tests.md") is False


def test_verify_happy_path(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task = make_task(verify_cmd="true", require_new_test=False, checkout_path=str(worktree))

    result = verify(task, worktree)

    assert result.passed is True
    assert result.exit_code == 0
    assert result.new_test_added is False


def test_verify_failure_path(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task = make_task(verify_cmd="false", require_new_test=False, checkout_path=str(worktree))

    result = verify(task, worktree)

    assert result.passed is False
    assert result.exit_code == 1
    assert result.reason is not None and "failed" in result.reason


def test_verify_require_new_test_with_test_diff(tmp_worktree: Path) -> None:
    subprocess.run(["git", "-C", str(tmp_worktree), "checkout", "-b", "feat-x"], check=True)
    commit_file(tmp_worktree, "tests/test_x.py", "def test_x():\n    assert True\n", "add test")
    task = make_task(verify_cmd="true", require_new_test=True, checkout_path=str(tmp_worktree))

    result = verify(task, tmp_worktree)

    assert result.passed is True
    assert result.new_test_added is True
    assert result.exit_code == 0


def test_require_new_test_no_diff(tmp_worktree: Path) -> None:
    subprocess.run(["git", "-C", str(tmp_worktree), "checkout", "-b", "feat-no-test"], check=True)
    commit_file(tmp_worktree, "src/playground/math.py", "def divide(a, b):\n    return a / b\n", "add source")
    task = make_task(verify_cmd="true", require_new_test=True, checkout_path=str(tmp_worktree))

    result = verify(task, tmp_worktree)

    assert result.passed is False
    assert result.new_test_added is False
    assert result.reason == "no test added"


def test_verify_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task = make_task(verify_cmd="sleep 1", require_new_test=False, checkout_path=str(worktree))

    def boom(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="sleep 1", timeout=600)

    monkeypatch.setattr(subprocess, "run", boom)

    result = verify(task, worktree)

    assert result.passed is False
    assert result.exit_code == -1
    assert result.reason == "verify_cmd timed out"


def test_verify_runs_in_correct_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    recorder = RecordingSubprocess()
    recorder.queue_result(make_subprocess_result(exit_code=0, stdout="ok", stderr=""))
    monkeypatch.setattr(subprocess, "run", recorder)
    task = make_task(verify_cmd="true", require_new_test=False, checkout_path=str(worktree))

    result = verify(task, worktree)

    assert result.passed is True
    args, kwargs = recorder.calls[0]
    assert args == "true"
    assert kwargs.get("cwd") == str(worktree)
    assert kwargs.get("shell") is True


def test_diff_includes_test_detects_feature_branch(tmp_worktree: Path) -> None:
    subprocess.run(["git", "-C", str(tmp_worktree), "checkout", "-b", "feat-test-diff"], check=True)
    commit_file(tmp_worktree, "tests/test_a.py", "def test_a():\n    assert True\n", "add test a")

    assert diff_includes_test(tmp_worktree) is True
