from __future__ import annotations

# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from nocturne.config import ReactionsConfig
from nocturne.models import PRWatch
from nocturne.reactions import poll_and_react
from nocturne.sources.github_pr import PRState
from nocturne.store import Store


def _cfg(**over: Any) -> Any:
    return SimpleNamespace(reactions=ReactionsConfig(enabled=True, **over))


def _state(lifecycle: str = "OPEN", ci: str = "PASSING", review: str = "NONE",
           head: str = "sha1", failing: str = "", feedback: str = "") -> PRState:
    return PRState(lifecycle, ci, review, head, failing, feedback)  # type: ignore[arg-type]


def _watch(store: Store, *, fix_attempts: int = 0, last_signature: str | None = None,
           age_hours: float = 0.0) -> PRWatch:
    now = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    w = PRWatch(
        pr_url="https://github.com/o/r/pull/1", task_id="o/r#1", repo_slug="o/r",
        pr_number=1, branch="nocturne/issue-1-1", base="main",
        fix_attempts=fix_attempts, last_signature=last_signature,
        created_at=now, updated_at=now,
    )
    store.add_pr_watch(w)
    return w


@pytest.fixture
def store() -> Any:
    s = Store(":memory:")
    yield s
    s.close()


def _patch_state(monkeypatch: pytest.MonkeyPatch, state: PRState) -> None:
    monkeypatch.setattr("nocturne.reactions.get_pr_state", lambda repo, num: state)


def _patch_dispatch(monkeypatch: pytest.MonkeyPatch, *, pushed: bool = True, tokens: int = 5000) -> list[Any]:
    calls: list[Any] = []

    def fake(watch: PRWatch, kind: str, feedback: str, cfg: Any, store: Any) -> tuple[bool, int]:
        calls.append({"kind": kind, "feedback": feedback, "pr": watch.pr_url})
        return (pushed, tokens)

    monkeypatch.setattr("nocturne.orchestrator.process_pr_reaction", fake)
    return calls


def _recording_notify() -> tuple[Any, list[tuple[str, str]]]:
    events: list[tuple[str, str]] = []
    def notify(watch: PRWatch, event: str, detail: str) -> None:
        events.append((event, detail))
    return notify, events


def test_disabled_is_noop(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    _watch(store)
    notify, events = _recording_notify()
    summary = poll_and_react(SimpleNamespace(reactions=ReactionsConfig(enabled=False)), store, notify)
    assert summary["checked"] == 0
    assert events == []


def test_merged_closes_watch(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _watch(store)
    _patch_state(monkeypatch, _state(lifecycle="MERGED"))
    notify, events = _recording_notify()

    poll_and_react(_cfg(), store, notify)

    assert store.get_pr_watch(w.pr_url).state == "merged"
    assert events[0][0] == "merged"


def test_closed_unmerged_stops_watch(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _watch(store)
    _patch_state(monkeypatch, _state(lifecycle="CLOSED"))
    notify, events = _recording_notify()

    poll_and_react(_cfg(), store, notify)

    assert store.get_pr_watch(w.pr_url).state == "closed"
    assert events[0][0] == "closed"


def test_failing_ci_dispatches_ci_fix(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _watch(store)
    _patch_state(monkeypatch, _state(ci="FAILING", failing="tests broke"))
    calls = _patch_dispatch(monkeypatch)
    notify, events = _recording_notify()

    summary = poll_and_react(_cfg(), store, notify)

    assert len(calls) == 1 and calls[0]["kind"] == "ci"
    assert calls[0]["feedback"] == "tests broke"
    assert store.get_pr_watch(w.pr_url).fix_attempts == 1
    assert summary["tokens"] == 5000
    assert events[0][0] == "ci_fix_pushed"


def test_changes_requested_dispatches_review_fix(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    _watch(store)
    _patch_state(monkeypatch, _state(review="CHANGES_REQUESTED", feedback="add tests"))
    calls = _patch_dispatch(monkeypatch)
    notify, _events = _recording_notify()

    poll_and_react(_cfg(), store, notify)

    assert len(calls) == 1 and calls[0]["kind"] == "review"
    assert calls[0]["feedback"] == "add tests"


def test_review_takes_priority_over_ci(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    _watch(store)
    _patch_state(monkeypatch, _state(ci="FAILING", review="CHANGES_REQUESTED", feedback="x"))
    calls = _patch_dispatch(monkeypatch)
    notify, _events = _recording_notify()

    poll_and_react(_cfg(), store, notify)

    assert calls[0]["kind"] == "review"  # human signal wins over CI


def test_approved_and_green_notifies_ready_no_dispatch(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _watch(store)
    _patch_state(monkeypatch, _state(ci="PASSING", review="APPROVED"))
    calls = _patch_dispatch(monkeypatch)
    notify, events = _recording_notify()

    poll_and_react(_cfg(), store, notify)

    assert calls == []  # never dispatches, never merges
    assert store.get_pr_watch(w.pr_url).state == "ready"
    assert events[0][0] == "ready"


def test_exhausted_attempts_escalates(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _watch(store, fix_attempts=3)
    _patch_state(monkeypatch, _state(ci="FAILING", failing="still broken"))
    calls = _patch_dispatch(monkeypatch)
    notify, events = _recording_notify()

    poll_and_react(_cfg(max_fix_attempts=3), store, notify)

    assert calls == []  # no further auto-fixing
    assert store.get_pr_watch(w.pr_url).state == "escalated"
    assert events[0][0] == "escalated"


def test_signature_dedup_skips_unchanged_state(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(ci="FAILING", head="samesha", failing="x")
    _watch(store, last_signature=state.signature())
    _patch_state(monkeypatch, state)
    calls = _patch_dispatch(monkeypatch)
    notify, _events = _recording_notify()

    poll_and_react(_cfg(), store, notify)

    assert calls == []  # same state already handled; no repeat dispatch


def test_pending_ci_records_signature_no_action(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _watch(store)
    state = _state(ci="PENDING")
    _patch_state(monkeypatch, state)
    calls = _patch_dispatch(monkeypatch)
    notify, _events = _recording_notify()

    poll_and_react(_cfg(), store, notify)

    assert calls == []
    assert store.get_pr_watch(w.pr_url).last_signature == state.signature()
    assert store.get_pr_watch(w.pr_url).state == "watching"


def test_watch_ttl_expiry_stops_watch(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _watch(store, age_hours=200)  # default ttl is 168h
    notify, events = _recording_notify()
    # get_pr_state must NOT be called once expired; leave it unpatched to prove that.

    poll_and_react(_cfg(), store, notify)

    assert store.get_pr_watch(w.pr_url).state == "closed"
    assert events[0][0] == "watch_expired"
