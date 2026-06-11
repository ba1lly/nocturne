"""Post-PR feedback loop: react to CI failures and review comments on PRs
Nocturne opened, shepherding them toward merge-ready.

It NEVER merges. ``approved-and-green`` only notifies a human (the merge call is
theirs), and ``guardrails.enforce_no_auto_merge`` blocks any merge attempt.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from nocturne._gh_retry import GhError
from nocturne._logging import get_logger
from nocturne.config import Config
from nocturne.models import PRWatch
from nocturne.sources.github_pr import PRState, get_pr_state
from nocturne.store import Store

log = get_logger("nocturne.reactions")

# notify(watch, event, detail) -> None. event is a short machine-ish tag
# (e.g. "ci_fix_pushed", "ready", "escalated", "merged").
NotifyFn = Callable[[PRWatch, str, str], None]


def _hours_since(iso_ts: datetime) -> float:
    now = datetime.now(timezone.utc)
    ts = iso_ts if iso_ts.tzinfo else iso_ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def _dispatch_fix(
    watch: PRWatch, kind: str, feedback: str, cfg: Config, store: Store, notify: NotifyFn
) -> int:
    """Run one fix attempt; update the watch; notify. Returns tokens used."""
    from nocturne.orchestrator import process_pr_reaction

    pushed, tokens = process_pr_reaction(watch, kind, feedback, cfg, store)
    store.update_pr_watch(
        watch.pr_url, fix_attempts=watch.fix_attempts + 1, last_signature=None,
    )
    if pushed:
        notify(watch, f"{kind}_fix_pushed", f"Pushed a fix addressing {kind} feedback.")
    else:
        notify(watch, f"{kind}_fix_failed", f"Could not auto-resolve {kind} feedback.")
    return tokens


def _react_one(watch: PRWatch, cfg: Config, store: Store, notify: NotifyFn) -> int:
    """Evaluate one watched PR and take at most one action. Returns tokens used."""
    rc = cfg.reactions

    if _hours_since(watch.created_at) > rc.watch_ttl_hours:
        store.update_pr_watch(watch.pr_url, state="closed")
        notify(watch, "watch_expired", "Stopped watching (exceeded watch TTL).")
        return 0

    state: PRState = get_pr_state(watch.repo_slug, watch.pr_number)

    if state.lifecycle == "MERGED":
        store.update_pr_watch(watch.pr_url, state="merged")
        notify(watch, "merged", "PR merged.")
        return 0
    if state.lifecycle == "CLOSED":
        store.update_pr_watch(watch.pr_url, state="closed")
        notify(watch, "closed", "PR closed without merging.")
        return 0

    signature = state.signature()
    if signature == watch.last_signature:
        return 0  # already handled this exact state; wait for it to change

    exhausted = watch.fix_attempts >= rc.max_fix_attempts

    # Reviewer feedback takes priority over CI (it's a direct human signal).
    if state.review == "CHANGES_REQUESTED" and rc.address_review_comments:
        if exhausted:
            store.update_pr_watch(watch.pr_url, state="escalated", last_signature=signature)
            notify(watch, "escalated", "Changes requested but fix attempts are exhausted.")
            return 0
        return _dispatch_fix(watch, "review", state.review_feedback, cfg, store, notify)

    if state.ci == "FAILING" and rc.fix_failing_ci:
        if exhausted:
            store.update_pr_watch(watch.pr_url, state="escalated", last_signature=signature)
            notify(watch, "escalated", "CI failing but fix attempts are exhausted.")
            return 0
        return _dispatch_fix(watch, "ci", state.failing_summary, cfg, store, notify)

    if state.ci == "PASSING" and state.review == "APPROVED" and rc.notify_when_ready:
        store.update_pr_watch(watch.pr_url, state="ready", last_signature=signature)
        notify(watch, "ready", "Approved and green - ready for you to merge.")
        return 0

    # Pending / nothing actionable: remember the state so we don't re-evaluate
    # until it actually changes (a new head sha or CI/review transition).
    store.update_pr_watch(watch.pr_url, last_signature=signature)
    return 0


def poll_and_react(cfg: Config, store: Store, notify: NotifyFn) -> dict[str, Any]:
    """Poll every active PR watch and take at most one action each.

    Returns a summary including total tokens consumed by any fix dispatches so
    the daemon can fold them into its budget accounting.
    """
    summary: dict[str, Any] = {"checked": 0, "actions": 0, "tokens": 0, "errors": []}
    if not cfg.reactions.enabled:
        return summary

    for watch in store.list_active_pr_watches():
        summary["checked"] += 1
        try:
            tokens = _react_one(watch, cfg, store, notify)
        except GhError as e:
            log.warning("pr-reaction: gh error on %s (skipping): %s", watch.pr_url, e)
            summary["errors"].append(f"{watch.pr_url}:{e}")
            continue
        except Exception as e:  # never let one PR kill the whole poll
            log.error("pr-reaction: unexpected error on %s: %s", watch.pr_url, e)
            summary["errors"].append(f"{watch.pr_url}:{e}")
            continue
        if tokens:
            summary["actions"] += 1
            summary["tokens"] += tokens
    return summary
