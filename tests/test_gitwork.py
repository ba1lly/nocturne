from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock

import pytest

from nocturne import gitwork
from nocturne.gitwork import (
    GitworkError,
    branch_name,
    cleanup,
    commit_push,
    make_worktree,
    open_pr,
    prune_worktrees,
)
from nocturne.guardrails import GuardrailViolation

from tests.fakes import FakeGhResult, RecordingSubprocess, make_subprocess_result


HOOK_PATH = Path(gitwork.__file__).parent / "_hooks" / "pre-push"
ZERO_SHA = "0000000000000000000000000000000000000000"


# -----------------------------
# Basic / pure-function coverage
# -----------------------------


def test_branch_name_format() -> None:
    assert branch_name(42, 3) == "nocturne/issue-42-3"
    assert re.match(r"^nocturne/issue-\d+-\d+$", branch_name(1, 1))
    assert re.match(r"^nocturne/issue-\d+-\d+$", branch_name(999, 17))


def test_prune_worktrees_invokes_git(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = RecordingSubprocess()
    monkeypatch.setattr(subprocess, "run", recorder)

    prune_worktrees(Path("/tmp/some-repo"))

    assert len(recorder.calls) == 1
    args, _kwargs = recorder.calls[0]
    assert list(args) == ["git", "-C", "/tmp/some-repo", "worktree", "prune"]


# -----------------------------
# make_worktree (real git via tmp_worktree fixture)
# -----------------------------


def test_make_worktree_creates_worktree_and_installs_hook(tmp_worktree: Path, tmp_path: Path) -> None:
    wt_path = tmp_path / "wt-1"
    result = make_worktree(tmp_worktree, "nocturne/issue-1-1", "main", wt_path)

    assert result == wt_path
    assert wt_path.exists()
    assert wt_path.is_dir()

    # Confirm the branch was created
    branch = subprocess.run(
        ["git", "-C", str(wt_path), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "nocturne/issue-1-1"

    # Hook file lives in the resolved hook dir
    hooks_rel = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "--git-path", "hooks"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    hook_path = Path(hooks_rel)
    if not hook_path.is_absolute():
        hook_path = (wt_path / hook_path).resolve()
    hook_path = hook_path / "pre-push"

    assert hook_path.exists()
    mode = hook_path.stat().st_mode & 0o777
    # acceptance criterion: mode >= 0o744
    assert mode >= 0o744, f"hook mode {oct(mode)} < 0o744"
    assert mode & 0o100, "owner exec bit must be set"


def test_make_worktree_cleans_stale_path(tmp_worktree: Path, tmp_path: Path) -> None:
    wt_path = tmp_path / "wt-stale"
    wt_path.mkdir()
    (wt_path / ".placeholder").write_text("stale", encoding="utf-8")

    # Should NOT raise even though the path pre-exists with junk
    result = make_worktree(tmp_worktree, "nocturne/issue-2-1", "main", wt_path)

    assert result == wt_path
    assert wt_path.exists()
    # Stale placeholder must be gone — replaced by a real worktree checkout
    assert not (wt_path / ".placeholder").exists()


# -----------------------------
# commit_push
# -----------------------------


def test_commit_push_uses_nocturne_identity_and_calls_guardrail(
    tmp_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_path = tmp_path / "wt-commit"
    make_worktree(tmp_worktree, "nocturne/issue-3-1", "main", wt_path)

    # Create a real file change so commit isn't empty
    (wt_path / "file.txt").write_text("hello", encoding="utf-8")

    # Stub out push (we can't push to a real remote here); record guardrail invocation
    guard_mock = MagicMock()
    monkeypatch.setattr("nocturne.gitwork.enforce_no_force_push", guard_mock)

    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and "push" in list(args):
            return CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)

    commit_push(wt_path, "feat: test commit")

    # Guardrail must have been invoked exactly once, with the push argv
    assert guard_mock.call_count == 1
    pushed_args = guard_mock.call_args.args[0]
    assert pushed_args[:3] == ["git", "-C", str(wt_path)]
    assert "push" in pushed_args
    assert "--force" not in pushed_args
    assert "-f" not in pushed_args

    # Inspect the actual commit author/committer in the worktree
    log = real_run(
        ["git", "-C", str(wt_path), "log", "-1", "--pretty=%an <%ae>|%cn <%ce>"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    author, committer = log.split("|")
    assert author == "Nocturne <nocturne@noreply.localhost>"
    assert committer == "Nocturne <nocturne@noreply.localhost>"


def test_commit_push_guardrail_blocks_force(
    tmp_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the guardrail raises, commit_push must propagate (no subprocess push call)."""
    wt_path = tmp_path / "wt-force"
    make_worktree(tmp_worktree, "nocturne/issue-4-1", "main", wt_path)
    (wt_path / "file.txt").write_text("x", encoding="utf-8")

    def raising_guard(args: list[str]) -> None:
        raise GuardrailViolation("synthetic force-push block")

    monkeypatch.setattr("nocturne.gitwork.enforce_no_force_push", raising_guard)

    push_called = {"n": 0}
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and "push" in list(args):
            push_called["n"] += 1
            return CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GuardrailViolation):
        commit_push(wt_path, "feat: should fail")

    assert push_called["n"] == 0, "push must NOT run if guardrail raises"


# -----------------------------
# open_pr
# -----------------------------


def test_open_pr_success_returns_url(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = RecordingSubprocess()
    recorder.queue_result(
        FakeGhResult.success(stdout="https://github.com/octo/repo/pull/42\n")
    )
    monkeypatch.setattr(subprocess, "run", recorder)

    url = open_pr("octo/repo", "nocturne/issue-5-1", "main", "title", "body")

    assert url == "https://github.com/octo/repo/pull/42"
    # Sanity: gh argv contains the expected flags
    args, _ = recorder.calls[0]
    assert list(args)[:5] == ["gh", "pr", "create", "--repo", "octo/repo"]
    assert "--head" in args and "nocturne/issue-5-1" in args


def test_open_pr_existing_pr_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = RecordingSubprocess()
    stderr = (
        "a pull request for branch \"nocturne/issue-6-1\" into branch \"main\" "
        "already exists:\nhttps://github.com/octo/repo/pull/99\n"
    )
    recorder.queue_result(make_subprocess_result(exit_code=1, stdout="", stderr=stderr))
    monkeypatch.setattr(subprocess, "run", recorder)

    url = open_pr("octo/repo", "nocturne/issue-6-1", "main", "t", "b")

    assert url == "https://github.com/octo/repo/pull/99"


def test_open_pr_hard_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = RecordingSubprocess()
    for _ in range(3):
        recorder.queue_result(FakeGhResult.rate_limited())
    monkeypatch.setattr(subprocess, "run", recorder)
    monkeypatch.setattr("nocturne.gitwork.time.sleep", lambda *_: None)

    with pytest.raises(GitworkError) as exc:
        open_pr("octo/repo", "nocturne/issue-7-1", "main", "t", "b")
    assert "failed to open PR" in str(exc.value)
    assert len(recorder.calls) == 3


def test_open_pr_non_retryable_failure_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = RecordingSubprocess()
    recorder.queue_result(FakeGhResult.auth_failed())
    monkeypatch.setattr(subprocess, "run", recorder)

    with pytest.raises(GitworkError):
        open_pr("octo/repo", "nocturne/issue-7-1", "main", "t", "b")
    assert len(recorder.calls) == 1


# -----------------------------
# cleanup
# -----------------------------


def test_cleanup_runs_worktree_remove(tmp_worktree: Path, tmp_path: Path) -> None:
    wt_path = tmp_path / "wt-cleanup"
    make_worktree(tmp_worktree, "nocturne/issue-8-1", "main", wt_path)
    assert wt_path.exists()

    cleanup(wt_path, tmp_worktree)

    assert not wt_path.exists()


def test_cleanup_swallows_when_already_gone(tmp_worktree: Path, tmp_path: Path) -> None:
    # Path was never registered as a worktree — cleanup must not raise
    cleanup(tmp_path / "never-was", tmp_worktree)


# -----------------------------
# Pre-push hook semantics (bash subprocess)
# -----------------------------


def _invoke_hook(stdin_line: str) -> CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=stdin_line,
        capture_output=True,
        text=True,
        check=False,
    )


def test_prepush_hook_rejects_deletion() -> None:
    line = f"(deleted) {ZERO_SHA} refs/heads/some-branch abc1234567890abcdef1234567890abcdef123456\n"
    result = _invoke_hook(line)
    assert result.returncode != 0, "deletion must be rejected"
    assert "deletion rejected" in result.stderr.lower() or "rejected" in result.stderr.lower()


def test_prepush_hook_allows_new_remote_branch() -> None:
    # New remote branch: remote_sha is all zeros
    line = (
        f"refs/heads/nocturne/issue-1-1 abc1234567890abcdef1234567890abcdef123456 "
        f"refs/heads/nocturne/issue-1-1 {ZERO_SHA}\n"
    )
    result = _invoke_hook(line)
    assert result.returncode == 0, f"new remote branch must be allowed; stderr={result.stderr!r}"


def test_prepush_hook_allows_normal_update() -> None:
    line = (
        "refs/heads/main def4567890abcdef1234567890abcdef12345678 "
        "refs/heads/main abc1234567890abcdef1234567890abcdef123456\n"
    )
    result = _invoke_hook(line)
    assert result.returncode == 0


def test_prepush_hook_is_executable() -> None:
    mode = HOOK_PATH.stat().st_mode & 0o777
    assert mode & 0o100, f"hook owner exec bit not set: {oct(mode)}"
    assert mode >= 0o744, f"hook mode {oct(mode)} < 0o744"
