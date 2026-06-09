"""Tests for nocturne.daemon — poll loop, quiet hours, budget, pause, shutdown."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest

from nocturne.models import Task, TaskStatus, TriageOutcome, TriageResult


def _tr(task_id: str, outcome: str, *, priority: int = 50, reason: str = "ok") -> TriageResult:
    return TriageResult(
        task_id=task_id,
        doable=(outcome == "DOABLE"),
        outcome=TriageOutcome(outcome),
        priority=priority,
        reason=reason,
    )


# -- fixtures --


@pytest.fixture
def fake_cfg():
    """Programmatically-built Config for daemon tests."""
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


def _make_task(issue_number: int = 1, status: str = "selected") -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=f"ba1lly/sandbox#{issue_number}",
        status=cast(TaskStatus, status),
        created_at=now,
        updated_at=now,
        repo_slug="ba1lly/sandbox",
        checkout_path="/tmp",
        issue_number=issue_number,
        title=f"issue {issue_number}",
        body="body",
        base="main",
        verify_cmd="pytest -q",
        require_new_test=False,
        coding_model="alibaba-coding-plan/qwen3-coder-plus",
        branch="",
        attempts=0,
    )


def _patch_pipeline(
    monkeypatch,
    *,
    issues=None,
    triaged=None,
    process_result=None,
    fetch_raises=None,
):
    """Patch fetch_eligible / triage_batch / process_task on the daemon module."""
    import nocturne.daemon as daemon_module
    import nocturne.orchestrator as orch
    import nocturne.sources.github_issues as gh
    import nocturne.triage as tri

    call_counts = {"fetch": 0, "triage": 0, "process": 0}
    process_calls: list[Task] = []

    def _fake_fetch(repo_cfg):
        call_counts["fetch"] += 1
        if fetch_raises is not None:
            raise fetch_raises
        return list(issues or [])

    def _fake_triage(issues_arg, cfg):
        call_counts["triage"] += 1
        return list(triaged or [])

    def _fake_process(task, cfg, store, *, dry_run=False):
        call_counts["process"] += 1
        process_calls.append(task)
        if process_result is not None:
            return process_result(task) if callable(process_result) else process_result
        # Default: mark done.
        task.status = "done"
        return task

    # Patch BOTH the source module and the daemon's local imports (daemon
    # imports inside run_one_cycle, so we patch the source modules).
    monkeypatch.setattr(gh, "fetch_eligible", _fake_fetch)
    monkeypatch.setattr(tri, "triage_batch", _fake_triage)
    monkeypatch.setattr(orch, "process_task", _fake_process)
    # Also patch the daemon module if it's already imported the names
    # (defensive — current daemon does function-scope imports so the source
    # patches above are what matter).
    _ = daemon_module
    return call_counts, process_calls


# -- construction / state tests --


def test_daemon_init_default_running(fake_cfg, inmem_store):
    from nocturne.daemon import Daemon
    d = Daemon(fake_cfg, inmem_store)
    assert d.is_paused() is False
    assert d.tokens_used == 0
    assert d.last_poll_at is None


def test_daemon_pause_sets_flag(fake_cfg, inmem_store):
    from nocturne.daemon import Daemon
    d = Daemon(fake_cfg, inmem_store)
    d.pause()
    assert inmem_store.get_daemon_flag("paused") == "1"
    assert d.is_paused() is True


def test_daemon_resume_clears_flag(fake_cfg, inmem_store):
    from nocturne.daemon import Daemon
    d = Daemon(fake_cfg, inmem_store)
    d.pause()
    d.resume()
    assert inmem_store.get_daemon_flag("paused") == "0"
    assert d.is_paused() is False


@pytest.mark.asyncio
async def test_check_paused_flag_reads_sqlite(fake_cfg, inmem_store):
    """Flag set directly via store is observed by _check_paused_flag()."""
    from nocturne.daemon import Daemon
    d = Daemon(fake_cfg, inmem_store)
    inmem_store.set_daemon_flag("paused", "1")
    assert await d._check_paused_flag() is True
    assert d.is_paused() is True
    inmem_store.set_daemon_flag("paused", "0")
    assert await d._check_paused_flag() is False
    assert d.is_paused() is False


def test_is_quiet_hour_respects_config(fake_cfg, inmem_store):
    from nocturne.daemon import Daemon
    # Empty list → never quiet.
    d = Daemon(fake_cfg, inmem_store)
    assert d._is_quiet_hour() is False
    # Set quiet_hours to include current hour.
    current_hour = datetime.now(timezone.utc).hour
    fake_cfg.daemon.quiet_hours = [current_hour]
    assert d._is_quiet_hour() is True
    # Set to a different hour.
    fake_cfg.daemon.quiet_hours = [(current_hour + 12) % 24]
    assert d._is_quiet_hour() is False


def test_is_budget_exhausted_at_threshold(fake_cfg, inmem_store):
    from nocturne.daemon import Daemon
    d = Daemon(fake_cfg, inmem_store)
    d._tokens_used = fake_cfg.guardrails.token_budget
    assert d._is_budget_exhausted() is False
    d._tokens_used = fake_cfg.guardrails.token_budget + 1
    assert d._is_budget_exhausted() is True


# -- run_one_cycle behavioural tests --


@pytest.mark.asyncio
async def test_run_one_cycle_paused_returns_paused_marker(
    fake_cfg, inmem_store, monkeypatch
):
    from nocturne.daemon import Daemon
    counts, _ = _patch_pipeline(monkeypatch, issues=[], triaged=[])
    d = Daemon(fake_cfg, inmem_store)
    inmem_store.set_daemon_flag("paused", "1")
    summary = await d.run_one_cycle()
    assert summary["paused"] is True
    assert counts["process"] == 0
    assert counts["fetch"] == 0


@pytest.mark.asyncio
async def test_run_one_cycle_quiet_hour_skips(fake_cfg, inmem_store, monkeypatch):
    from nocturne.daemon import Daemon
    counts, _ = _patch_pipeline(monkeypatch, issues=[], triaged=[])
    fake_cfg.daemon.quiet_hours = [datetime.now(timezone.utc).hour]
    d = Daemon(fake_cfg, inmem_store)
    summary = await d.run_one_cycle()
    assert summary["quiet_hour"] is True
    assert counts["process"] == 0
    assert counts["fetch"] == 0


@pytest.mark.asyncio
async def test_run_one_cycle_budget_halts(fake_cfg, inmem_store, monkeypatch):
    from nocturne.daemon import Daemon
    counts, _ = _patch_pipeline(monkeypatch, issues=[], triaged=[])
    d = Daemon(fake_cfg, inmem_store)
    d._tokens_used = fake_cfg.guardrails.token_budget + 1
    summary = await d.run_one_cycle()
    assert summary["budget_exhausted"] is True
    assert counts["process"] == 0
    assert counts["fetch"] == 0


@pytest.mark.asyncio
async def test_run_one_cycle_processes_doable(fake_cfg, inmem_store, monkeypatch):
    from nocturne.daemon import Daemon
    task = _make_task(1)
    tr = _tr(task.id, "DOABLE")
    counts, process_calls = _patch_pipeline(
        monkeypatch, issues=[task], triaged=[(task, tr)]
    )
    d = Daemon(fake_cfg, inmem_store)
    summary = await d.run_one_cycle()
    assert summary["fetched"] == 1
    assert summary["doable"] == 1
    assert summary["processed_done"] == 1
    assert summary["processed_failed"] == 0
    assert counts["process"] == 1
    assert process_calls[0].id == task.id
    assert d.last_poll_at is not None


@pytest.mark.asyncio
async def test_run_one_cycle_skips_non_doable(fake_cfg, inmem_store, monkeypatch):
    from nocturne.daemon import Daemon
    t_skip = _make_task(1)
    t_need = _make_task(2)
    triaged = [
        (t_skip, _tr(t_skip.id, "SKIP", priority=10, reason="r")),
        (t_need, _tr(t_need.id, "NEED_INPUT", priority=20, reason="r")),
    ]
    counts, _ = _patch_pipeline(
        monkeypatch, issues=[t_skip, t_need], triaged=triaged
    )
    d = Daemon(fake_cfg, inmem_store)
    summary = await d.run_one_cycle()
    assert summary["skip"] == 1
    assert summary["need_input"] == 1
    assert summary["doable"] == 0
    assert counts["process"] == 0


@pytest.mark.asyncio
async def test_run_one_cycle_budget_halt_mid_batch(
    fake_cfg, inmem_store, monkeypatch
):
    from nocturne.daemon import Daemon
    tasks = [_make_task(i) for i in range(1, 6)]
    triaged = [(t, _tr(t.id, "DOABLE")) for t in tasks]
    counts, _ = _patch_pipeline(monkeypatch, issues=tasks, triaged=triaged)
    # Budget allows ~2 tasks (each costs 5000 heuristic tokens).
    fake_cfg.guardrails.token_budget = 7000
    d = Daemon(fake_cfg, inmem_store)
    summary = await d.run_one_cycle()
    assert summary.get("budget_exhausted") is True
    assert counts["process"] < 5
    assert counts["process"] >= 1


@pytest.mark.asyncio
async def test_run_one_cycle_fetch_failure_collected_in_errors(
    fake_cfg, inmem_store, monkeypatch
):
    from nocturne.daemon import Daemon
    _patch_pipeline(
        monkeypatch,
        fetch_raises=RuntimeError("network down"),
    )
    d = Daemon(fake_cfg, inmem_store)
    summary = await d.run_one_cycle()
    assert summary["errors"]
    assert "fetch:" in summary["errors"][0]
    assert "network down" in summary["errors"][0]


@pytest.mark.asyncio
async def test_run_one_cycle_process_failure_collected(
    fake_cfg, inmem_store, monkeypatch
):
    """process_task raising → captured in errors; cycle continues."""
    from nocturne.daemon import Daemon
    task = _make_task(1)
    tr = _tr(task.id, "DOABLE")

    def _boom(task):
        raise RuntimeError("opencode crashed")

    counts, _ = _patch_pipeline(
        monkeypatch,
        issues=[task],
        triaged=[(task, tr)],
        process_result=_boom,
    )
    d = Daemon(fake_cfg, inmem_store)
    summary = await d.run_one_cycle()
    assert counts["process"] == 1
    assert summary["errors"]
    assert "process:" in summary["errors"][0]
    assert "opencode crashed" in summary["errors"][0]


@pytest.mark.asyncio
async def test_run_one_cycle_failed_task_counted(
    fake_cfg, inmem_store, monkeypatch
):
    """process_task returning failed status → counted in processed_failed."""
    from nocturne.daemon import Daemon
    task = _make_task(1)
    tr = _tr(task.id, "DOABLE")

    def _make_failed(t):
        t.status = "failed"
        return t

    _patch_pipeline(
        monkeypatch,
        issues=[task],
        triaged=[(task, tr)],
        process_result=_make_failed,
    )
    d = Daemon(fake_cfg, inmem_store)
    summary = await d.run_one_cycle()
    assert summary["processed_done"] == 0
    assert summary["processed_failed"] == 1


# -- poll loop / shutdown tests --


@pytest.mark.asyncio
async def test_poll_loop_stops_on_stop_event(fake_cfg, inmem_store, monkeypatch):
    """_stop event terminates _poll_loop within bounded time."""
    from nocturne.daemon import Daemon
    _patch_pipeline(monkeypatch, issues=[], triaged=[])
    # Short poll interval so the sleep wait_for picks up _stop quickly.
    fake_cfg.daemon.poll_interval_sec = 1
    d = Daemon(fake_cfg, inmem_store)

    async def _stopper():
        await asyncio.sleep(0.1)
        d._stop.set()

    stopper = asyncio.create_task(_stopper())
    await asyncio.wait_for(d._poll_loop(), timeout=2.0)
    await stopper


@pytest.mark.asyncio
async def test_run_with_no_bot_does_not_crash(fake_cfg, inmem_store, monkeypatch):
    """daemon.run() without a bot returns cleanly when _stop is set."""
    from nocturne.daemon import Daemon
    _patch_pipeline(monkeypatch, issues=[], triaged=[])
    fake_cfg.daemon.poll_interval_sec = 1
    d = Daemon(fake_cfg, inmem_store)

    async def _stopper():
        await asyncio.sleep(0.1)
        d._stop.set()

    stopper = asyncio.create_task(_stopper())
    await asyncio.wait_for(d.run(), timeout=3.0)
    await stopper


@pytest.mark.asyncio
async def test_poll_loop_paused_observes_resume(
    fake_cfg, inmem_store, monkeypatch
):
    """While paused, loop polls SQLite; flipping flag '0' must unblock it."""
    from nocturne.daemon import Daemon
    counts, _ = _patch_pipeline(monkeypatch, issues=[], triaged=[])
    fake_cfg.daemon.poll_interval_sec = 1
    d = Daemon(fake_cfg, inmem_store)
    # Pause via SQLite before starting the loop.
    inmem_store.set_daemon_flag("paused", "1")

    async def _resume_then_stop():
        await asyncio.sleep(0.2)
        # Resume by flipping the SQLite flag directly (simulating another
        # process).
        inmem_store.set_daemon_flag("paused", "0")
        # Give the loop multiple pause-ticks (≤1s each) to observe the flag,
        # then enough time to run the actual cycle.
        await asyncio.sleep(1.5)
        d._stop.set()

    runner = asyncio.create_task(d._poll_loop())
    helper = asyncio.create_task(_resume_then_stop())
    await asyncio.wait_for(runner, timeout=5.0)
    await helper
    # After resume, at least one cycle should have run (fetch called).
    assert counts["fetch"] >= 1


@pytest.mark.asyncio
async def test_poll_loop_unpause_refreshes_health_heartbeat_cross_process(
    fake_cfg, tmp_path
):
    from nocturne.daemon import Daemon
    from nocturne.store import Store

    db_path = tmp_path / "nocturne.db"
    daemon_store = Store(db_path)
    cli_store = Store(db_path)
    fake_cfg.daemon.poll_interval_sec = 0.1
    d = Daemon(fake_cfg, daemon_store)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _long_cycle():
        started.set()
        await release.wait()
        return {"fetched": 0, "errors": []}

    d.run_one_cycle = _long_cycle  # type: ignore[method-assign]
    cli_store.set_daemon_flag("paused", "0")
    runner = asyncio.create_task(d._poll_loop())
    await asyncio.wait_for(started.wait(), timeout=1.0)

    cli_store.set_daemon_flag("paused", "1")
    await asyncio.sleep(0.25)
    cli_store.set_daemon_flag("paused", "0")
    unpaused_at = datetime.now(timezone.utc)

    async def _wait_for_heartbeat() -> None:
        while d.last_poll_at is None or d.last_poll_at < unpaused_at:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_wait_for_heartbeat(), timeout=1.0)
    release.set()
    d._stop.set()
    await asyncio.wait_for(runner, timeout=1.0)
    daemon_store.close()
    cli_store.close()


@pytest.mark.asyncio
async def test_wait_for_resume_timeout(fake_cfg, inmem_store):
    """wait_for_resume returns False on timeout when still paused."""
    from nocturne.daemon import Daemon
    d = Daemon(fake_cfg, inmem_store)
    d.pause()
    result = await d.wait_for_resume(timeout=0.05)
    assert result is False


@pytest.mark.asyncio
async def test_wait_for_resume_returns_true_when_running(fake_cfg, inmem_store):
    """wait_for_resume returns True immediately when not paused."""
    from nocturne.daemon import Daemon
    d = Daemon(fake_cfg, inmem_store)
    result = await d.wait_for_resume(timeout=0.05)
    assert result is True


# -- _schedule_review tests (Task 39) --


@pytest.mark.asyncio
async def test_review_scheduled_post_done(fake_cfg, inmem_store, monkeypatch):
    """A task with status='done' and pr_url schedules a review_fix_loop task."""
    import nocturne.review as review_mod
    from nocturne.daemon import Daemon

    completed = asyncio.Event()
    captured: dict = {}

    def fake_review_fix_loop(pr_url, wt, cfg, store, task_id, base):
        captured["pr_url"] = pr_url
        captured["task_id"] = task_id
        captured["base"] = base
        completed.set()
        from nocturne.review import ReviewResult
        return ReviewResult(clean=True, findings=[], raw_output="", attempts=1)

    monkeypatch.setattr(review_mod, "review_fix_loop", fake_review_fix_loop)

    d = Daemon(fake_cfg, inmem_store)
    task = _make_task(1, status="done")
    task.pr_url = "https://github.com/x/y/pull/42"

    d._schedule_review(task)

    assert task.id in d._review_inflight
    # Allow the background task to run
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    await asyncio.gather(*d._review_inflight.values(), return_exceptions=True)
    assert captured["pr_url"] == "https://github.com/x/y/pull/42"
    assert captured["task_id"] == task.id
    assert captured["base"] == task.base


@pytest.mark.asyncio
async def test_review_skip_when_disabled(fake_cfg, inmem_store):
    """cfg.review.enabled=False → _schedule_review must do nothing."""
    from nocturne.daemon import Daemon
    fake_cfg.review.enabled = False
    d = Daemon(fake_cfg, inmem_store)
    task = _make_task(1, status="done")
    task.pr_url = "https://github.com/x/y/pull/1"
    d._schedule_review(task)
    assert d._review_inflight == {}


@pytest.mark.asyncio
async def test_review_skip_when_no_pr_url(fake_cfg, inmem_store):
    """No pr_url on the task → _schedule_review must do nothing."""
    from nocturne.daemon import Daemon
    d = Daemon(fake_cfg, inmem_store)
    task = _make_task(1, status="done")
    task.pr_url = None
    d._schedule_review(task)
    assert d._review_inflight == {}
