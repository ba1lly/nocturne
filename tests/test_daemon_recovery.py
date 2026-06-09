"""Tests for nocturne.daemon_recovery — PID-based task reconciliation + worktree prune."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from nocturne import daemon_recovery
from nocturne.daemon_recovery import (
    _parse_worktree_porcelain,
    pid_alive,
    reconcile,
    reconcile_tasks,
    reconcile_worktrees,
)
from nocturne.models import Task, TaskStatus
from nocturne.store import Store

# ---------- helpers ----------


def _now() -> datetime:
    return datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)


def _task(
    task_id: str,
    *,
    status: str = "selected",
    opencode_pid: int | None = None,
) -> Task:
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
        "opencode_pid": opencode_pid,
    }
    return Task.model_validate(data)


@pytest.fixture
def cfg():
    """Programmatically-built Config with checkout_path pointing at the nocturne repo itself."""
    from nocturne.config import (
        Config,
        DaemonConfig,
        DiscordConfig,
        GitHubConfig,
        GuardrailsConfig,
        HealthcheckConfig,
        ModelsConfig,
        OpenCodeConfig,
        PersonaConfig,
        ProviderConfig,
        RepoConfig,
        ReviewConfig,
        SandboxConfig,
    )

    return Config(
        github=GitHubConfig(owner="ba1lly"),
        sandbox=SandboxConfig(),
        providers={
            "alibaba-coding-plan": ProviderConfig(
                base_url="https://x",
                api_key_env="DASHSCOPE_API_KEY",
            )
        },
        models=ModelsConfig(
            reasoning="alibaba-coding-plan/qwen3.6-plus",
            report="alibaba-coding-plan/qwen3.6-plus",
            coding="alibaba-coding-plan/qwen3-coder-plus",
        ),
        opencode=OpenCodeConfig(),
        repos=[
            RepoConfig(
                slug="ba1lly/sandbox",
                checkout_path=str(Path(__file__).resolve().parents[1]),
                label="agent",
                base="main",
                verify_cmd="pytest -q",
                require_new_test=False,
            )
        ],
        guardrails=GuardrailsConfig(),
        discord=DiscordConfig(channel_id=1234, mention_user_id=5678),
        daemon=DaemonConfig(poll_interval_sec=300),
        review=ReviewConfig(),
        healthcheck=HealthcheckConfig(),
        persona=PersonaConfig(enabled=False, soul_path=None),
    )


# ---------- pid_alive ----------


def test_pid_alive_for_self() -> None:
    assert pid_alive(os.getpid()) is True


def test_pid_alive_for_dead_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_no_such_proc(pid: int, sig: int) -> None:
        raise ProcessLookupError("no such process")

    monkeypatch.setattr(os, "kill", _raise_no_such_proc)
    assert pid_alive(99999) is False


def test_pid_alive_handles_invalid_pid() -> None:
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_pid_alive_permission_error_means_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_perm(pid: int, sig: int) -> None:
        raise PermissionError("not permitted")

    monkeypatch.setattr(os, "kill", _raise_perm)
    assert pid_alive(12345) is True


def test_pid_alive_oserror_treated_as_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_oserror(pid: int, sig: int) -> None:
        raise OSError("EIO or similar")

    monkeypatch.setattr(os, "kill", _raise_oserror)
    assert pid_alive(12345) is False


# ---------- reconcile_tasks ----------


def test_dead_pid_recovery(inmem_store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task("owner/repo#1", status="running", opencode_pid=99999)
    inmem_store.insert_task(task)
    monkeypatch.setattr(
        "nocturne.daemon_recovery.pid_alive",
        lambda pid: pid != 99999,
    )

    summary = reconcile_tasks(inmem_store)

    assert summary["killed_running"] == 1
    assert summary["killed_selected"] == 0
    refreshed = inmem_store.get_task(task.id)
    assert refreshed is not None
    assert refreshed.status == "failed"


def test_alive_pid_not_recovered(inmem_store: Store) -> None:
    task = _task("owner/repo#2", status="running", opencode_pid=os.getpid())
    inmem_store.insert_task(task)

    summary = reconcile_tasks(inmem_store)

    assert summary["unchanged"] == 1
    assert summary["killed_running"] == 0
    refreshed = inmem_store.get_task(task.id)
    assert refreshed is not None
    assert refreshed.status == "running"


def test_running_no_pid_marked_failed(inmem_store: Store) -> None:
    task = _task("owner/repo#3", status="running", opencode_pid=None)
    inmem_store.insert_task(task)

    summary = reconcile_tasks(inmem_store)

    assert summary["killed_running"] == 1
    refreshed = inmem_store.get_task(task.id)
    assert refreshed is not None
    assert refreshed.status == "failed"


def test_selected_with_dead_pid_marked_failed(
    inmem_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task("owner/repo#4", status="selected", opencode_pid=99999)
    inmem_store.insert_task(task)
    monkeypatch.setattr(
        "nocturne.daemon_recovery.pid_alive",
        lambda pid: pid != 99999,
    )

    summary = reconcile_tasks(inmem_store)

    assert summary["killed_selected"] == 1
    refreshed = inmem_store.get_task(task.id)
    assert refreshed is not None
    assert refreshed.status == "failed"


def test_selected_with_no_pid_unchanged(inmem_store: Store) -> None:
    task = _task("owner/repo#5", status="selected", opencode_pid=None)
    inmem_store.insert_task(task)

    summary = reconcile_tasks(inmem_store)

    assert summary["unchanged"] == 1
    assert summary["killed_selected"] == 0
    refreshed = inmem_store.get_task(task.id)
    assert refreshed is not None
    assert refreshed.status == "selected"


def test_parked_untouched(inmem_store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # Parked tasks have a question; build one with a dead PID and parked status.
    data: dict[str, object] = {
        "id": "owner/repo#6",
        "repo_slug": "owner/repo",
        "checkout_path": "/tmp/parked",
        "issue_number": 6,
        "title": "T",
        "body": "B",
        "base": "main",
        "verify_cmd": "pytest -q",
        "require_new_test": True,
        "coding_model": "alibaba-coding-plan/qwen3-coder-plus",
        "branch": "nocturne/x",
        "status": "parked",
        "attempts": 0,
        "pr_url": None,
        "question": "Need clarification?",
        "answer": None,
        "created_at": _now(),
        "updated_at": _now(),
        "opencode_pid": 99999,
    }
    task = Task.model_validate(data)
    inmem_store.insert_task(task)
    # Even if pid is dead, parked must not be touched.
    monkeypatch.setattr("nocturne.daemon_recovery.pid_alive", lambda pid: False)

    summary = reconcile_tasks(inmem_store)

    assert summary["killed_running"] == 0
    assert summary["killed_selected"] == 0
    refreshed = inmem_store.get_task(task.id)
    assert refreshed is not None
    assert refreshed.status == "parked"


# ---------- _parse_worktree_porcelain ----------


def test_parse_worktree_porcelain_empty() -> None:
    assert _parse_worktree_porcelain("") == []


def test_parse_worktree_porcelain_multiple() -> None:
    output = (
        "worktree /home/u/repo\n"
        "HEAD abc123\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /tmp/nocturne/wt-1\n"
        "HEAD def456\n"
        "branch refs/heads/nocturne/issue-1-1\n"
        "\n"
        "worktree /tmp/nocturne/wt-2\n"
        "HEAD 789aaa\n"
        "branch refs/heads/nocturne/issue-2-1\n"
    )
    paths = _parse_worktree_porcelain(output)
    assert paths == [
        Path("/home/u/repo"),
        Path("/tmp/nocturne/wt-1"),
        Path("/tmp/nocturne/wt-2"),
    ]


# ---------- reconcile_worktrees ----------


def test_stale_worktree_cleanup(
    cfg, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_path = Path(cfg.repos[0].checkout_path).expanduser()
    real_wt = tmp_path / "real-wt"
    real_wt.mkdir()
    ghost_wt = Path("/tmp/nonexistent-wt-12345-ghost")
    assert not ghost_wt.exists()

    porcelain_listing = (
        f"worktree {repo_path}\n"
        f"HEAD abc\n"
        f"branch refs/heads/main\n"
        f"\n"
        f"worktree {real_wt}\n"
        f"HEAD def\n"
        f"branch refs/heads/wt-real\n"
        f"\n"
        f"worktree {ghost_wt}\n"
        f"HEAD 789\n"
        f"branch refs/heads/wt-ghost\n"
    )

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        # args layout: ["git", "-C", <repo>, "worktree", <subcmd>, ...]
        subcmd = args[4] if len(args) >= 5 else ""
        if subcmd == "prune":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if subcmd == "list":
            return subprocess.CompletedProcess(args, 0, stdout=porcelain_listing, stderr="")
        if subcmd == "remove":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("nocturne.daemon_recovery.subprocess.run", fake_run)

    cleaned = reconcile_worktrees(cfg)

    assert ghost_wt in cleaned
    assert real_wt not in cleaned
    # The remove subprocess must have been called against the ghost path.
    remove_calls = [c for c in calls if len(c) >= 5 and c[4] == "remove"]
    assert any(str(ghost_wt) in c for c in remove_calls)


def test_reconcile_worktrees_skips_repo_without_git(
    cfg, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Re-point cfg.repos[0].checkout_path to a tmp dir without .git
    no_git = tmp_path / "no-git-repo"
    no_git.mkdir()
    cfg.repos[0].__dict__["checkout_path"] = str(no_git)

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("nocturne.daemon_recovery.subprocess.run", fake_run)

    cleaned = reconcile_worktrees(cfg)

    assert cleaned == []
    # No git subprocess invocations should have happened for the missing-.git repo.
    assert calls == []


def test_reconcile_worktrees_prune_failure_continues(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(args) >= 5 and args[4] == "prune":
            raise subprocess.CalledProcessError(1, args, stderr="prune failed")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("nocturne.daemon_recovery.subprocess.run", fake_run)

    cleaned = reconcile_worktrees(cfg)
    # No cleanup, but no exception raised — function must swallow the prune failure.
    assert cleaned == []


# ---------- reconcile (combined) ----------


def test_reconcile_combined_summary(
    cfg, inmem_store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Set up a dead-pid running task.
    task = _task("owner/repo#7", status="running", opencode_pid=99999)
    inmem_store.insert_task(task)
    monkeypatch.setattr(
        "nocturne.daemon_recovery.pid_alive",
        lambda pid: pid != 99999,
    )

    # Set up ghost-worktree side: prune ok, list returns one ghost + main repo, remove ok.
    repo_path = Path(cfg.repos[0].checkout_path).expanduser()
    ghost_wt = Path("/tmp/nonexistent-wt-combined-99999")
    porcelain_listing = (
        f"worktree {repo_path}\n"
        f"HEAD abc\n"
        f"branch refs/heads/main\n"
        f"\n"
        f"worktree {ghost_wt}\n"
        f"HEAD def\n"
        f"branch refs/heads/wt-ghost\n"
    )

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        subcmd = args[4] if len(args) >= 5 else ""
        if subcmd == "list":
            return subprocess.CompletedProcess(args, 0, stdout=porcelain_listing, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("nocturne.daemon_recovery.subprocess.run", fake_run)

    summary = reconcile(cfg, inmem_store)

    assert summary["worktrees_cleaned_count"] == 1
    assert str(ghost_wt) in summary["worktrees_cleaned"]
    assert summary["killed_running"] == 1
    assert summary["unchanged"] == 0
    refreshed = inmem_store.get_task(task.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
