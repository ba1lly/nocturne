"""Tests for nocturne.triage — combined Task 20 + 21.

Covers:
  - classify() outcome semantics (DOABLE / SKIP / NEED_INPUT)
  - classify() fallback to SKIP on parse error / invalid outcome
  - triage_batch() ordering (DOABLE → NEED_INPUT → SKIP, priority desc within)
  - already_commented_skip() marker detection + gh-error safety
  - post_skip_comment() idempotency + non-blocking on failure
  - triage_batch() resilience when post_skip_comment raises
  - build_triage_graph() compiles
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nocturne._gh_retry import GhRateLimited
from nocturne.config import (
    Config,
    DaemonConfig,
    DiscordConfig,
    GuardrailsConfig,
    HealthcheckConfig,
    ModelsConfig,
    OpenCodeConfig,
    PersonaConfig,
    ProviderConfig,
    RepoConfig,
    ReviewConfig,
)
from nocturne.models import Task, TriageResult

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    task_id: str = "issue#1",
    issue_number: int = 1,
    title: str = "Fix off-by-one in divide()",
    body: str = "divide(10, 3) returns 4 instead of 3. Should floor-divide.",
) -> Task:
    now = datetime.now(UTC)
    return Task(
        id=task_id,
        repo_slug="ba1lly/nocturne-playground",
        checkout_path="/tmp",
        issue_number=issue_number,
        title=title,
        body=body,
        base="main",
        verify_cmd="pytest -q",
        require_new_test=False,
        coding_model="",
        branch="",
        status="selected",
        attempts=0,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def cfg(tmp_worktree: Path) -> Config:
    return Config(
        github={"owner": "ba1lly"},
        sandbox={"repo_name": "nocturne-playground"},
        providers={
            "alibaba-coding-plan": ProviderConfig(
                base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                api_key_env="DASHSCOPE_API_KEY",
            ),
        },
        models=ModelsConfig(
            reasoning="alibaba-coding-plan/qwen3.6-plus",
            coding="alibaba-coding-plan/qwen3-coder-plus",
            report="alibaba-coding-plan/qwen3.6-plus",
        ),
        opencode=OpenCodeConfig(),
        repos=[RepoConfig(slug="ba1lly/nocturne-playground", checkout_path=str(tmp_worktree), verify_cmd="pytest -q")],
        guardrails=GuardrailsConfig(),
        discord=DiscordConfig(channel_id=1, mention_user_id=1),
        daemon=DaemonConfig(),
        review=ReviewConfig(),
        healthcheck=HealthcheckConfig(),
        persona=PersonaConfig(enabled=False),
    )


# ---------------------------------------------------------------------------
# classify() — outcome semantics
# ---------------------------------------------------------------------------


class TestClassify:
    def test_classify_doable_for_clear_bug(self, cfg: Config, mock_openai) -> None:
        mock_openai.responses.append(
            '{"outcome":"DOABLE","priority":85,"reason":"clear bug"}'
        )
        from nocturne.triage import classify

        issue = _make_task(task_id="issue#1", issue_number=1, title="Fix off-by-one")
        result = classify(issue, cfg)

        assert isinstance(result, TriageResult)
        assert result.outcome == "DOABLE"
        assert result.doable is True
        assert result.priority == 85
        assert result.reason == "clear bug"
        assert result.task_id == "issue#1"

    def test_classify_skip_for_vague(self, cfg: Config, mock_openai) -> None:
        mock_openai.responses.append(
            '{"outcome":"SKIP","priority":5,"reason":"vague — refactor everything"}'
        )
        from nocturne.triage import classify

        issue = _make_task(task_id="issue#4", issue_number=4, title="Refactor everything")
        result = classify(issue, cfg)

        assert result.outcome == "SKIP"
        assert result.doable is False
        assert result.priority == 5

    def test_classify_need_input_for_ambiguous(self, cfg: Config, mock_openai) -> None:
        mock_openai.responses.append(
            '{"outcome":"NEED_INPUT","priority":30,"reason":"which function to improve?"}'
        )
        from nocturne.triage import classify

        issue = _make_task(task_id="issue#3", issue_number=3, title="Improve the math module")
        result = classify(issue, cfg)

        assert result.outcome == "NEED_INPUT"
        assert result.doable is False
        assert result.priority == 30

    def test_classify_invalid_json_falls_back_skip(self, cfg: Config, mock_openai) -> None:
        mock_openai.responses.append("not json at all { broken")
        from nocturne.triage import classify

        issue = _make_task()
        result = classify(issue, cfg)

        assert result.outcome == "SKIP"
        assert result.doable is False
        assert result.priority == 0
        assert "parse error" in result.reason.lower()

    def test_classify_unknown_outcome_rejected_via_pydantic(self, cfg: Config, mock_openai) -> None:
        # PARTIAL is forbidden by the TriageOutcome Literal — must fall back to SKIP
        mock_openai.responses.append(
            '{"outcome":"PARTIAL","priority":50,"reason":"split it up"}'
        )
        from nocturne.triage import classify

        issue = _make_task()
        result = classify(issue, cfg)

        assert result.outcome == "SKIP"
        assert "parse error" in result.reason.lower()

    def test_classify_priority_clamped(self, cfg: Config, mock_openai) -> None:
        # Out-of-range priority is clamped into [0,100] so pydantic accepts it.
        mock_openai.responses.append(
            '{"outcome":"DOABLE","priority":150,"reason":"super high prio"}'
        )
        from nocturne.triage import classify

        issue = _make_task()
        result = classify(issue, cfg)

        assert result.outcome == "DOABLE"
        assert result.priority == 100  # clamped to max


# ---------------------------------------------------------------------------
# triage_batch() — ordering + non-blocking comments
# ---------------------------------------------------------------------------


class TestTriageBatch:
    def test_triage_batch_orders_doable_then_need_input_then_skip(
        self, cfg: Config, mock_openai, monkeypatch
    ) -> None:
        # Suppress comment posting so this test only exercises ordering
        monkeypatch.setattr(
            "nocturne.triage.post_skip_comment", lambda *a, **kw: None
        )

        mock_openai.responses.extend(
            [
                '{"outcome":"SKIP","priority":5,"reason":"vague"}',
                '{"outcome":"DOABLE","priority":80,"reason":"clear"}',
                '{"outcome":"NEED_INPUT","priority":30,"reason":"which?"}',
            ]
        )

        from nocturne.triage import triage_batch

        issues = [
            _make_task(task_id="issue#a", issue_number=10, title="vague"),
            _make_task(task_id="issue#b", issue_number=11, title="clear"),
            _make_task(task_id="issue#c", issue_number=12, title="ambiguous"),
        ]
        result = triage_batch(issues, cfg)

        outcomes = [pair[1].outcome for pair in result]
        assert outcomes == ["DOABLE", "NEED_INPUT", "SKIP"]

    def test_triage_batch_orders_by_priority_within_outcome(
        self, cfg: Config, mock_openai, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "nocturne.triage.post_skip_comment", lambda *a, **kw: None
        )
        mock_openai.responses.extend(
            [
                '{"outcome":"DOABLE","priority":50,"reason":"medium"}',
                '{"outcome":"DOABLE","priority":90,"reason":"trivial typo"}',
            ]
        )

        from nocturne.triage import triage_batch

        issues = [
            _make_task(task_id="issue#low", issue_number=20, title="medium DOABLE"),
            _make_task(task_id="issue#high", issue_number=21, title="trivial typo"),
        ]
        result = triage_batch(issues, cfg)

        # Both DOABLE; priority=90 must come first
        assert result[0][1].priority == 90
        assert result[1][1].priority == 50

    def test_triage_batch_post_skip_comment_failure_non_blocking(
        self, cfg: Config, mock_openai, monkeypatch
    ) -> None:
        # One SKIP issue; post_skip_comment raises but triage_batch must NOT raise
        def boom(*a, **kw):
            raise RuntimeError("comment subsystem exploded")

        monkeypatch.setattr("nocturne.triage.post_skip_comment", boom)

        mock_openai.responses.append(
            '{"outcome":"SKIP","priority":3,"reason":"too big"}'
        )

        from nocturne.triage import triage_batch

        issues = [_make_task(task_id="issue#skip", issue_number=99, title="huge refactor")]
        result = triage_batch(issues, cfg)

        # Did not raise; the triaged pair is still returned
        assert len(result) == 1
        assert result[0][1].outcome == "SKIP"


# ---------------------------------------------------------------------------
# already_commented_skip() — marker detection + safety
# ---------------------------------------------------------------------------


class TestAlreadyCommentedSkip:
    def test_already_commented_skip_detects_marker(self, monkeypatch) -> None:
        from nocturne.triage import NOCTURNE_SKIP_MARKER, already_commented_skip

        def fake_run_gh(args, **kwargs):
            return f"{NOCTURNE_SKIP_MARKER}\n[Nocturne triage] Skipped: too big\n"

        monkeypatch.setattr("nocturne.triage.run_gh", fake_run_gh)

        assert already_commented_skip("ba1lly/nocturne-playground", 4) is True

    def test_already_commented_skip_returns_false_when_absent(self, monkeypatch) -> None:
        from nocturne.triage import already_commented_skip

        # Empty stdout means no matching comment from the --jq filter
        monkeypatch.setattr("nocturne.triage.run_gh", lambda args, **kw: "")

        assert already_commented_skip("ba1lly/nocturne-playground", 5) is False

    def test_already_commented_skip_safe_on_gh_error(self, monkeypatch) -> None:
        from nocturne.triage import already_commented_skip

        def raises(args, **kwargs):
            raise GhRateLimited("rate limited")

        monkeypatch.setattr("nocturne.triage.run_gh", raises)

        # Must NOT raise; return False so caller can attempt to post
        assert already_commented_skip("ba1lly/nocturne-playground", 6) is False


# ---------------------------------------------------------------------------
# post_skip_comment() — idempotency + non-blocking
# ---------------------------------------------------------------------------


class TestPostSkipComment:
    def test_post_skip_comment_idempotent_when_already_present(self, monkeypatch) -> None:
        from nocturne.triage import post_skip_comment

        calls: list[list[str]] = []

        def record_run_gh(args, **kwargs):
            calls.append(list(args))
            return ""

        monkeypatch.setattr("nocturne.triage.already_commented_skip", lambda r, n: True)
        monkeypatch.setattr("nocturne.triage.run_gh", record_run_gh)

        post_skip_comment("ba1lly/nocturne-playground", 4, "too big")

        # `gh issue comment` MUST NOT have been called
        assert not any("comment" in a for call in calls for a in call), (
            f"expected no gh issue comment call, got: {calls}"
        )

    def test_post_skip_comment_posts_when_absent(self, monkeypatch) -> None:
        from nocturne.triage import NOCTURNE_SKIP_MARKER, post_skip_comment

        calls: list[list[str]] = []

        def record_run_gh(args, **kwargs):
            calls.append(list(args))
            return ""

        monkeypatch.setattr("nocturne.triage.already_commented_skip", lambda r, n: False)
        monkeypatch.setattr("nocturne.triage.run_gh", record_run_gh)

        post_skip_comment("ba1lly/nocturne-playground", 4, "too big / architectural")

        # Exactly one run_gh call, which is `gh issue comment ...`
        assert len(calls) == 1
        args = calls[0]
        assert args[:3] == ["gh", "issue", "comment"]
        assert "4" in args
        assert "--repo" in args
        assert "ba1lly/nocturne-playground" in args
        # Body contains the marker + reason
        body_idx = args.index("--body")
        body = args[body_idx + 1]
        assert body.startswith(NOCTURNE_SKIP_MARKER)
        assert "too big / architectural" in body

    def test_post_skip_comment_non_blocking_on_gh_failure(self, monkeypatch) -> None:
        from nocturne.triage import post_skip_comment

        monkeypatch.setattr("nocturne.triage.already_commented_skip", lambda r, n: False)

        def raises(args, **kwargs):
            raise GhRateLimited("api rate limit exceeded")

        monkeypatch.setattr("nocturne.triage.run_gh", raises)

        # MUST NOT raise — non-blocking by contract
        result = post_skip_comment("ba1lly/nocturne-playground", 4, "too big")
        assert result is None


# ---------------------------------------------------------------------------
# build_triage_graph() — LangGraph compiles
# ---------------------------------------------------------------------------


class TestTriageGraph:
    def test_build_triage_graph_compiles(self) -> None:
        from nocturne.triage import build_triage_graph

        graph = build_triage_graph()
        # The compiled graph is callable / has .invoke; minimally it must not be None
        assert graph is not None
        assert hasattr(graph, "invoke")
