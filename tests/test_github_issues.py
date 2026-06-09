from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nocturne._gh_retry import (
    GhAuthError,
    GhError,
    GhRateLimited,
    GhSubprocessError,
    IssueNotFound,
    run_gh,
)
from nocturne.config import RepoConfig
from nocturne.models import Task
from nocturne.sources import github_issues
from nocturne.sources.github_issues import (
    comment,
    fetch_eligible,
    fetch_one,
    get_issue_state,
)
from tests.fakes import FakeGhResult, RecordingSubprocess, make_subprocess_result


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch) -> RecordingSubprocess:
    recorder = RecordingSubprocess()
    # _gh_retry imports subprocess and calls subprocess.run
    import nocturne._gh_retry as gh_retry
    monkeypatch.setattr(gh_retry.subprocess, "run", recorder)
    return recorder


@pytest.fixture
def repo_cfg(tmp_worktree: Path) -> RepoConfig:
    return RepoConfig(
        slug="owner/repo",
        checkout_path=str(tmp_worktree),
        label="agent",
        base="main",
        verify_cmd="pytest -q",
        require_new_test=True,
    )


# -----------------------------
# fetch_eligible
# -----------------------------

def _issue_payload() -> list[dict[str, Any]]:
    return [
        {
            "number": 1,
            "title": "First issue",
            "body": "Body 1",
            "labels": [{"name": "agent"}],
            "assignees": [],
        },
        {
            "number": 2,
            "title": "Second issue",
            "body": None,
            "labels": [{"name": "agent"}],
            "assignees": [],
        },
        {
            "number": 3,
            "title": "Assigned issue",
            "body": "skip me",
            "labels": [{"name": "agent"}],
            "assignees": [{"login": "someone"}],
        },
    ]


def test_fetch_eligible_parses_json_to_tasks(monkeypatch, repo_cfg):
    recorder = _patch_subprocess(monkeypatch)
    recorder.queue_result(FakeGhResult.success(stdout=json.dumps(_issue_payload())))

    tasks = fetch_eligible(repo_cfg)

    # 2 eligible (issue 3 is assigned)
    assert len(tasks) == 2
    assert all(isinstance(t, Task) for t in tasks)


def test_fetch_eligible_drops_assigned_issues(monkeypatch, repo_cfg):
    recorder = _patch_subprocess(monkeypatch)
    recorder.queue_result(FakeGhResult.success(stdout=json.dumps(_issue_payload())))

    tasks = fetch_eligible(repo_cfg)

    numbers = [t.issue_number for t in tasks]
    assert 3 not in numbers
    assert numbers == [1, 2]


def test_fetch_eligible_builds_task_id_slug_hash_number(monkeypatch, repo_cfg):
    recorder = _patch_subprocess(monkeypatch)
    recorder.queue_result(FakeGhResult.success(stdout=json.dumps(_issue_payload())))

    tasks = fetch_eligible(repo_cfg)

    assert tasks[0].id == "owner/repo#1"
    assert tasks[1].id == "owner/repo#2"


def test_fetch_eligible_invokes_correct_gh_command(monkeypatch, repo_cfg):
    recorder = _patch_subprocess(monkeypatch)
    recorder.queue_result(FakeGhResult.success(stdout=json.dumps(_issue_payload())))

    fetch_eligible(repo_cfg)

    assert len(recorder.calls) == 1
    args, _ = recorder.calls[0]
    assert args[0] == "gh"
    assert "issue" in args and "list" in args
    assert "--repo" in args and "owner/repo" in args
    assert "--label" in args and "agent" in args
    assert "--state" in args and "open" in args
    assert "--json" in args


def test_fetch_eligible_handles_null_body(monkeypatch, repo_cfg):
    recorder = _patch_subprocess(monkeypatch)
    recorder.queue_result(FakeGhResult.success(stdout=json.dumps(_issue_payload())))

    tasks = fetch_eligible(repo_cfg)

    # Issue 2 had body=None → must become ""
    issue2 = next(t for t in tasks if t.issue_number == 2)
    assert issue2.body == ""


# -----------------------------
# comment
# -----------------------------

def test_comment_invokes_gh_issue_comment(monkeypatch):
    recorder = _patch_subprocess(monkeypatch)
    recorder.queue_result(FakeGhResult.success(stdout=""))

    comment("owner/repo", 42, "Hello world")

    assert len(recorder.calls) == 1
    args, _ = recorder.calls[0]
    assert args[:3] == ["gh", "issue", "comment"]
    assert "42" in args
    assert "--repo" in args and "owner/repo" in args
    assert "--body" in args and "Hello world" in args


# -----------------------------
# get_issue_state
# -----------------------------

def test_get_issue_state_returns_open_stripped(monkeypatch):
    recorder = _patch_subprocess(monkeypatch)
    recorder.queue_result(FakeGhResult.success(stdout="OPEN\n"))

    assert get_issue_state("owner/repo", 7) == "OPEN"


def test_get_issue_state_returns_closed_stripped(monkeypatch):
    recorder = _patch_subprocess(monkeypatch)
    recorder.queue_result(FakeGhResult.success(stdout="CLOSED\n"))

    assert get_issue_state("owner/repo", 7) == "CLOSED"


# -----------------------------
# run_gh retry behavior
# -----------------------------

def test_rate_limit_retries(monkeypatch):
    """Per QA scenario - this exact test name is referenced in the plan."""
    recorder = _patch_subprocess(monkeypatch)
    sleep = RecordingSleep()
    recorder.queue_result(FakeGhResult.rate_limited())
    recorder.queue_result(FakeGhResult.rate_limited())
    recorder.queue_result(FakeGhResult.rate_limited())
    recorder.queue_result(FakeGhResult.success(stdout="ok"))

    # Make max_attempts=4 so the 3 rate-limits + 1 success all fit
    result = run_gh(["gh", "issue", "list"], max_attempts=4, sleep_fn=sleep)

    assert result == "ok"
    assert len(recorder.calls) == 4
    # exponential backoff: 1, 2, 4
    assert sleep.calls == [1.0, 2.0, 4.0]


def test_rate_limit_exhaustion_raises(monkeypatch):
    recorder = _patch_subprocess(monkeypatch)
    sleep = RecordingSleep()
    for _ in range(5):
        recorder.queue_result(FakeGhResult.rate_limited())

    with pytest.raises(GhRateLimited):
        run_gh(["gh", "issue", "list"], max_attempts=3, sleep_fn=sleep)

    # max_attempts=3 → tries 3 times
    assert len(recorder.calls) == 3


def test_auth_no_retry(monkeypatch):
    """Per QA scenario - this exact test name is referenced in the plan."""
    recorder = _patch_subprocess(monkeypatch)
    sleep = RecordingSleep()
    recorder.queue_result(FakeGhResult.auth_failed())

    with pytest.raises(GhAuthError):
        run_gh(["gh", "issue", "list"], max_attempts=3, sleep_fn=sleep)

    # subprocess called exactly once, sleep never called
    assert len(recorder.calls) == 1
    assert sleep.calls == []


def test_not_found_no_retry(monkeypatch):
    recorder = _patch_subprocess(monkeypatch)
    sleep = RecordingSleep()
    recorder.queue_result(FakeGhResult.not_found())

    with pytest.raises(IssueNotFound):
        run_gh(["gh", "issue", "view", "42"], sleep_fn=sleep)

    assert len(recorder.calls) == 1
    assert sleep.calls == []


def test_generic_failure_raises_subprocess_error(monkeypatch):
    recorder = _patch_subprocess(monkeypatch)
    sleep = RecordingSleep()
    recorder.queue_result(
        make_subprocess_result(exit_code=1, stderr="something totally unrelated broke\n")
    )

    with pytest.raises(GhSubprocessError):
        run_gh(["gh", "issue", "list"], sleep_fn=sleep)

    assert len(recorder.calls) == 1
    assert sleep.calls == []


def test_run_gh_rejects_non_gh_args(monkeypatch):
    _patch_subprocess(monkeypatch)
    with pytest.raises((ValueError, AssertionError)):
        run_gh(["git", "status"])


# -----------------------------
# fetch_one
# -----------------------------

def test_fetch_one_rejects_closed_issues(monkeypatch):
    recorder = _patch_subprocess(monkeypatch)
    payload = {
        "number": 5,
        "title": "Closed issue",
        "body": "done",
        "labels": [{"name": "agent"}],
        "assignees": [],
        "state": "CLOSED",
    }
    recorder.queue_result(FakeGhResult.success(stdout=json.dumps(payload)))

    rc = RepoConfig.model_construct(
        slug="owner/repo",
        checkout_path="/tmp/whatever",
        label="agent",
        base="main",
        verify_cmd="pytest -q",
        require_new_test=True,
    )

    with pytest.raises(GhError):
        fetch_one("owner/repo", 5, rc)


def test_fetch_one_returns_task_for_open_issue(monkeypatch, repo_cfg):
    recorder = _patch_subprocess(monkeypatch)
    payload = {
        "number": 9,
        "title": "Open issue",
        "body": "do thing",
        "labels": [{"name": "agent"}],
        "assignees": [],
        "state": "OPEN",
    }
    recorder.queue_result(FakeGhResult.success(stdout=json.dumps(payload)))

    task = fetch_one("owner/repo", 9, repo_cfg)
    assert task.id == "owner/repo#9"
    assert task.title == "Open issue"
    assert task.body == "do thing"
