from __future__ import annotations

import json
from typing import Any

import pytest

from nocturne.sources import github_pr
from nocturne.sources.github_pr import get_pr_state, parse_pr_number


def _patch_api(monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> None:
    """Patch run_gh so each `gh api <path>` returns responses[<matching key>].

    Keys are matched as a SUFFIX of the path so `pulls/9` doesn't also swallow
    `pulls/9/reviews`. Unmatched paths return {}.
    """
    def fake_run_gh(args: list[str]) -> str:
        path = args[-1]
        for key, value in responses.items():
            if path.endswith(key):
                return json.dumps(value)
        return "{}"

    monkeypatch.setattr(github_pr, "run_gh", fake_run_gh)


def test_parse_pr_number() -> None:
    assert parse_pr_number("https://github.com/o/r/pull/42") == 42
    assert parse_pr_number("https://github.com/o/r/pull/7/") == 7


def test_merged_pr_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, {"pulls/5": {"merged": True, "state": "closed", "head": {"sha": "abc"}}})
    state = get_pr_state("o/r", 5)
    assert state.lifecycle == "MERGED"
    assert state.ci == "NONE" and state.review == "NONE"


def test_closed_unmerged_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, {"pulls/5": {"merged": False, "state": "closed", "head": {"sha": "abc"}}})
    assert get_pr_state("o/r", 5).lifecycle == "CLOSED"


def test_open_pr_failing_ci_from_check_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, {
        "pulls/9": {"merged": False, "state": "open", "head": {"sha": "deadbeef"}},
        "check-runs": {"check_runs": [
            {"name": "tests", "status": "completed", "conclusion": "failure",
             "output": {"summary": "3 tests failed in test_math.py"}},
            {"name": "lint", "status": "completed", "conclusion": "success"},
        ]},
        "status": {"statuses": []},
        "reviews": [],
    })
    state = get_pr_state("o/r", 9)
    assert state.lifecycle == "OPEN"
    assert state.ci == "FAILING"
    assert "tests" in state.failing_summary
    assert "3 tests failed" in state.failing_summary


def test_open_pr_pending_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, {
        "pulls/9": {"merged": False, "state": "open", "head": {"sha": "s"}},
        "check-runs": {"check_runs": [{"name": "tests", "status": "in_progress", "conclusion": None}]},
        "status": {"statuses": []},
        "reviews": [],
    })
    assert get_pr_state("o/r", 9).ci == "PENDING"


def test_open_pr_passing_ci_and_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, {
        "pulls/9": {"merged": False, "state": "open", "head": {"sha": "s"}},
        "check-runs": {"check_runs": [{"name": "tests", "status": "completed", "conclusion": "success"}]},
        "status": {"statuses": []},
        "reviews": [{"user": {"login": "alice"}, "state": "APPROVED", "body": ""}],
    })
    state = get_pr_state("o/r", 9)
    assert state.ci == "PASSING"
    assert state.review == "APPROVED"


def test_failing_check_run_pulls_actions_job_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Actions check-runs only carry the job name in output.summary, so the
    real error must be pulled from the job log via gh run view --log-failed."""
    log = (
        "test\truff\t2026-06-11T16:14:32Z \x1b[36;1mruff check .\x1b[0m\n"
        "test\truff\t2026-06-11T16:14:33Z F401 [*] `os` imported but unused\n"
        "test\truff\t2026-06-11T16:14:33Z  --> src/playground/math.py:1:8\n"
        "test\truff\t2026-06-11T16:14:33Z ##[error]Process completed with exit code 1.\n"
    )

    def fake_run_gh(args: list[str]) -> str:
        path_or_cmd = args[-1]
        if args[:3] == ["gh", "run", "view"]:
            return log
        if path_or_cmd.endswith("pulls/9"):
            return json.dumps({"merged": False, "state": "open", "head": {"sha": "s"}})
        if path_or_cmd.endswith("check-runs"):
            return json.dumps({"check_runs": [{
                "name": "test", "status": "completed", "conclusion": "failure",
                "output": {"summary": "test"},  # generic Actions summary
                "details_url": "https://github.com/o/r/actions/runs/12345/job/678",
            }]})
        return "{}" if path_or_cmd.endswith(("status",)) else "[]"

    monkeypatch.setattr(github_pr, "run_gh", fake_run_gh)
    state = get_pr_state("o/r", 9)

    assert state.ci == "FAILING"
    assert "F401" in state.failing_summary
    assert "src/playground/math.py:1:8" in state.failing_summary
    assert "\x1b[" not in state.failing_summary  # ANSI stripped


def test_failing_check_run_falls_back_to_summary_without_log(monkeypatch: pytest.MonkeyPatch) -> None:
    from nocturne._gh_retry import GhError

    def fake_run_gh(args: list[str]) -> str:
        p = args[-1]
        if args[:3] == ["gh", "run", "view"]:
            raise GhError("no log")
        if p.endswith("pulls/9"):
            return json.dumps({"merged": False, "state": "open", "head": {"sha": "s"}})
        if p.endswith("check-runs"):
            return json.dumps({"check_runs": [{
                "name": "build", "status": "completed", "conclusion": "failure",
                "output": {"summary": "the build broke"},
                # no details_url -> cannot fetch a log
            }]})
        return "{}" if p.endswith("status") else "[]"

    monkeypatch.setattr(github_pr, "run_gh", fake_run_gh)
    state = get_pr_state("o/r", 9)
    assert state.ci == "FAILING"
    assert "the build broke" in state.failing_summary


def test_failing_commit_status_counts_as_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, {
        "pulls/9": {"merged": False, "state": "open", "head": {"sha": "s"}},
        "check-runs": {"check_runs": []},
        "status": {"statuses": [{"state": "failure", "context": "ci/external", "description": "build broke"}]},
        "reviews": [],
    })
    state = get_pr_state("o/r", 9)
    assert state.ci == "FAILING"
    assert "ci/external" in state.failing_summary


def test_changes_requested_collects_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, {
        "pulls/9": {"merged": False, "state": "open", "head": {"sha": "s"}},
        "check-runs": {"check_runs": [{"name": "t", "status": "completed", "conclusion": "success"}]},
        "status": {"statuses": []},
        "reviews": [{"user": {"login": "bob"}, "state": "CHANGES_REQUESTED", "body": "Please add error handling"}],
        "comments": [{"path": "src/a.py", "body": "this can be None"}],
    })
    state = get_pr_state("o/r", 9)
    assert state.review == "CHANGES_REQUESTED"
    assert "add error handling" in state.review_feedback
    assert "src/a.py" in state.review_feedback


def test_dismissed_review_clears_standing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, {
        "pulls/9": {"merged": False, "state": "open", "head": {"sha": "s"}},
        "check-runs": {"check_runs": [{"name": "t", "status": "completed", "conclusion": "success"}]},
        "status": {"statuses": []},
        "reviews": [
            {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED", "body": "x"},
            {"user": {"login": "bob"}, "state": "DISMISSED", "body": ""},
        ],
    })
    # bob's changes-requested was dismissed -> no longer blocking
    assert get_pr_state("o/r", 9).review == "REVIEW_REQUIRED"


def test_signature_changes_with_head_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    base = {
        "check-runs": {"check_runs": [{"name": "t", "status": "completed", "conclusion": "failure", "output": {}}]},
        "status": {"statuses": []},
        "reviews": [],
    }
    _patch_api(monkeypatch, {"pulls/9": {"merged": False, "state": "open", "head": {"sha": "aaa"}}, **base})
    sig1 = get_pr_state("o/r", 9).signature()
    _patch_api(monkeypatch, {"pulls/9": {"merged": False, "state": "open", "head": {"sha": "bbb"}}, **base})
    sig2 = get_pr_state("o/r", 9).signature()
    assert sig1 != sig2
