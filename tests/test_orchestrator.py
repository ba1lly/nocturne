from __future__ import annotations

# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportAny=false, reportExplicitAny=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportArgumentType=false, reportUnusedCallResult=false

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nocturne import orchestrator
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
from nocturne.guardrails import GuardrailViolation
from nocturne.models import OpenCodeResult, Task, VerifyResult
from nocturne.store import Store

from tests.fakes import FakeOpenCodeResult


@pytest.fixture
def cfg(tmp_worktree: Path, tmp_path: Path) -> Config:
    return Config(
        github=GitHubConfig(owner="ba1lly"),
        sandbox=SandboxConfig(repo_name="nocturne-playground", checkout_path=str(tmp_worktree)),
        providers={"alibaba-coding-plan": ProviderConfig(base_url="https://example.test", api_key_env="DASHSCOPE_API_KEY")},
        models=ModelsConfig(
            reasoning="alibaba-coding-plan/qwen3-reasoning-plus",
            coding="alibaba-coding-plan/qwen3-coder-plus",
            report="alibaba-coding-plan/qwen3-report-plus",
        ),
        opencode=OpenCodeConfig(command="opencode", timeout_min=1, worktree_root=str(tmp_path / "wt-root")),
        repos=[RepoConfig(slug="ba1lly/nocturne-playground", checkout_path=str(tmp_worktree), verify_cmd="pytest -q")],
        guardrails=GuardrailsConfig(max_attempts=3),
        discord=DiscordConfig(channel_id=1, mention_user_id=1),
        daemon=DaemonConfig(),
        review=ReviewConfig(),
        healthcheck=HealthcheckConfig(),
        persona=PersonaConfig(enabled=False),
    )


def _make_task(checkout_path: Path, *, task_id: str = "issue#42", repo_slug: str = "ba1lly/nocturne-playground", status: str = "selected", base: str = "main") -> Task:
    now = datetime.now(UTC)
    return Task(
        id=task_id,
        repo_slug=repo_slug,
        checkout_path=str(checkout_path),
        issue_number=42,
        title="Fix the thing",
        body="Body of the issue.",
        base=base,
        verify_cmd="pytest -q",
        require_new_test=False,
        coding_model="",
        branch="",
        status=status,
        attempts=0,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def task(tmp_worktree: Path, inmem_store: Store) -> Task:
    t = _make_task(tmp_worktree)
    inmem_store.insert_task(t)
    return t


class _FakeCalls:
    def __init__(self) -> None:
        self.make_worktree: list[tuple[Any, ...]] = []
        self.opencode_run: list[dict[str, Any]] = []
        self.verify: list[tuple[Any, ...]] = []
        self.commit_push: list[tuple[Any, ...]] = []
        self.open_pr: list[tuple[Any, ...]] = []
        self.cleanup: list[tuple[Any, ...]] = []


def _patch_all(
    monkeypatch: pytest.MonkeyPatch,
    *,
    opencode_results: list[OpenCodeResult] | None = None,
    verify_results: list[VerifyResult] | None = None,
    pr_url: str = "https://github.com/ba1lly/nocturne-playground/pull/9",
    raise_in_assert: bool = False,
    pid_emit: int | None = 42,
) -> _FakeCalls:
    calls = _FakeCalls()
    oc_iter = iter(opencode_results or [FakeOpenCodeResult.success("ok")])
    v_iter = iter(verify_results or [VerifyResult(passed=True, exit_code=0, stdout="", stderr="", new_test_added=False)])

    def fake_make_worktree(repo_path: Path, branch: str, base: str, worktree_path: Path) -> Path:
        calls.make_worktree.append((repo_path, branch, base, worktree_path))
        worktree_path.mkdir(parents=True, exist_ok=True)
        return worktree_path

    def fake_run(task: Task, cwd: Path, cfg: Config, prior_failure: str | None = None, on_pid_started=None) -> OpenCodeResult:
        calls.opencode_run.append({"task_id": task.id, "cwd": cwd, "prior_failure": prior_failure})
        if on_pid_started is not None and pid_emit is not None:
            on_pid_started(pid_emit)
        return next(oc_iter)

    def fake_verify(task: Task, wt: Path) -> VerifyResult:
        calls.verify.append((task.id, wt))
        return next(v_iter)

    def fake_commit_push(wt: Path, message: str) -> None:
        calls.commit_push.append((wt, message))

    def fake_open_pr(repo: str, branch: str, base: str, title: str, body: str) -> str:
        calls.open_pr.append((repo, branch, base, title, body))
        return pr_url

    def fake_cleanup(wt: Path, repo_path: Path) -> None:
        calls.cleanup.append((wt, repo_path))

    def fake_assert_not_main(worktree: Path, base: str) -> None:
        if raise_in_assert:
            raise GuardrailViolation("worktree on protected base branch")

    monkeypatch.setattr("nocturne.orchestrator.make_worktree", fake_make_worktree)
    monkeypatch.setattr("nocturne.orchestrator.commit_push", fake_commit_push)
    monkeypatch.setattr("nocturne.orchestrator.open_pr", fake_open_pr)
    monkeypatch.setattr("nocturne.orchestrator.gitwork.cleanup", fake_cleanup)
    monkeypatch.setattr("nocturne.orchestrator.opencode_driver.run", fake_run)
    monkeypatch.setattr("nocturne.orchestrator.verifier.verify", fake_verify)
    monkeypatch.setattr("nocturne.guardrails.assert_not_main_branch", fake_assert_not_main)
    return calls


# --------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------


def test_green_path_end_to_end(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(monkeypatch)

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "done"
    assert result.pr_url == "https://github.com/ba1lly/nocturne-playground/pull/9"

    persisted = inmem_store.get_task(task.id)
    assert persisted is not None
    assert persisted.status == "done"
    assert persisted.pr_url == result.pr_url
    assert persisted.opencode_pid == 42

    assert len(calls.make_worktree) == 1
    assert len(calls.opencode_run) == 1
    assert len(calls.verify) == 1
    assert len(calls.commit_push) == 1
    assert len(calls.open_pr) == 1
    assert len(calls.cleanup) == 1


def test_dry_run_skips_push_and_pr(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(monkeypatch)

    result = orchestrator.process_task(task, cfg, inmem_store, dry_run=True)

    assert result.status == "done"
    assert result.pr_url == "dry-run"
    assert len(calls.commit_push) == 0
    assert len(calls.open_pr) == 0
    persisted = inmem_store.get_task(task.id)
    assert persisted is not None
    assert persisted.pr_url == "dry-run"


def test_retry_then_succeed(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(
        monkeypatch,
        opencode_results=[
            FakeOpenCodeResult.with_error_event("first boom"),
            FakeOpenCodeResult.success("ok"),
        ],
    )

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "done"
    assert len(calls.opencode_run) == 2
    assert calls.opencode_run[0]["prior_failure"] is None
    assert calls.opencode_run[1]["prior_failure"] is not None
    assert "first boom" in calls.opencode_run[1]["prior_failure"]


def test_retry_injects_prior_failure(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """QA scenario lines 1674-1679: retry passes prior_failure on every retry."""
    calls = _patch_all(
        monkeypatch,
        opencode_results=[
            FakeOpenCodeResult.with_error_event("first failure msg"),
            FakeOpenCodeResult.with_error_event("second failure msg"),
            FakeOpenCodeResult.with_error_event("third failure msg"),
        ],
    )

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "failed"
    assert len(calls.opencode_run) == cfg.guardrails.max_attempts
    assert calls.opencode_run[0]["prior_failure"] is None
    for invocation in calls.opencode_run[1:]:
        assert invocation["prior_failure"] is not None
        assert "exit_code=" in invocation["prior_failure"]


def test_retry_exhausted_marks_failed(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(
        monkeypatch,
        opencode_results=[FakeOpenCodeResult.with_error_event("boom")] * cfg.guardrails.max_attempts,
    )

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "failed"
    assert len(calls.opencode_run) == cfg.guardrails.max_attempts
    assert len(calls.commit_push) == 0
    assert len(calls.open_pr) == 0
    persisted = inmem_store.get_task(task.id)
    assert persisted is not None
    assert persisted.status == "failed"


def test_error_event_blocks_pr(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """QA scenario lines 1681-1686: even with exit 0, error events block PR."""
    err_with_zero_exit = OpenCodeResult(
        exit_code=0,
        events=[{"type": "error", "message": "boom"}],
        sentinel_seen=False,
        need_input_question=None,
        pid=99,
        error_events=[{"type": "error", "message": "boom"}],
    )
    calls = _patch_all(
        monkeypatch,
        opencode_results=[err_with_zero_exit, err_with_zero_exit, err_with_zero_exit],
    )

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "failed"
    assert len(calls.open_pr) == 0
    assert len(calls.commit_push) == 0


def test_verify_fail_retries(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(
        monkeypatch,
        opencode_results=[FakeOpenCodeResult.success("ok"), FakeOpenCodeResult.success("ok")],
        verify_results=[
            VerifyResult(passed=False, exit_code=1, stdout="OUT", stderr="ERR", new_test_added=False, reason="bad"),
            VerifyResult(passed=True, exit_code=0, stdout="", stderr="", new_test_added=False),
        ],
    )

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "done"
    assert len(calls.opencode_run) == 2
    assert calls.opencode_run[1]["prior_failure"] is not None
    fail_text = calls.opencode_run[1]["prior_failure"]
    assert "OUT" in fail_text
    assert "ERR" in fail_text
    assert "bad" in fail_text


def test_sentinel_breaks_out(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(
        monkeypatch,
        opencode_results=[FakeOpenCodeResult.with_sentinel("which API?")],
    )

    result = orchestrator.process_task(task, cfg, inmem_store)

    # M1: sentinel breaks loop; status becomes "failed" (M3 will refactor to "parked").
    assert result.status == "failed"
    assert len(calls.opencode_run) == 1
    assert len(calls.commit_push) == 0
    assert len(calls.open_pr) == 0


def test_pid_written_via_callback(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    _patch_all(monkeypatch, pid_emit=99887)

    orchestrator.process_task(task, cfg, inmem_store)

    persisted = inmem_store.get_task(task.id)
    assert persisted is not None
    assert persisted.opencode_pid == 99887


def test_status_transitions_selected_to_done(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """Task starts as 'selected' (set by fixture). After process_task, row is 'done'."""
    pre = inmem_store.get_task(task.id)
    assert pre is not None
    assert pre.status == "selected"

    _patch_all(monkeypatch)
    orchestrator.process_task(task, cfg, inmem_store)

    post = inmem_store.get_task(task.id)
    assert post is not None
    assert post.status == "done"


def test_main_branch_assertion_fires(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """QA scenario lines 1688-1693: WorktreeContext fires assert_not_main_branch on exit."""
    _patch_all(monkeypatch, raise_in_assert=True)

    with pytest.raises(GuardrailViolation):
        orchestrator.process_task(task, cfg, inmem_store)


def test_unallowed_repo_raises(monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store) -> None:
    bad_task = _make_task(tmp_worktree, task_id="bad#1", repo_slug="evil/repo")
    inmem_store.insert_task(bad_task)
    calls = _patch_all(monkeypatch)

    with pytest.raises(GuardrailViolation):
        orchestrator.process_task(bad_task, cfg, inmem_store)

    # No side effects after gate fires.
    assert len(calls.make_worktree) == 0
    assert len(calls.opencode_run) == 0


def test_cleanup_called_on_success(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(monkeypatch)

    orchestrator.process_task(task, cfg, inmem_store)

    assert len(calls.cleanup) == 1


def test_no_cleanup_on_failure(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(
        monkeypatch,
        opencode_results=[FakeOpenCodeResult.with_error_event("boom")] * cfg.guardrails.max_attempts,
    )

    orchestrator.process_task(task, cfg, inmem_store)

    assert len(calls.cleanup) == 0


def test_format_error_shape() -> None:
    result = FakeOpenCodeResult.with_error_event("boom-text")

    formatted = orchestrator.format_error(result)

    assert "exit_code=1" in formatted
    assert "boom-text" in formatted
    assert "error_events_count=1" in formatted


def test_branch_name_set(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    _patch_all(monkeypatch)

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.branch == f"nocturne/issue-{task.issue_number}-{result.attempts}"
    assert result.attempts == 1


def test_attempts_incremented_in_store(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    _patch_all(monkeypatch)

    orchestrator.process_task(task, cfg, inmem_store)

    persisted = inmem_store.get_task(task.id)
    assert persisted is not None
    assert persisted.attempts == 1


def test_worktree_path_includes_issue_and_attempt(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(monkeypatch)

    orchestrator.process_task(task, cfg, inmem_store)

    (_, _, _, wt_path) = calls.make_worktree[0]
    assert "ba1lly__nocturne-playground" in str(wt_path)
    assert f"issue-{task.issue_number}-1" in str(wt_path)
