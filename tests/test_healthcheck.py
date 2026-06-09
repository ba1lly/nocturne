"""Tests for nocturne.healthcheck — /health and /metrics endpoints."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nocturne.healthcheck import Healthcheck
from nocturne.models import Task, TaskStatus
from nocturne.store import Store


@pytest.fixture
def inmem_store():
    """In-memory SQLite store for testing."""
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def fake_cfg():
    """Programmatically-built Config for healthcheck tests."""
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


def _fake_request():
    """Build a fake aiohttp Request object."""
    return MagicMock()


def _fake_daemon(last_poll_age_s=0, stopped=False):  # type: ignore[no-untyped-def]
    """Build a fake Daemon with controlled last_poll_at."""
    daemon = MagicMock()
    if last_poll_age_s is None:
        daemon.last_poll_at = None
    else:
        daemon.last_poll_at = datetime.now(timezone.utc) - timedelta(seconds=last_poll_age_s)
    daemon._stop = MagicMock()
    daemon._stop.is_set.return_value = stopped
    return daemon


@pytest.mark.asyncio
async def test_healthy_200(inmem_store, fake_cfg):
    """Health endpoint returns 200 when daemon is healthy."""
    daemon = _fake_daemon(last_poll_age_s=10)  # fresh poll
    hc = Healthcheck(fake_cfg, inmem_store, daemon=daemon)
    resp = await hc.health_handler(_fake_request())
    assert resp.status == 200
    body = resp.body.decode("utf-8")
    data = json.loads(body)
    assert data["status"] == "healthy"
    assert data["daemon_alive"] is True
    assert data["sqlite_ok"] is True


@pytest.mark.asyncio
async def test_stale_503(inmem_store, fake_cfg):
    """Health endpoint returns 503 when poll is stale."""
    # poll_interval_sec=300, staleness_factor=2 → threshold=600s
    daemon = _fake_daemon(last_poll_age_s=700)  # past threshold
    hc = Healthcheck(fake_cfg, inmem_store, daemon=daemon)
    resp = await hc.health_handler(_fake_request())
    assert resp.status == 503
    body = resp.body.decode("utf-8")
    data = json.loads(body)
    assert data["status"] == "stale"


@pytest.mark.asyncio
async def test_no_last_poll_503_after_grace_period_elapses(inmem_store, fake_cfg):
    """If the daemon never completes its first poll AND the staleness
    threshold elapses, healthcheck must report stale. This is the original
    stuck-daemon detection path; the grace period only protects normal
    startup, not a permanently-broken poll loop."""
    daemon = _fake_daemon(last_poll_age_s=None)
    hc = Healthcheck(fake_cfg, inmem_store, daemon=daemon)
    hc._started_at = time.time() - (hc._staleness_threshold_s() + 60)

    resp = await hc.health_handler(_fake_request())

    assert resp.status == 503
    body = resp.body.decode("utf-8")
    data = json.loads(body)
    assert data["status"] == "stale"


@pytest.mark.asyncio
async def test_no_last_poll_200_during_startup_grace(inmem_store, fake_cfg):
    """When the daemon just started and hasn't completed its first poll yet
    (last_poll_at=None) but uptime is within the staleness threshold, the
    healthcheck must report HEALTHY. Without this, M5 Test 4's 30s sleep
    after systemctl start hits a 503 because Approach 1's first poll cycle
    takes minutes."""
    daemon = _fake_daemon(last_poll_age_s=None)
    hc = Healthcheck(fake_cfg, inmem_store, daemon=daemon)

    resp = await hc.health_handler(_fake_request())

    assert resp.status == 200
    body = resp.body.decode("utf-8")
    data = json.loads(body)
    assert data["status"] == "healthy"
    assert data["last_poll_age_s"] is not None
    assert data["last_poll_age_s"] < hc._staleness_threshold_s()


@pytest.mark.asyncio
async def test_stopped_daemon_503(inmem_store, fake_cfg):
    """Health endpoint returns 503 when daemon is stopped."""
    daemon = _fake_daemon(last_poll_age_s=10, stopped=True)
    hc = Healthcheck(fake_cfg, inmem_store, daemon=daemon)
    resp = await hc.health_handler(_fake_request())
    assert resp.status == 503
    body = resp.body.decode("utf-8")
    data = json.loads(body)
    assert data["daemon_alive"] is False


@pytest.mark.asyncio
async def test_metrics_returns_counts(inmem_store, fake_cfg):
    """Metrics endpoint returns Prometheus-format text with task counts."""
    daemon = _fake_daemon(last_poll_age_s=10)
    hc = Healthcheck(fake_cfg, inmem_store, daemon=daemon)
    resp = await hc.metrics_handler(_fake_request())
    text = resp.text
    assert "nocturne_tasks" in text
    assert 'status="done"' in text
    assert 'status="failed"' in text
    assert 'status="parked"' in text


@pytest.mark.asyncio
async def test_health_includes_queue_depth(inmem_store, fake_cfg):
    """Health endpoint includes queue_depth from selected tasks."""
    # Insert a selected task
    t = Task(
        id="x/y#1",
        repo_slug="x/y",
        checkout_path="/tmp/x",
        issue_number=1,
        title="t",
        body="b",
        base="main",
        verify_cmd="pytest",
        require_new_test=False,
        coding_model="x/y",
        branch="b",
        status="selected",
        attempts=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    inmem_store.insert_task(t)
    daemon = _fake_daemon(last_poll_age_s=10)
    hc = Healthcheck(fake_cfg, inmem_store, daemon=daemon)
    resp = await hc.health_handler(_fake_request())
    data = json.loads(resp.body.decode("utf-8"))
    assert data["queue_depth"] == 1


@pytest.mark.asyncio
async def test_loopback_default(fake_cfg):
    """Confirm bind_host default is 127.0.0.1 (loopback only)."""
    assert fake_cfg.healthcheck.bind_host == "127.0.0.1"
    assert fake_cfg.healthcheck.bind_port == 8765


@pytest.mark.asyncio
async def test_start_stop_lifecycle(inmem_store, fake_cfg):
    """Smoke test: start binds to a port, stop cleans up."""
    # Use port 0 (ephemeral) to avoid conflicts
    fake_cfg.healthcheck.bind_port = 0
    daemon = _fake_daemon(last_poll_age_s=10)
    hc = Healthcheck(fake_cfg, inmem_store, daemon=daemon)
    try:
        await hc.start()
        assert hc._runner is not None
        assert hc._site is not None
    finally:
        await hc.stop()
        assert hc._runner is None


@pytest.mark.asyncio
async def test_health_includes_parked_count(inmem_store, fake_cfg):
    """Health endpoint includes parked_count from parked tasks."""
    # Insert a parked task
    t = Task(
        id="x/y#2",
        repo_slug="x/y",
        checkout_path="/tmp/x",
        issue_number=2,
        title="t",
        body="b",
        base="main",
        verify_cmd="pytest",
        require_new_test=False,
        coding_model="x/y",
        branch="b",
        status="parked",
        attempts=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    inmem_store.insert_task(t)
    daemon = _fake_daemon(last_poll_age_s=10)
    hc = Healthcheck(fake_cfg, inmem_store, daemon=daemon)
    resp = await hc.health_handler(_fake_request())
    data = json.loads(resp.body.decode("utf-8"))
    assert data["parked_count"] == 1
