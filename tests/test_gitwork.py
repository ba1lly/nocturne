from __future__ import annotations

import os
import re
import subprocess
import time
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
    reap_stale_worktrees,
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


def test_make_worktree_adds_nocturne_artifacts_to_local_exclude(
    tmp_worktree: Path, tmp_path: Path,
) -> None:
    """The nocturne-internal artifacts (.nocturne-pr-body.md + .reviews/)
    MUST be in .git/info/exclude so commit_push's `git add -A` never
    sweeps them into a real commit. This is the local-only ignore path."""
    wt_path = tmp_path / "wt-excl"
    make_worktree(tmp_worktree, "nocturne/issue-9-1", "main", wt_path)

    git_path_result = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    )
    exclude_rel = git_path_result.stdout.strip()
    exclude_path = Path(exclude_rel)
    if not exclude_path.is_absolute():
        exclude_path = (wt_path / exclude_rel).resolve()

    assert exclude_path.is_file(), f".git/info/exclude not created at {exclude_path}"
    content = exclude_path.read_text(encoding="utf-8")
    assert ".nocturne-pr-body.md" in content
    assert ".reviews/" in content


def test_make_worktree_local_exclude_is_idempotent(tmp_worktree: Path, tmp_path: Path) -> None:
    """Calling make_worktree twice (after cleanup) must not double-add
    the exclude entries - preserves any user-added rules already there."""
    wt_path = tmp_path / "wt-idem"
    make_worktree(tmp_worktree, "nocturne/issue-10-1", "main", wt_path)

    git_path_result = subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    )
    exclude_path = Path(git_path_result.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = (wt_path / exclude_path).resolve()

    first = exclude_path.read_text(encoding="utf-8")
    pr_body_count = first.count(".nocturne-pr-body.md")
    reviews_count = first.count(".reviews/")

    make_worktree(tmp_worktree, "nocturne/issue-10-1", "main", wt_path)
    second = exclude_path.read_text(encoding="utf-8")

    assert second.count(".nocturne-pr-body.md") == pr_body_count
    assert second.count(".reviews/") == reviews_count


def test_make_worktree_excludes_build_artifacts(tmp_worktree: Path, tmp_path: Path) -> None:
    """A stray __pycache__/*.pyc or tool cache (e.g. from running verify_cmd)
    must be ignored by git so commit_push's `git add -A` can't sweep it into a
    generated PR - even when the target repo has no .gitignore."""
    wt_path = tmp_path / "wt-artifacts"
    make_worktree(tmp_worktree, "nocturne/issue-11-1", "main", wt_path)

    (wt_path / "src").mkdir(parents=True, exist_ok=True)
    pycache = wt_path / "src" / "__pycache__"
    pycache.mkdir(parents=True, exist_ok=True)
    (pycache / "mod.cpython-312.pyc").write_bytes(b"\x00\x01")
    (wt_path / ".pytest_cache").mkdir(exist_ok=True)
    (wt_path / ".pytest_cache" / "CACHEDIR.TAG").write_text("x", encoding="utf-8")
    (wt_path / ".coverage").write_text("x", encoding="utf-8")

    status = subprocess.run(
        ["git", "-C", str(wt_path), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "__pycache__" not in status
    assert ".pyc" not in status
    assert ".pytest_cache" not in status
    assert ".coverage" not in status


def test_local_exclude_upgrades_from_legacy_two_line_file(tmp_worktree: Path, tmp_path: Path) -> None:
    """An exclude file written by an older nocturne (only the two internal
    patterns) gains the build-artifact patterns, and user rules are kept."""
    wt_path = tmp_path / "wt-upgrade"
    make_worktree(tmp_worktree, "nocturne/issue-12-1", "main", wt_path)

    exclude_path = Path(subprocess.run(
        ["git", "-C", str(wt_path), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    ).stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = (wt_path / exclude_path).resolve()

    # Simulate a legacy exclude file + a user-added rule.
    exclude_path.write_text("my-secret-notes.txt\n.nocturne-pr-body.md\n.reviews/\n", encoding="utf-8")
    from nocturne.gitwork import _add_nocturne_local_excludes
    _add_nocturne_local_excludes(wt_path)

    content = exclude_path.read_text(encoding="utf-8")
    assert "my-secret-notes.txt" in content      # user rule preserved
    assert "__pycache__/" in content             # new pattern added
    assert content.count(".nocturne-pr-body.md") == 1  # not duplicated


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
    # Stale placeholder must be gone - replaced by a real worktree checkout
    assert not (wt_path / ".placeholder").exists()


# -----------------------------
# commit_push
# -----------------------------


def _seed_origin_ref(repo: Path, base: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", f"refs/remotes/origin/{base}", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_commit_push_uses_nocturne_identity_and_calls_guardrail(
    tmp_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_origin_ref(tmp_worktree, "main")
    wt_path = tmp_path / "wt-commit"
    make_worktree(tmp_worktree, "nocturne/issue-3-1", "main", wt_path)

    (wt_path / "file.txt").write_text("hello", encoding="utf-8")

    guard_mock = MagicMock()
    monkeypatch.setattr("nocturne.gitwork.enforce_no_force_push", guard_mock)

    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and "push" in list(args):
            return CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)

    commit_push(wt_path, "feat: test commit", "main")

    assert guard_mock.call_count == 1
    pushed_args = guard_mock.call_args.args[0]
    assert pushed_args[:3] == ["git", "-C", str(wt_path)]
    assert "push" in pushed_args
    assert "--force" not in pushed_args
    assert "-f" not in pushed_args

    log = real_run(
        ["git", "-C", str(wt_path), "log", "-1", "--pretty=%an <%ae>|%cn <%ce>"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    author, committer = log.split("|")
    assert author == "Nocturne <nocturne@noreply.localhost>"
    assert committer == "Nocturne <nocturne@noreply.localhost>"


def test_commit_push_squashes_opencode_self_commits_under_nocturne_identity(
    tmp_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_origin_ref(tmp_worktree, "main")
    wt_path = tmp_path / "wt-squash"
    make_worktree(tmp_worktree, "nocturne/issue-5-1", "main", wt_path)

    (wt_path / "a.txt").write_text("from opencode", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(wt_path), "-c", "user.name=Alice", "-c", "user.email=alice@example.com",
         "add", "a.txt"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(wt_path), "-c", "user.name=Alice", "-c", "user.email=alice@example.com",
         "commit", "-m", "opencode: did the thing"],
        check=True, capture_output=True, text=True,
    )

    (wt_path / "b.txt").write_text("from opencode 2", encoding="utf-8")

    monkeypatch.setattr("nocturne.gitwork.enforce_no_force_push", MagicMock())
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and "push" in list(args):
            return CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)

    commit_push(wt_path, "closes #5: nocturne does the thing", "main")

    count = real_run(
        ["git", "-C", str(wt_path), "rev-list", "--count", "origin/main..HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert count == "1", f"expected exactly 1 commit ahead of origin/main, got {count}"

    log = real_run(
        ["git", "-C", str(wt_path), "log", "-1", "--pretty=%an <%ae>|%cn <%ce>|%s"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    author, committer, subject = log.split("|")
    assert author == "Nocturne <nocturne@noreply.localhost>"
    assert committer == "Nocturne <nocturne@noreply.localhost>"
    assert subject == "closes #5: nocturne does the thing"

    assert (wt_path / "a.txt").exists()
    assert (wt_path / "b.txt").exists()


def test_commit_push_raises_when_no_changes_after_reset(
    tmp_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_origin_ref(tmp_worktree, "main")
    wt_path = tmp_path / "wt-empty"
    make_worktree(tmp_worktree, "nocturne/issue-6-1", "main", wt_path)

    monkeypatch.setattr("nocturne.gitwork.enforce_no_force_push", MagicMock())

    with pytest.raises(GitworkError, match="no changes to commit"):
        commit_push(wt_path, "closes #6: nothing", "main")


def test_commit_push_blocks_sensitive_path_and_never_pushes(
    tmp_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diff that plants a CI workflow (a secret-exfil vector an injected
    agent could try) is rejected before commit, and push is never reached."""
    _seed_origin_ref(tmp_worktree, "main")
    wt_path = tmp_path / "wt-sensitive"
    make_worktree(tmp_worktree, "nocturne/issue-13-1", "main", wt_path)

    (wt_path / "legit.py").write_text("print('ok')\n", encoding="utf-8")
    workflows = wt_path / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "exfil.yml").write_text("on: push\n", encoding="utf-8")

    push_guard = MagicMock()
    monkeypatch.setattr("nocturne.gitwork.enforce_no_force_push", push_guard)

    with pytest.raises(GuardrailViolation, match=r"\.github/workflows/exfil\.yml"):
        commit_push(wt_path, "closes #13: sneaky", "main")

    # The force-push guard sits just before push; never reaching it proves we
    # bailed before any push could happen.
    assert push_guard.call_count == 0


def test_commit_push_allows_clean_diff(
    tmp_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diff touching only ordinary source files passes the scope guard."""
    _seed_origin_ref(tmp_worktree, "main")
    wt_path = tmp_path / "wt-clean"
    make_worktree(tmp_worktree, "nocturne/issue-14-1", "main", wt_path)
    (wt_path / "src.py").write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr("nocturne.gitwork.enforce_no_force_push", MagicMock())
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and "push" in list(args):
            return CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)

    commit_push(wt_path, "closes #14: clean change", "main")  # must not raise


def test_commit_push_guardrail_blocks_force(
    tmp_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_origin_ref(tmp_worktree, "main")
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
        commit_push(wt_path, "feat: should fail", "main")

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


def test_open_pr_passes_network_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = RecordingSubprocess()
    recorder.queue_result(FakeGhResult.success(stdout="https://github.com/octo/repo/pull/1\n"))
    monkeypatch.setattr(subprocess, "run", recorder)

    open_pr("octo/repo", "nocturne/issue-9-1", "main", "t", "b")

    assert recorder.calls[0][1].get("timeout"), "gh pr create must run with a network timeout"


def test_open_pr_timeout_is_retried_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresponsive GitHub must not hang the daemon: timeouts retry, then fail."""
    calls = {"n": 0}

    def boom(*_a, **_kw):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd="gh", timeout=120)

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr("nocturne.gitwork.time.sleep", lambda *_: None)

    with pytest.raises(GitworkError) as exc:
        open_pr("octo/repo", "nocturne/issue-10-1", "main", "t", "b")
    assert "timed out" in str(exc.value)
    assert calls["n"] == 3  # three attempts before giving up


# -----------------------------
# reap_stale_worktrees
# -----------------------------


def test_reap_removes_old_dirs_keeps_recent(tmp_path: Path) -> None:
    root = tmp_path / "wt-root"
    root.mkdir()
    old = root / "owner__repo-issue-1-1"
    fresh = root / "owner__repo-issue-2-1"
    old.mkdir()
    fresh.mkdir()
    # Age the old worktree past the TTL.
    old_time = time.time() - 72 * 3600
    os.utime(old, (old_time, old_time))

    removed = reap_stale_worktrees(root, ttl_hours=48)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_reap_disabled_when_ttl_zero(tmp_path: Path) -> None:
    root = tmp_path / "wt-root"
    root.mkdir()
    old = root / "stale"
    old.mkdir()
    old_time = time.time() - 1000 * 3600
    os.utime(old, (old_time, old_time))

    assert reap_stale_worktrees(root, ttl_hours=0) == 0
    assert old.exists()


def test_reap_missing_root_is_noop(tmp_path: Path) -> None:
    assert reap_stale_worktrees(tmp_path / "does-not-exist", ttl_hours=48) == 0


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
    # Path was never registered as a worktree - cleanup must not raise
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
