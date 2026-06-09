from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest
from pydantic import ValidationError

from nocturne.models import (
    OpenCodeResult,
    ParkedTask,
    RunReport,
    Task,
    TaskStatus,
    TriageOutcome,
    TriageResult,
    VerifyResult,
)


def _task_kwargs() -> dict[str, Any]:
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    return {
        "id": "r#1",
        "repo_slug": "owner/repo",
        "checkout_path": "/tmp/repo-checkout",
        "issue_number": 1,
        "title": "T",
        "body": "B",
        "base": "main",
        "verify_cmd": "pytest -q",
        "require_new_test": True,
        "coding_model": "alibaba-coding-plan/qwen3-coder-plus",
        "branch": "nocturne/issue-1-1",
        "status": "selected",
        "attempts": 0,
        "pr_url": None,
        "question": None,
        "answer": None,
        "created_at": now,
        "updated_at": now,
        "opencode_pid": None,
    }


def test_task_round_trip_model_dump_and_validate() -> None:
    task = Task(**cast(Any, _task_kwargs()))

    round_tripped = Task.model_validate(task.model_dump())

    assert round_tripped == task


@pytest.mark.parametrize(
    ("repo_slug", "valid"),
    [
        ("owner/repo", True),
        ("Owner_1/repo-2", True),
        ("owner", False),
        ("/repo", False),
        ("owner/repo/extra", False),
    ],
)
def test_task_repo_slug_validation(repo_slug: str, valid: bool) -> None:
    data = _task_kwargs()
    data["repo_slug"] = repo_slug

    if valid:
        assert Task(**cast(Any, data)).repo_slug == repo_slug
    else:
        with pytest.raises(ValidationError):
            Task(**cast(Any, data))


def test_task_status_is_literal_enum_surface() -> None:
    task = Task(**cast(Any, _task_kwargs()))

    assert task.status == "selected"
    assert TaskStatus.__args__ == (
        "selected",
        "running",
        "done",
        "parked",
        "skipped",
        "failed",
        "aborted",
    )


def test_parked_task_requires_question() -> None:
    data = _task_kwargs()
    data.update({"question": "Need input", "parked_at": datetime.now(timezone.utc), "status": "parked"})

    parked = ParkedTask(**cast(Any, data))
    assert parked.question == "Need input"

    data["question"] = ""
    with pytest.raises(ValidationError):
        ParkedTask(**cast(Any, data))


@pytest.mark.parametrize("outcome", ["DOABLE", "SKIP", "NEED_INPUT"])
def test_triage_outcome_accepts_only_allowed_values(outcome: str) -> None:
    triage = TriageResult(
        task_id="x",
        doable=True,
        outcome=TriageOutcome(outcome),
        priority=50,
        reason="r",
    )

    assert triage.outcome == outcome


@pytest.mark.parametrize("outcome", ["PARTIAL", "SPLIT", "ESCALATE", "DEFER"])
def test_triage_outcome_rejects_scope_creep_values(outcome: str) -> None:
    with pytest.raises(ValidationError):
        TriageOutcome(outcome)


def test_verify_result_captures_all_fields() -> None:
    result = VerifyResult(
        passed=False,
        exit_code=1,
        stdout="out",
        stderr="err",
        new_test_added=True,
        reason=None,
    )

    assert result.exit_code == 1
    assert result.stderr == "err"


def test_opencode_result_includes_pid_and_events() -> None:
    result = OpenCodeResult(
        exit_code=0,
        events=[{"type": "message"}],
        sentinel_seen=True,
        need_input_question=None,
        pid=1234,
        error_events=[],
    )

    assert result.pid == 1234
    assert result.events == [{"type": "message"}]


def test_opencode_result_pid_is_optional() -> None:
    result = OpenCodeResult(
        exit_code=0,
        events=[],
        sentinel_seen=False,
        need_input_question=None,
        error_events=[],
    )

    assert result.pid is None


def test_run_report_groups_all_results() -> None:
    task = Task(**cast(Any, _task_kwargs()))
    parked = ParkedTask(
        **cast(
            Any,
            {**_task_kwargs(), "question": "Need input", "parked_at": datetime.now(timezone.utc), "status": "parked"},
        )
    )

    report = RunReport(
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        done=[task],
        parked=[parked],
        skipped=[(1, "skip")],
        errors=["boom"],
        summary="ok",
        token_usage=42,
    )

    assert report.done[0] == task
    assert report.parked[0] == parked
    assert report.skipped == [(1, "skip")]
