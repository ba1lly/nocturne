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
from nocturne.sources.github_issues import IssueSnapshot
from nocturne.store import Store
from tests.fakes import FakeOpenCodeResult


@pytest.fixture
def cfg(tmp_worktree: Path, tmp_path: Path) -> Config:
    return Config(
        github=GitHubConfig(owner="owner"),
        sandbox=SandboxConfig(repo_name="nocturne-playground", checkout_path=str(tmp_worktree)),
        providers={"alibaba-coding-plan": ProviderConfig(base_url="https://example.test", api_key_env="DASHSCOPE_API_KEY")},
        models=ModelsConfig(
            reasoning="alibaba-coding-plan/qwen3-reasoning-plus",
            coding="alibaba-coding-plan/qwen3-coder-plus",
            report="alibaba-coding-plan/qwen3-report-plus",
        ),
        opencode=OpenCodeConfig(command="opencode", timeout_min=1, worktree_root=str(tmp_path / "wt-root")),
        repos=[RepoConfig(slug="owner/nocturne-playground", checkout_path=str(tmp_worktree), verify_cmd="pytest -q")],
        guardrails=GuardrailsConfig(max_attempts=3),
        discord=DiscordConfig(channel_id=1, mention_user_id=1),
        daemon=DaemonConfig(),
        review=ReviewConfig(),
        healthcheck=HealthcheckConfig(),
        persona=PersonaConfig(enabled=False),
    )


def _make_task(checkout_path: Path, *, task_id: str = "issue#42", repo_slug: str = "owner/nocturne-playground", status: str = "selected", base: str = "main") -> Task:
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
    pr_url: str = "https://github.com/owner/nocturne-playground/pull/9",
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

    def fake_verify(task: Task, wt: Path, *, strip_env: Any = ()) -> VerifyResult:
        calls.verify.append((task.id, wt))
        return next(v_iter)

    def fake_commit_push(wt: Path, message: str, base: str) -> None:
        calls.commit_push.append((wt, message, base))

    def fake_open_pr(repo: str, branch: str, base: str, title: str, body: str) -> str:
        calls.open_pr.append((repo, branch, base, title, body))
        return pr_url

    def fake_cleanup(wt: Path, repo_path: Path) -> None:
        calls.cleanup.append((wt, repo_path))

    def fake_assert_not_main(worktree: Path, base: str) -> None:
        if raise_in_assert:
            raise GuardrailViolation("worktree on protected base branch")

    def fake_get_issue_snapshot(repo: str, issue: int) -> IssueSnapshot:
        # OPEN and still carrying the trigger label -> no drift, proceed to PR.
        return IssueSnapshot(state="OPEN", labels=("agent",))

    def fake_find_blocking_open_pr(repo: str, issue: int) -> int | None:
        return None

    monkeypatch.setattr("nocturne.orchestrator.make_worktree", fake_make_worktree)
    monkeypatch.setattr("nocturne.orchestrator.commit_push", fake_commit_push)
    monkeypatch.setattr("nocturne.orchestrator.open_pr", fake_open_pr)
    monkeypatch.setattr("nocturne.orchestrator.gitwork.cleanup", fake_cleanup)
    monkeypatch.setattr("nocturne.orchestrator.opencode_driver.run", fake_run)
    monkeypatch.setattr("nocturne.orchestrator.verifier.verify", fake_verify)
    monkeypatch.setattr("nocturne.guardrails.assert_not_main_branch", fake_assert_not_main)
    monkeypatch.setattr("nocturne.orchestrator.get_issue_snapshot", fake_get_issue_snapshot)
    monkeypatch.setattr("nocturne.orchestrator.find_blocking_open_pr", fake_find_blocking_open_pr)
    return calls


# --------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------


def test_green_path_end_to_end(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(monkeypatch)

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "done"
    assert result.pr_url == "https://github.com/owner/nocturne-playground/pull/9"

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


def test_no_cleanup_on_dry_run_success(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    calls = _patch_all(monkeypatch)

    orchestrator.process_task(task, cfg, inmem_store, dry_run=True)

    assert len(calls.cleanup) == 0, "dry-run must keep worktree so the operator can audit the would-be diff"
    assert len(calls.commit_push) == 0


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
    assert "owner__nocturne-playground" in str(wt_path)
    assert f"issue-{task.issue_number}-1" in str(wt_path)


# --------------------------------------------------------------------------------------
# run_batch tests (Task 22)
# --------------------------------------------------------------------------------------


def _make_triage_result(task: Task, outcome: str, priority: int, reason: str = "") -> Any:
    from nocturne.models import TriageOutcome, TriageResult

    return TriageResult(
        task_id=task.id,
        doable=(outcome == "DOABLE"),
        outcome=TriageOutcome(outcome),
        priority=priority,
        reason=reason or f"{outcome} reason",
    )


def _make_done_task(input_task: Task) -> Task:
    return input_task.model_copy(
        update={"status": "done", "pr_url": "https://github.com/x/y/pull/1"}
    )


def test_run_batch_dispatches_doable_to_process_task(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#1")
    t1 = t1.model_copy(update={"issue_number": 1})
    t2 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#2")
    t2 = t2.model_copy(update={"issue_number": 2})
    t3 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#3")
    t3 = t3.model_copy(update={"issue_number": 3})

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1, t2, t3])
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: [
            (t1, _make_triage_result(t1, "DOABLE", 80)),
            (t3, _make_triage_result(t3, "NEED_INPUT", 40, "what API?")),
            (t2, _make_triage_result(t2, "SKIP", 0, "too vague")),
        ],
    )

    process_calls: list[Task] = []

    def fake_process(task: Task, c: Config, store: Store, *, dry_run: bool = False) -> Task:
        process_calls.append(task)
        store.update_status(task.id, "done")
        return _make_done_task(task)

    monkeypatch.setattr("nocturne.orchestrator.process_task", fake_process)

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert len(process_calls) == 1
    assert process_calls[0].id == t1.id
    assert len(report.done) == 1
    assert report.done[0].id == t1.id
    assert len(report.skipped) == 1
    assert report.skipped[0] == (2, "too vague")
    assert len(report.parked) == 1
    assert report.parked[0].id == t3.id
    assert report.parked[0].question == "what API?"
    assert report.errors == []


def test_run_batch_skip_not_passed_to_process_task(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#10").model_copy(update={"issue_number": 10})
    t2 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#11").model_copy(update={"issue_number": 11})

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1, t2])
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: [
            (t1, _make_triage_result(t1, "SKIP", 0, "out of scope")),
            (t2, _make_triage_result(t2, "SKIP", 0, "design needed")),
        ],
    )

    process_calls: list[Task] = []
    monkeypatch.setattr(
        "nocturne.orchestrator.process_task",
        lambda task, c, store, *, dry_run=False: (process_calls.append(task), task)[1],
    )

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert process_calls == []
    assert len(report.skipped) == 2
    assert {entry[0] for entry in report.skipped} == {10, 11}
    assert report.done == []
    assert report.parked == []


def test_run_batch_need_input_posts_question_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#3").model_copy(update={"issue_number": 3})

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1])
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: [
            (t1, _make_triage_result(t1, "NEED_INPUT", 25, "Which functions should I improve?")),
        ],
    )

    posted: list[tuple[str, int, str]] = []

    def recorder(repo_slug: str, issue_number: int, question: str) -> None:
        posted.append((repo_slug, issue_number, question))

    monkeypatch.setattr("nocturne.askflow.post_park_comment", recorder)

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert len(posted) == 1
    assert posted[0][1] == 3
    assert posted[0][2] == "Which functions should I improve?"
    assert len(report.parked) == 1
    assert report.parked[0].question == "Which functions should I improve?"

    persisted = inmem_store.get_task(t1.id)
    assert persisted is not None
    assert persisted.status == "parked"


def test_run_batch_need_input_dry_run_does_not_post(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#3").model_copy(update={"issue_number": 3})

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1])
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: [
            (t1, _make_triage_result(t1, "NEED_INPUT", 25, "What's the spec?")),
        ],
    )

    posted: list[tuple[str, int, str]] = []

    def recorder(repo_slug: str, issue_number: int, question: str) -> None:
        posted.append((repo_slug, issue_number, question))

    monkeypatch.setattr("nocturne.askflow.post_park_comment", recorder)

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store, dry_run=True)

    assert posted == [], "dry_run=True must not invoke post_park_comment"
    assert len(report.parked) == 1
    assert report.parked[0].question == "What's the spec?"


def test_run_batch_need_input_falls_back_when_reason_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#3").model_copy(update={"issue_number": 3})

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1])
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: [
            (t1, _make_triage_result(t1, "NEED_INPUT", 25, "")),
        ],
    )

    posted: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        "nocturne.askflow.post_park_comment",
        lambda repo_slug, issue_number, question: posted.append((repo_slug, issue_number, question)),
    )

    orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert len(posted) == 1
    assert posted[0][2], "fallback question must be non-empty"


def test_run_batch_skips_already_done_tasks(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#1").model_copy(update={"issue_number": 1})
    inmem_store.insert_task(t1)
    inmem_store.update_status(t1.id, "done")

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1])

    triage_called = {"n": 0}

    def boom_triage(issues, c, **_kw):
        triage_called["n"] += 1
        return []

    monkeypatch.setattr("nocturne.orchestrator.triage_batch", boom_triage)

    process_called = {"n": 0}
    monkeypatch.setattr(
        "nocturne.orchestrator.process_task",
        lambda task, c, store, *, dry_run=False: (process_called.update({"n": process_called["n"] + 1}), task)[1],
    )

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert triage_called["n"] == 0, "already-done task must not be re-triaged"
    assert process_called["n"] == 0, "already-done task must not be re-processed"
    assert report.errors == []


def test_run_batch_processes_resumed_task_without_re_triage(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t3 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#3").model_copy(update={"issue_number": 3})
    inmem_store.insert_task(t3)
    inmem_store.park_task(t3.id, "what spec?")
    inmem_store.resume_task(t3.id, "Add median(values) returning the median.")

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t3])

    triage_called = {"n": 0}
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: (triage_called.update({"n": triage_called["n"] + 1}), [])[1],
    )

    process_called: list[Task] = []

    def fake_process(task: Task, c: Config, store: Store, *, dry_run: bool = False) -> Task:
        process_called.append(task)
        store.update_status(task.id, "done")
        return _make_done_task(task)

    monkeypatch.setattr("nocturne.orchestrator.process_task", fake_process)

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert triage_called["n"] == 0, "resumed task must not be re-triaged"
    assert len(process_called) == 1
    assert process_called[0].id == t3.id
    assert (process_called[0].answer or "").startswith("Add median"), \
        "process_task must receive the resumed task row including the persisted answer"
    assert len(report.done) == 1
    assert report.done[0].id == t3.id


def test_run_batch_mixed_fresh_resumed_and_done(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#1").model_copy(update={"issue_number": 1})
    t2 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#2").model_copy(update={"issue_number": 2})
    t3 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#3").model_copy(update={"issue_number": 3})

    inmem_store.insert_task(t1)
    inmem_store.update_status(t1.id, "done")
    inmem_store.insert_task(t3)
    inmem_store.park_task(t3.id, "?")
    inmem_store.resume_task(t3.id, "answer")

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1, t2, t3])

    triage_inputs: list[list[Task]] = []

    def captured_triage(issues, c, **_kw):
        triage_inputs.append(list(issues))
        return [(issues[0], _make_triage_result(issues[0], "DOABLE", 80))]

    monkeypatch.setattr("nocturne.orchestrator.triage_batch", captured_triage)

    processed: list[Task] = []

    def fake_process(task: Task, c: Config, store: Store, *, dry_run: bool = False) -> Task:
        processed.append(task)
        store.update_status(task.id, "done")
        return _make_done_task(task)

    monkeypatch.setattr("nocturne.orchestrator.process_task", fake_process)

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert len(triage_inputs) == 1
    assert [t.id for t in triage_inputs[0]] == [t2.id], "only fresh issue #2 should hit triage"
    assert {t.id for t in processed} == {t2.id, t3.id}
    assert {t.id for t in report.done} == {t2.id, t3.id}
    assert report.errors == []


def test_process_task_inserts_task_if_missing_from_store(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    """Regression guard for the daemon-reprocess loop observed in M5 Test 3
    (the run that produced PR #30 then thrashed forever on issue #1).

    process_task is called from TWO paths:
      - orchestrator.run_batch via _dispatch_triaged, which DOES insert.
      - daemon.run_one_cycle directly, which did NOT insert.

    Without insert, the next daemon cycle's partition_eligible sees
    get_task(id) == None and re-queues the same issue forever. The cleanest
    fix is to make process_task self-sufficient: insert if missing, no-op
    if already there. Belt + suspenders for both call paths.
    """
    _patch_all(monkeypatch)
    fresh_task = _make_task(tmp_worktree, task_id="owner/repo#daemon-fresh").model_copy(
        update={"issue_number": 99},
    )
    assert inmem_store.get_task(fresh_task.id) is None, "precondition: task not in store"

    orchestrator.process_task(fresh_task, cfg, inmem_store)

    persisted = inmem_store.get_task(fresh_task.id)
    assert persisted is not None, "process_task must insert when row is missing"
    assert persisted.status == "done"


def test_process_task_does_not_double_insert_when_task_already_in_store(
    monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store
) -> None:
    """The defensive insert must be a no-op if the row already exists,
    so the run_batch path (which inserts via _dispatch_triaged before
    calling process_task) doesn't blow up on UNIQUE."""
    _patch_all(monkeypatch)
    assert inmem_store.get_task(task.id) is not None, \
        "precondition: 'task' fixture pre-inserts via inmem_store.insert_task"

    orchestrator.process_task(task, cfg, inmem_store)

    persisted = inmem_store.get_task(task.id)
    assert persisted is not None
    assert persisted.status == "done"


def test_process_task_writes_review_runs_row_on_success(
    monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store
) -> None:
    """Approach 1 review-runs observability: after each successful PR
    creation, process_task writes a review_runs row signalling that the
    inline review path ran. M5 Test 3 Phase B (poll for review_runs.ended_at)
    relies on this."""
    _patch_all(monkeypatch)

    orchestrator.process_task(task, cfg, inmem_store)

    import sqlite3
    rows = inmem_store._conn.execute(
        "SELECT pr_url, attempts, clean, ended_at FROM review_runs"
    ).fetchall()
    assert len(rows) == 1
    pr_url, attempts, clean, ended_at = rows[0]
    assert pr_url == task.pr_url
    assert attempts == 1
    assert clean == 1
    assert ended_at is not None


def test_process_task_dry_run_does_not_write_review_runs(
    monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store
) -> None:
    _patch_all(monkeypatch)

    orchestrator.process_task(task, cfg, inmem_store, dry_run=True)

    count = inmem_store._conn.execute(
        "SELECT COUNT(*) FROM review_runs"
    ).fetchone()[0]
    assert count == 0, "dry-run must not write any review_runs rows"


def test_read_pr_body_parses_title_and_body_from_markdown(
    tmp_path: Path, tmp_worktree: Path,
) -> None:
    """opencode writes .nocturne-pr-body.md per task.md.jinja2 Step 6.
    First '# ' line is the PR title (and commit subject). Everything below
    is the GitHub PR body."""
    from nocturne.orchestrator import _read_pr_body

    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".nocturne-pr-body.md").write_text(
        "# Fix the divide() off-by-one bug\n"
        "\n"
        "The divide function returned `a / (b + 1)` instead of `a / b`.\n"
        "This patch corrects the formula and adds tests for the\n"
        "acceptance criteria.\n"
        "\n"
        "Closes #42\n",
        encoding="utf-8",
    )
    task = _make_task(tmp_worktree, task_id="owner/repo#42").model_copy(update={"issue_number": 42})

    title, body = _read_pr_body(wt, task)

    assert title == "Fix the divide() off-by-one bug"
    assert "divide function returned" in body
    assert "Closes #42" in body
    assert title not in body, "the H1 title must NOT be duplicated in the body"


def test_read_pr_body_falls_back_when_file_missing(tmp_path: Path, tmp_worktree: Path) -> None:
    from nocturne.orchestrator import _read_pr_body

    wt = tmp_path / "wt"
    wt.mkdir()
    task = _make_task(tmp_worktree, task_id="owner/repo#7").model_copy(
        update={"issue_number": 7, "title": "Add multiply()"},
    )

    title, body = _read_pr_body(wt, task)

    assert title == "closes #7: Add multiply()"
    assert "Closes #7" in body
    assert "Nocturne" in body


def test_read_pr_body_falls_back_when_no_h1_heading(tmp_path: Path, tmp_worktree: Path) -> None:
    """If opencode wrote the file but forgot the H1 line, fall back to the
    legacy title and keep the file's content as the body so nothing is lost."""
    from nocturne.orchestrator import _read_pr_body

    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".nocturne-pr-body.md").write_text(
        "Just some prose without an H1 heading.\n",
        encoding="utf-8",
    )
    task = _make_task(tmp_worktree, task_id="owner/repo#3").model_copy(
        update={"issue_number": 3, "title": "Improve math"},
    )

    title, body = _read_pr_body(wt, task)

    assert title == "closes #3: Improve math"
    assert "Just some prose" in body


def test_partition_eligible_respects_all_lifecycle_states(
    tmp_worktree: Path, inmem_store: Store,
) -> None:
    """Regression guard for the daemon-reprocess bug observed in M5 Test 3.

    The daemon log showed it re-processed already-done tasks in cycle 2
    because the daemon called triage_batch directly instead of going
    through partition_eligible. This test pins the helper's contract so
    both run_batch and daemon.run_one_cycle stay in lockstep.
    """
    t1 = _make_task(tmp_worktree, task_id="owner/repo#1").model_copy(update={"issue_number": 1})
    t2 = _make_task(tmp_worktree, task_id="owner/repo#2").model_copy(update={"issue_number": 2})
    t3 = _make_task(tmp_worktree, task_id="owner/repo#3").model_copy(update={"issue_number": 3})
    t4 = _make_task(tmp_worktree, task_id="owner/repo#4").model_copy(update={"issue_number": 4})
    t5 = _make_task(tmp_worktree, task_id="owner/repo#5").model_copy(update={"issue_number": 5})
    t6 = _make_task(tmp_worktree, task_id="owner/repo#6").model_copy(update={"issue_number": 6})

    inmem_store.insert_task(t1)
    inmem_store.update_status(t1.id, "done")
    inmem_store.insert_task(t2)
    inmem_store.park_task(t2.id, "?")
    inmem_store.insert_task(t3)
    inmem_store.park_task(t3.id, "?")
    inmem_store.resume_task(t3.id, "real answer")
    inmem_store.insert_task(t5)
    inmem_store.update_status(t5.id, "failed")
    inmem_store.insert_task(t6)
    inmem_store.update_status(t6.id, "aborted")

    fetched = [t1, t2, t3, t4, t5, t6]

    to_triage, resumed = orchestrator.partition_eligible(fetched, inmem_store)

    assert {t.id for t in to_triage} == {t4.id}, "only fresh #4 should be triaged"
    assert {t.id for t in resumed} == {t3.id}, "only resumed #3 should bypass triage"
    assert (resumed[0].answer or "").startswith("real answer"), "must return stored row with persisted answer"


def test_run_batch_ordering_respected(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#1").model_copy(update={"issue_number": 1})
    t2 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#2").model_copy(update={"issue_number": 2})
    t3 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#3").model_copy(update={"issue_number": 3})

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1, t2, t3])
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: [
            (t2, _make_triage_result(t2, "DOABLE", 90)),
            (t3, _make_triage_result(t3, "DOABLE", 50)),
            (t1, _make_triage_result(t1, "SKIP", 0)),
        ],
    )

    process_calls: list[Task] = []

    def fake_process(task: Task, c: Config, store: Store, *, dry_run: bool = False) -> Task:
        process_calls.append(task)
        store.update_status(task.id, "done")
        return _make_done_task(task)

    monkeypatch.setattr("nocturne.orchestrator.process_task", fake_process)

    orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert len(process_calls) == 2
    assert process_calls[0].id == t2.id
    assert process_calls[1].id == t3.id


def test_run_batch_wallclock_aborts_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    cfg.guardrails.global_wallclock_hours = 0
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#1").model_copy(update={"issue_number": 1})
    t2 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#2").model_copy(update={"issue_number": 2})

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1, t2])
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: [
            (t1, _make_triage_result(t1, "DOABLE", 80)),
            (t2, _make_triage_result(t2, "DOABLE", 70)),
        ],
    )

    def fake_check_wallclock(started_at, c):
        raise GuardrailViolation("wallclock exhausted")

    monkeypatch.setattr("nocturne.orchestrator.check_wallclock", fake_check_wallclock)

    process_calls: list[Task] = []
    monkeypatch.setattr(
        "nocturne.orchestrator.process_task",
        lambda task, c, store, *, dry_run=False: (process_calls.append(task), task)[1],
    )

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert process_calls == []
    assert any("wallclock" in e for e in report.errors)


def test_run_batch_fetch_failure_returns_empty_report(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, inmem_store: Store
) -> None:
    from nocturne.sources.github_issues import GhError

    def boom(repo_cfg):
        raise GhError("gh CLI not authenticated")

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", boom)

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert report.done == []
    assert report.parked == []
    assert report.skipped == []
    assert len(report.errors) == 1
    assert "fetch_eligible" in report.errors[0]


def test_run_batch_individual_task_failure_continues_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#1").model_copy(update={"issue_number": 1})
    t2 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#2").model_copy(update={"issue_number": 2})

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1, t2])
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: [
            (t1, _make_triage_result(t1, "DOABLE", 90)),
            (t2, _make_triage_result(t2, "DOABLE", 80)),
        ],
    )

    process_calls: list[Task] = []

    def fake_process(task: Task, c: Config, store: Store, *, dry_run: bool = False) -> Task:
        process_calls.append(task)
        if task.id == t1.id:
            raise RuntimeError("disk full")
        store.update_status(task.id, "done")
        return _make_done_task(task)

    monkeypatch.setattr("nocturne.orchestrator.process_task", fake_process)

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert len(process_calls) == 2
    assert len(report.done) == 1
    assert report.done[0].id == t2.id
    assert len(report.errors) == 1
    assert t1.id in report.errors[0]
    assert "RuntimeError" in report.errors[0]


def test_run_batch_dry_run_forwards_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#1").model_copy(update={"issue_number": 1})

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1])
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: [(t1, _make_triage_result(t1, "DOABLE", 80))],
    )

    captured_kwargs: list[dict[str, Any]] = []

    def fake_process(task: Task, c: Config, store: Store, *, dry_run: bool = False) -> Task:
        captured_kwargs.append({"dry_run": dry_run})
        store.update_status(task.id, "done")
        return _make_done_task(task)

    monkeypatch.setattr("nocturne.orchestrator.process_task", fake_process)

    orchestrator.run_batch(cfg.repos[0], cfg, inmem_store, dry_run=True)

    assert captured_kwargs == [{"dry_run": True}]


# --------------------------------------------------------------------------------------
# Pre-PR drift guard: closed / label-removed / superseded-by-PR abort tests
# --------------------------------------------------------------------------------------


def test_aborts_on_closed_issue(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """When issue is CLOSED at check time, abort without PR."""
    calls = _patch_all(monkeypatch)
    monkeypatch.setattr(
        "nocturne.orchestrator.get_issue_snapshot",
        lambda repo, issue: IssueSnapshot(state="CLOSED", labels=("agent",)),
    )

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "aborted"
    assert len(calls.commit_push) == 0
    assert len(calls.open_pr) == 0
    assert len(calls.cleanup) == 1

    persisted = inmem_store.get_task(task.id)
    assert persisted is not None
    assert persisted.status == "aborted"


def test_404_treated_as_aborted(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """When issue returns 404 (IssueNotFound), treat as aborted."""
    from nocturne._gh_retry import IssueNotFound

    calls = _patch_all(monkeypatch)

    def fake_snapshot(repo: str, issue: int) -> IssueSnapshot:
        raise IssueNotFound("not found")

    monkeypatch.setattr("nocturne.orchestrator.get_issue_snapshot", fake_snapshot)

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "aborted"
    assert len(calls.commit_push) == 0
    assert len(calls.open_pr) == 0
    assert len(calls.cleanup) == 1

    persisted = inmem_store.get_task(task.id)
    assert persisted is not None
    assert persisted.status == "aborted"


def test_aborts_when_trigger_label_removed(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """A maintainer pulling the trigger label mid-run aborts without a PR."""
    calls = _patch_all(monkeypatch)
    monkeypatch.setattr(
        "nocturne.orchestrator.get_issue_snapshot",
        lambda repo, issue: IssueSnapshot(state="OPEN", labels=("bug", "wontfix")),
    )

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "aborted"
    assert len(calls.commit_push) == 0
    assert len(calls.open_pr) == 0
    assert len(calls.cleanup) == 1


def test_aborts_when_open_pr_already_addresses_issue(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """If a human already opened a PR that closes the issue, do not open a dup."""
    calls = _patch_all(monkeypatch)
    monkeypatch.setattr(
        "nocturne.orchestrator.find_blocking_open_pr",
        lambda repo, issue: 123,
    )

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "aborted"
    assert len(calls.commit_push) == 0
    assert len(calls.open_pr) == 0
    assert len(calls.cleanup) == 1


def test_open_proceeds_to_pr(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """When issue is OPEN, labelled, and unsuperseded, proceed with commit and PR."""
    calls = _patch_all(monkeypatch)

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "done"
    assert len(calls.commit_push) == 1
    assert len(calls.open_pr) == 1
    assert len(calls.cleanup) == 1

    persisted = inmem_store.get_task(task.id)
    assert persisted is not None
    assert persisted.status == "done"


def test_token_budget_exhausted_stops_retrying(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """A single task whose cumulative tokens reach the global budget must stop
    after the current attempt instead of retrying and blowing past it."""
    cfg.guardrails.token_budget = 10_000
    heavy = OpenCodeResult(
        exit_code=0,
        events=[{"type": "text", "text": "ok"}],
        sentinel_seen=False,
        need_input_question=None,
        pid=1,
        error_events=[],
        token_usage=12_000,  # one attempt already exceeds budget
    )
    calls = _patch_all(monkeypatch, opencode_results=[heavy, heavy, heavy])

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "failed"
    assert len(calls.opencode_run) == 1, "must not retry once budget is exhausted"
    assert len(calls.commit_push) == 0
    assert len(calls.open_pr) == 0


def test_gh_failure_defaults_to_open(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store) -> None:
    """When the snapshot recheck raises a transient GhError, proceed (open_pr is the backstop)."""
    from nocturne._gh_retry import GhRateLimited

    calls = _patch_all(monkeypatch)

    def fake_snapshot(repo: str, issue: int) -> IssueSnapshot:
        raise GhRateLimited("rate limited")

    monkeypatch.setattr("nocturne.orchestrator.get_issue_snapshot", fake_snapshot)

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert result.status == "done"
    assert len(calls.commit_push) == 1
    assert len(calls.open_pr) == 1

    persisted = inmem_store.get_task(task.id)
    assert persisted is not None
    assert persisted.status == "done"


def test_run_batch_collects_aborted_into_report(
    monkeypatch: pytest.MonkeyPatch, tmp_worktree: Path, cfg: Config, inmem_store: Store
) -> None:
    """run_batch collects aborted tasks into RunReport.aborted."""
    t1 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#1").model_copy(update={"issue_number": 1})
    t2 = _make_task(tmp_worktree, task_id="owner/nocturne-playground#2").model_copy(update={"issue_number": 2})

    monkeypatch.setattr("nocturne.orchestrator.fetch_eligible", lambda repo_cfg: [t1, t2])
    monkeypatch.setattr(
        "nocturne.orchestrator.triage_batch",
        lambda issues, c, **_kw: [
            (t1, _make_triage_result(t1, "DOABLE", 80)),
            (t2, _make_triage_result(t2, "DOABLE", 80)),
        ],
    )

    def fake_process(task: Task, c: Config, store: Store, *, dry_run: bool = False) -> Task:
        if task.id == t1.id:
            store.update_status(task.id, "aborted")
            task.status = "aborted"
        else:
            store.update_status(task.id, "done")
            task.status = "done"
        return task

    monkeypatch.setattr("nocturne.orchestrator.process_task", fake_process)

    report = orchestrator.run_batch(cfg.repos[0], cfg, inmem_store)

    assert len(report.done) == 1
    assert report.done[0].id == t2.id
    assert len(report.aborted) == 1
    assert report.aborted[0].id == t1.id


# --------------------------------------------------------------------------------------
# Post-PR feedback loop: watch registration + re-dispatch
# --------------------------------------------------------------------------------------


def test_pr_watch_registered_when_reactions_enabled(
    monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store
) -> None:
    cfg.reactions.enabled = True
    _patch_all(monkeypatch)

    result = orchestrator.process_task(task, cfg, inmem_store)

    watch = inmem_store.get_pr_watch(result.pr_url)
    assert watch is not None
    assert watch.task_id == task.id
    assert watch.pr_number == 9  # from the default mocked PR url .../pull/9
    assert watch.state == "watching"


def test_pr_watch_not_registered_when_reactions_disabled(
    monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store
) -> None:
    assert cfg.reactions.enabled is False  # default
    _patch_all(monkeypatch)

    result = orchestrator.process_task(task, cfg, inmem_store)

    assert inmem_store.get_pr_watch(result.pr_url) is None


def test_process_pr_reaction_pushes_fix_on_success(
    monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    from nocturne.models import PRWatch

    inmem_store.insert_task(task) if inmem_store.get_task(task.id) is None else None
    now = datetime.now(UTC)
    watch = PRWatch(
        pr_url="https://github.com/owner/nocturne-playground/pull/9", task_id=task.id,
        repo_slug=task.repo_slug, pr_number=9, branch="nocturne/issue-42-1", base="main",
        created_at=now, updated_at=now,
    )
    inmem_store.add_pr_watch(watch)

    made: dict[str, object] = {}

    def fake_make_from_branch(repo_path, branch, wt_path):
        made["branch"] = branch
        Path(wt_path).mkdir(parents=True, exist_ok=True)
        return Path(wt_path)

    followup: list[tuple] = []

    monkeypatch.setattr("nocturne.orchestrator.gitwork.make_worktree_from_branch", fake_make_from_branch)
    monkeypatch.setattr("nocturne.orchestrator.gitwork.commit_push_followup",
                        lambda wt, msg: followup.append((wt, msg)))
    monkeypatch.setattr("nocturne.orchestrator.gitwork.cleanup", lambda wt, repo: None)
    monkeypatch.setattr("nocturne.orchestrator.opencode_driver.run",
                        lambda *a, **k: FakeOpenCodeResult.success("fixed"))
    monkeypatch.setattr("nocturne.orchestrator.verifier.verify",
                        lambda task, wt, *, strip_env=(): VerifyResult(passed=True, exit_code=0, stdout="", stderr="", new_test_added=False))
    monkeypatch.setattr("nocturne.guardrails.assert_not_main_branch", lambda wt, base: None)

    pushed, _tokens = orchestrator.process_pr_reaction(watch, "ci", "tests failed", cfg, inmem_store)

    assert pushed is True
    assert made["branch"] == "nocturne/issue-42-1"  # re-materialised the PR branch, not base
    assert len(followup) == 1  # fast-forward fix commit pushed, no new PR


def test_process_pr_reaction_no_push_when_verify_fails(
    monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, inmem_store: Store, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    from nocturne.models import PRWatch

    if inmem_store.get_task(task.id) is None:
        inmem_store.insert_task(task)
    now = datetime.now(UTC)
    watch = PRWatch(
        pr_url="https://github.com/owner/nocturne-playground/pull/9", task_id=task.id,
        repo_slug=task.repo_slug, pr_number=9, branch="nocturne/issue-42-1", base="main",
        created_at=now, updated_at=now,
    )
    inmem_store.add_pr_watch(watch)

    followup: list[tuple] = []
    monkeypatch.setattr("nocturne.orchestrator.gitwork.make_worktree_from_branch",
                        lambda repo, branch, wt: (Path(wt).mkdir(parents=True, exist_ok=True), Path(wt))[1])
    monkeypatch.setattr("nocturne.orchestrator.gitwork.commit_push_followup",
                        lambda wt, msg: followup.append((wt, msg)))
    monkeypatch.setattr("nocturne.orchestrator.gitwork.cleanup", lambda wt, repo: None)
    monkeypatch.setattr("nocturne.orchestrator.opencode_driver.run",
                        lambda *a, **k: FakeOpenCodeResult.success("attempted"))
    monkeypatch.setattr("nocturne.orchestrator.verifier.verify",
                        lambda task, wt, *, strip_env=(): VerifyResult(passed=False, exit_code=1, stdout="", stderr="", new_test_added=False, reason="still broken"))
    monkeypatch.setattr("nocturne.guardrails.assert_not_main_branch", lambda wt, base: None)

    pushed, _tokens = orchestrator.process_pr_reaction(watch, "ci", "tests failed", cfg, inmem_store)

    assert pushed is False
    assert followup == [], "must NOT push when the fix does not pass verify"
