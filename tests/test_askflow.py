"""Tests for nocturne.askflow — combined Task 25 + 26.

Covers:
  - detect_need_input() sentinel propagation from OpenCodeResult
  - park_task() persists status + question; rejects empty question
  - auto_question_from_failure() embeds attempts + truncated excerpt
  - resume_task() flips status + persists answer; rejects non-parked / missing / empty
  - render_resume_prompt() prepends answer; no injection on empty answer
  - list_parked() returns only parked tasks
  - LITERAL sentinel in non-last event → no false park (Metis Sandbox Issue #5 guard)
  - park → resume roundtrip + prompt injection
  - build_hitl_graph() compiles without invoking
  - post_park_comment_node idempotency: skip when marker present, post when absent,
    non-blocking on gh errors (the Metis "no duplicate comment on resume" invariant
    tested at unit level)
"""

from __future__ import annotations

# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false, reportExplicitAny=false, reportUnannotatedClassAttribute=false, reportArgumentType=false
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

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
from nocturne.models import OpenCodeResult, ParkedTask, Task, VerifyResult
from nocturne.store import Store

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_worktree: Path, tmp_path: Path) -> Config:
    return Config(
        github=GitHubConfig(owner="ba1lly"),
        sandbox=SandboxConfig(repo_name="nocturne-playground", checkout_path=str(tmp_worktree)),
        providers={
            "alibaba-coding-plan": ProviderConfig(
                base_url="https://example.test",
                api_key_env="DASHSCOPE_API_KEY",
            )
        },
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


def _make_task(
    *,
    task_id: str = "issue#42",
    issue_number: int = 42,
    status: str = "selected",
    title: str = "Fix the thing",
    body: str = "Body of the issue.",
    answer: str | None = None,
    question: str | None = None,
    checkout_path: str = "/tmp/test",
) -> Task:
    now = datetime.now(UTC)
    return Task(
        id=task_id,
        repo_slug="ba1lly/nocturne-playground",
        checkout_path=checkout_path,
        issue_number=issue_number,
        title=title,
        body=body,
        base="main",
        verify_cmd="pytest -q",
        require_new_test=False,
        coding_model="",
        branch="",
        status=status,  # type: ignore[arg-type]
        attempts=0,
        created_at=now,
        updated_at=now,
        question=question,
        answer=answer,
    )


# ---------------------------------------------------------------------------
# Group A: Task 25 primitives — detect_need_input
# ---------------------------------------------------------------------------


class TestDetectNeedInput:
    def test_returns_question_when_sentinel_seen(self) -> None:
        from nocturne.askflow import detect_need_input

        result = OpenCodeResult(
            exit_code=0,
            events=[{"type": "text", "text": "##NOCTURNE_NEED_INPUT##\nWhy?"}],
            sentinel_seen=True,
            need_input_question="Why?",
            pid=123,
            error_events=[],
        )
        assert detect_need_input(result) == "Why?"

    def test_none_when_not_seen(self) -> None:
        from nocturne.askflow import detect_need_input

        result = OpenCodeResult(
            exit_code=0,
            events=[{"type": "text", "text": "ok"}],
            sentinel_seen=False,
            need_input_question=None,
            pid=123,
            error_events=[],
        )
        assert detect_need_input(result) is None

    def test_none_when_seen_but_no_question(self) -> None:
        """Defensive guard: pydantic allows sentinel_seen=True with None question."""
        from nocturne.askflow import detect_need_input

        result = OpenCodeResult(
            exit_code=0,
            events=[],
            sentinel_seen=True,
            need_input_question=None,
            pid=123,
            error_events=[],
        )
        assert detect_need_input(result) is None


# ---------------------------------------------------------------------------
# Group A: park_task
# ---------------------------------------------------------------------------


class TestParkTask:
    def test_persists_and_returns_parked_task(self, inmem_store: Store) -> None:
        from nocturne.askflow import park_task

        task = _make_task(task_id="issue#park-1")
        inmem_store.insert_task(task)

        parked = park_task(task, "What output format?", inmem_store)

        assert isinstance(parked, ParkedTask)
        assert parked.question == "What output format?"
        assert parked.status == "parked"

        stored = inmem_store.get_task(task.id)
        assert stored is not None
        assert stored.status == "parked"
        assert stored.question == "What output format?"

    def test_rejects_empty_question(self, inmem_store: Store) -> None:
        from nocturne.askflow import AskflowError, park_task

        task = _make_task(task_id="issue#park-empty")
        inmem_store.insert_task(task)

        with pytest.raises(AskflowError, match="non-empty question"):
            park_task(task, "", inmem_store)

        with pytest.raises(AskflowError, match="non-empty question"):
            park_task(task, "   \n  ", inmem_store)


# ---------------------------------------------------------------------------
# Group A: auto_question_from_failure
# ---------------------------------------------------------------------------


class TestAutoQuestionFromFailure:
    def test_includes_attempts_and_excerpt(self) -> None:
        from nocturne.askflow import auto_question_from_failure

        vr = VerifyResult(
            passed=False,
            exit_code=1,
            stdout="",
            stderr="boom\nblah",
            new_test_added=False,
            reason="fail",
        )
        q = auto_question_from_failure(vr, attempts=3)
        assert "3 retries" in q
        assert "boom" in q
        assert "How should I proceed?" in q
        assert "fail" in q

    def test_truncates_long_error(self) -> None:
        from nocturne.askflow import auto_question_from_failure

        vr = VerifyResult(
            passed=False,
            exit_code=1,
            stdout="",
            stderr="x" * 2000,
            new_test_added=False,
            reason="big fail",
        )
        q = auto_question_from_failure(vr, attempts=5)
        # Must not include all 2000 'x' chars — excerpt capped at 800.
        # +1 tolerance for the 'x' in the literal word "excerpt:" in the
        # template (one occurrence). Anything beyond that signals leakage.
        assert q.count("x") <= 801
        assert "x" * 800 in q
        assert "x" * 900 not in q

    def test_falls_back_to_stdout_when_stderr_empty(self) -> None:
        from nocturne.askflow import auto_question_from_failure

        vr = VerifyResult(
            passed=False,
            exit_code=1,
            stdout="stdout-msg",
            stderr="",
            new_test_added=False,
            reason="r",
        )
        q = auto_question_from_failure(vr, attempts=1)
        assert "stdout-msg" in q


# ---------------------------------------------------------------------------
# Group A: resume_task
# ---------------------------------------------------------------------------


class TestResumeTask:
    def test_changes_status_and_persists_answer(self, inmem_store: Store) -> None:
        from nocturne.askflow import park_task, resume_task

        task = _make_task(task_id="issue#resume-1")
        inmem_store.insert_task(task)
        park_task(task, "Which framework?", inmem_store)

        refreshed = resume_task(task.id, "use pytest", inmem_store)

        assert refreshed.status == "selected"
        assert refreshed.answer == "use pytest"

        # store consistency
        stored = inmem_store.get_task(task.id)
        assert stored is not None
        assert stored.status == "selected"
        assert stored.answer == "use pytest"

    def test_rejects_non_parked(self, inmem_store: Store) -> None:
        from nocturne.askflow import AskflowError, resume_task

        task = _make_task(task_id="issue#resume-notparked", status="selected")
        inmem_store.insert_task(task)

        with pytest.raises(AskflowError, match="not parked"):
            resume_task(task.id, "ans", inmem_store)

    def test_rejects_missing_task(self, inmem_store: Store) -> None:
        from nocturne.askflow import AskflowError, resume_task

        with pytest.raises(AskflowError, match="not found"):
            resume_task("does-not-exist", "ans", inmem_store)

    def test_rejects_empty_answer(self, inmem_store: Store) -> None:
        from nocturne.askflow import AskflowError, park_task, resume_task

        task = _make_task(task_id="issue#resume-empty")
        inmem_store.insert_task(task)
        park_task(task, "q?", inmem_store)

        with pytest.raises(AskflowError, match="non-empty answer"):
            resume_task(task.id, "", inmem_store)
        with pytest.raises(AskflowError, match="non-empty answer"):
            resume_task(task.id, "   \n", inmem_store)


# ---------------------------------------------------------------------------
# Group A: render_resume_prompt
# ---------------------------------------------------------------------------


class TestRenderResumePrompt:
    def test_resume_prompt_includes_answer(self, cfg: Config) -> None:
        from nocturne.askflow import render_resume_prompt

        task = _make_task(task_id="issue#prompt-1", answer="my answer")
        rendered = render_resume_prompt(task, cfg)

        assert "Human responded to your earlier question: my answer" in rendered
        assert "Now continue:" in rendered
        # Base prompt content should still be present (verify_cmd is in the template)
        assert "pytest -q" in rendered

    def test_no_injection_if_empty_answer(self, cfg: Config) -> None:
        from nocturne.askflow import render_resume_prompt

        task = _make_task(task_id="issue#prompt-empty", answer=None)
        rendered = render_resume_prompt(task, cfg)

        assert "Human responded" not in rendered

        task2 = _make_task(task_id="issue#prompt-ws", answer="   \n  ")
        rendered2 = render_resume_prompt(task2, cfg)
        assert "Human responded" not in rendered2


# ---------------------------------------------------------------------------
# Group A: list_parked
# ---------------------------------------------------------------------------


class TestListParked:
    def test_returns_only_parked_tasks(self, inmem_store: Store) -> None:
        from nocturne.askflow import list_parked, park_task

        parked_task = _make_task(task_id="issue#a", issue_number=1)
        selected_task = _make_task(task_id="issue#b", issue_number=2)
        done_task = _make_task(task_id="issue#c", issue_number=3)

        inmem_store.insert_task(parked_task)
        inmem_store.insert_task(selected_task)
        inmem_store.insert_task(done_task)

        park_task(parked_task, "why?", inmem_store)
        inmem_store.update_status(done_task.id, "done")

        result = list_parked(inmem_store)

        assert len(result) == 1
        assert result[0].id == "issue#a"
        assert result[0].question == "why?"
        assert isinstance(result[0], ParkedTask)


# ---------------------------------------------------------------------------
# Group B: literal-sentinel false-positive guard (Metis Sandbox Issue #5)
# ---------------------------------------------------------------------------


class TestLiteralSentinelGuard:
    def test_literal_sentinel_no_false_park(self) -> None:
        """Sentinel appears in a NON-LAST event (e.g. echoed issue body); the
        last text event does NOT contain it → no park.

        We pass the raw events through opencode_driver.detect_sentinel to mirror
        production semantics, then assert detect_need_input() returns None.
        """
        from nocturne.askflow import detect_need_input
        from nocturne.opencode_driver import detect_sentinel

        events: list[dict[str, Any]] = [
            # Middle event echoes the literal sentinel (e.g. from issue body)
            {
                "type": "text",
                "text": "User asked about ##NOCTURNE_NEED_INPUT##\nin the issue body.",
            },
            # Final event is clean — agent completed normally
            {"type": "text", "text": "Done. PR opened."},
        ]
        question = detect_sentinel(events)
        assert question is None  # production detector ignores non-last events

        result = OpenCodeResult(
            exit_code=0,
            events=events,
            sentinel_seen=question is not None,
            need_input_question=question,
            pid=1,
            error_events=[],
        )
        assert detect_need_input(result) is None


# ---------------------------------------------------------------------------
# Group C: park + resume roundtrip
# ---------------------------------------------------------------------------


class TestParkResumeRoundtrip:
    def test_park_then_resume_roundtrip(
        self, inmem_store: Store, cfg: Config
    ) -> None:
        from nocturne.askflow import (
            park_task,
            render_resume_prompt,
            resume_task,
        )

        task = _make_task(task_id="issue#roundtrip")
        inmem_store.insert_task(task)

        # Park
        parked = park_task(task, "Which algorithm?", inmem_store)
        assert parked.status == "parked"
        stored = inmem_store.get_task(task.id)
        assert stored is not None and stored.status == "parked"

        # Resume
        refreshed = resume_task(task.id, "use binary search", inmem_store)
        assert refreshed.status == "selected"
        assert refreshed.answer == "use binary search"

        # Render prompt — answer must be injected
        rendered = render_resume_prompt(refreshed, cfg)
        assert "use binary search" in rendered
        assert "Human responded to your earlier question" in rendered


# ---------------------------------------------------------------------------
# Group D: Task 26 LangGraph graph
# ---------------------------------------------------------------------------


class TestHITLGraph:
    def test_build_hitl_graph_compiles(
        self, cfg: Config, inmem_store: Store
    ) -> None:
        """Smoke-test: the graph constructs and compiles without exception.

        We do NOT invoke the graph here — exercising interrupt + checkpoint
        is brittle in-unit; the idempotency invariant is locked separately
        via the post_park_comment_node tests below.
        """
        try:
            from nocturne.askflow import build_hitl_graph
        except ImportError:
            pytest.skip("langgraph not installed")

        compiled = build_hitl_graph(cfg, inmem_store)
        assert compiled is not None
        # Has the expected entry point + nodes wired
        assert hasattr(compiled, "invoke")


# ---------------------------------------------------------------------------
# Group E: post_park_comment_node idempotency (the Metis "no duplicate
# comment on resume" invariant, tested at unit level — equivalent to the
# graph-level resume scenario but far cheaper to drive)
# ---------------------------------------------------------------------------


class _RunGhRecorder:
    """Lightweight stand-in for run_gh that records calls and returns a
    queued sequence of stdout strings (or raises if a queued entry is an
    Exception instance)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.queued: list[Any] = []

    def queue(self, value: Any) -> None:
        self.queued.append(value)

    def __call__(self, args: list[str], **_kwargs: Any) -> str:
        self.calls.append(list(args))
        if not self.queued:
            return ""
        value = self.queued.pop(0)
        if isinstance(value, Exception):
            raise value
        return str(value)

    def comment_calls(self) -> list[list[str]]:
        return [c for c in self.calls if len(c) >= 2 and c[1] == "issue" and c[2] == "comment"]

    def view_calls(self) -> list[list[str]]:
        return [c for c in self.calls if len(c) >= 2 and c[1] == "issue" and c[2] == "view"]


class TestPostParkCommentIdempotency:
    def _state(self, question: str = "Which framework?") -> dict[str, Any]:
        task = _make_task(task_id="issue#postpark", issue_number=42)
        return {"task": task, "question": question}

    def test_post_park_comment_skips_if_marker_present(
        self, inmem_store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nocturne.askflow import QUESTION_MARKER, post_park_comment_node

        recorder = _RunGhRecorder()
        recorder.queue(f"{QUESTION_MARKER}\n[Nocturne] Which framework?")

        import nocturne._gh_retry as _gh
        monkeypatch.setattr(_gh, "run_gh", recorder)

        state = self._state()
        result = post_park_comment_node(state, inmem_store)

        assert result == {}
        assert len(recorder.view_calls()) == 1
        assert len(recorder.comment_calls()) == 0

    def test_post_park_comment_posts_when_no_marker(
        self, inmem_store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nocturne.askflow import QUESTION_MARKER, post_park_comment_node

        recorder = _RunGhRecorder()
        # view returns empty → no existing marker → post
        recorder.queue("")
        recorder.queue("")  # comment call's stdout

        import nocturne._gh_retry as _gh
        monkeypatch.setattr(_gh, "run_gh", recorder)

        state = self._state(question="What output format?")
        result = post_park_comment_node(state, inmem_store)

        assert result == {}
        assert len(recorder.view_calls()) == 1
        assert len(recorder.comment_calls()) == 1

        # Verify body content
        comment_call = recorder.comment_calls()[0]
        body_idx = comment_call.index("--body") + 1
        body = comment_call[body_idx]
        assert body.startswith(QUESTION_MARKER)
        assert "What output format?" in body

    def test_post_park_comment_non_blocking_on_gh_error(
        self, inmem_store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nocturne._gh_retry import GhSubprocessError
        from nocturne.askflow import post_park_comment_node

        recorder = _RunGhRecorder()
        # view raises (gh broken) — fall through and try to post anyway
        recorder.queue(GhSubprocessError("gh broken"))
        # post also raises — must NOT propagate
        recorder.queue(GhSubprocessError("post failed"))

        import nocturne._gh_retry as _gh
        monkeypatch.setattr(_gh, "run_gh", recorder)

        state = self._state()
        # Must not raise
        result = post_park_comment_node(state, inmem_store)
        assert result == {}

    def test_post_park_comment_called_twice_idempotent(
        self, inmem_store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Locks the resume invariant end-to-end at unit level:

        Simulate two consecutive calls (as would happen if LangGraph's
        post-interrupt re-run mechanism were ever to fire the node twice).
        First call sees empty view → posts. Second call sees the marker
        (we wire the recorder so the view-call returns the just-posted body)
        → does NOT post again.

        Net `gh issue comment` calls: exactly 1.
        """
        from nocturne.askflow import QUESTION_MARKER, post_park_comment_node

        # Stateful recorder: tracks whether a comment has been posted, and
        # makes subsequent view-calls return the marker accordingly.
        class _StatefulGh:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []
                self.posted_body: str | None = None

            def __call__(self, args: list[str], **_kwargs: Any) -> str:
                self.calls.append(list(args))
                if len(args) >= 3 and args[1] == "issue" and args[2] == "view":
                    if self.posted_body is not None:
                        return self.posted_body  # marker present
                    return ""
                if len(args) >= 3 and args[1] == "issue" and args[2] == "comment":
                    body_idx = args.index("--body") + 1
                    self.posted_body = args[body_idx]
                    return ""
                return ""

        gh = _StatefulGh()
        import nocturne._gh_retry as _gh_module
        monkeypatch.setattr(_gh_module, "run_gh", gh)

        state = self._state(question="resume idempotency check")
        post_park_comment_node(state, inmem_store)
        post_park_comment_node(state, inmem_store)

        # Two view calls (one per invocation), but only ONE comment call
        view_calls = [c for c in gh.calls if c[1:3] == ["issue", "view"]]
        comment_calls = [c for c in gh.calls if c[1:3] == ["issue", "comment"]]
        assert len(view_calls) == 2
        assert len(comment_calls) == 1
        assert gh.posted_body is not None
        assert gh.posted_body.startswith(QUESTION_MARKER)
        assert "resume idempotency check" in gh.posted_body


# ---------------------------------------------------------------------------
# Group F: resume_with_answer (M3 lightweight variant)
# ---------------------------------------------------------------------------


class TestResumeWithAnswer:
    def test_resume_with_answer_flips_status(
        self, inmem_store: Store, cfg: Config
    ) -> None:
        from nocturne.askflow import park_task, resume_with_answer

        task = _make_task(task_id="issue#rwa")
        inmem_store.insert_task(task)
        park_task(task, "which?", inmem_store)

        refreshed = resume_with_answer(task.id, "this one", cfg, inmem_store)

        assert refreshed.status == "selected"
        assert refreshed.answer == "this one"
