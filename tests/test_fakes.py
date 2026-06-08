from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from nocturne.models import OpenCodeResult
from nocturne.store import Store

from tests.fakes import FakeGhResult, FakeOpenCodeResult, FakeOpenAI, RecordingSubprocess


def test_fake_opencode_success_builds_expected_result() -> None:
    result = FakeOpenCodeResult.success("hello")

    assert isinstance(result, OpenCodeResult)
    assert result.exit_code == 0
    assert result.sentinel_seen is False
    assert result.events


def test_fake_opencode_with_sentinel_marks_need_input() -> None:
    result = FakeOpenCodeResult.with_sentinel("Why?")

    assert result.sentinel_seen is True
    assert result.need_input_question == "Why?"
    assert "##NOCTURNE_NEED_INPUT##" in result.events[-1]["text"]


def test_fake_gh_rate_limited_looks_like_failed_completed_process() -> None:
    result = FakeGhResult.rate_limited()

    assert result.returncode != 0
    assert "rate limit" in result.stderr


def test_fake_gh_auth_failed_contains_http_401() -> None:
    result = FakeGhResult.auth_failed()

    assert "HTTP 401" in result.stderr


def test_inmem_store_fixture_starts_empty(inmem_store: Store) -> None:
    assert inmem_store.list_by_status("selected") == []


def test_mock_subprocess_records_invocations(mock_subprocess: RecordingSubprocess) -> None:
    result = subprocess.run(["echo", "x"])

    assert result.returncode == 0
    assert mock_subprocess.calls[0][0] == ["echo", "x"]


def test_tmp_worktree_fixture_has_initial_commit(tmp_worktree) -> None:
    assert (tmp_worktree / ".git").is_dir()
    log = subprocess.run(["git", "-C", str(tmp_worktree), "log", "--oneline"], capture_output=True, text=True, check=True)
    assert len([line for line in log.stdout.splitlines() if line.strip()]) == 1


def test_fake_clock_freezes_and_advances(fake_clock) -> None:
    from nocturne import guardrails, store

    frozen = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)

    assert store.datetime.now(timezone.utc) == frozen
    assert guardrails.datetime.now(timezone.utc) == frozen

    fake_clock.advance(60)

    assert store.datetime.now(timezone.utc) == frozen.replace(minute=1)
    assert store.datetime.utcnow() == datetime(2026, 6, 8, 12, 1)


def test_fake_openai_queue_returns_scripted_content(mock_openai: FakeOpenAI) -> None:
    mock_openai.responses.append("ready")

    response = mock_openai.chat.completions.create(model="x", messages=[])

    assert response.choices[0].message.content == "ready"
