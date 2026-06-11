"""Live PR status for the post-PR feedback loop.

Reads a PR's open/merged/closed state, CI conclusion, and review decision via
``gh api`` (REST). REST is deliberate: this repo's GitHub instance rejects the
Projects-classic GraphQL path that ``gh pr view`` walks, so we never touch it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from nocturne._gh_retry import run_gh

PRLifecycle = Literal["OPEN", "MERGED", "CLOSED"]
CIState = Literal["PASSING", "FAILING", "PENDING", "NONE"]
ReviewState = Literal["APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", "NONE"]

_FAILING_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
_PENDING_STATUSES = {"queued", "in_progress", "pending", "waiting", "requested"}
_MAX_FEEDBACK_CHARS = 12000


@dataclass(frozen=True)
class PRState:
    lifecycle: PRLifecycle
    ci: CIState
    review: ReviewState
    head_sha: str
    failing_summary: str
    review_feedback: str

    def signature(self) -> str:
        """Stable key for "this exact PR state" so a reaction fires once per
        distinct state (new push -> new head sha -> new signature)."""
        return f"{self.head_sha}|{self.ci}|{self.review}"


def _api(path: str) -> Any:
    out = run_gh(["gh", "api", path])
    return json.loads(out) if out.strip() else None


def _lifecycle(pull: dict[str, Any]) -> PRLifecycle:
    if pull.get("merged") or pull.get("merged_at"):
        return "MERGED"
    if pull.get("state") == "closed":
        return "CLOSED"
    return "OPEN"


def _ci_state(repo_slug: str, sha: str) -> tuple[CIState, str]:
    """Combine check-runs and legacy commit statuses into one CI verdict."""
    failing: list[str] = []
    any_pending = False
    any_success = False

    check_runs = _api(f"repos/{repo_slug}/commits/{sha}/check-runs") or {}
    for run in check_runs.get("check_runs", []) if isinstance(check_runs, dict) else []:
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            any_pending = True
            continue
        if conclusion in _FAILING_CONCLUSIONS:
            name = run.get("name", "check")
            output = run.get("output") or {}
            detail = output.get("summary") or output.get("title") or ""
            failing.append(f"### CI check failed: {name}\n{detail}".rstrip())
        elif conclusion in ("success", "neutral", "skipped"):
            any_success = True

    combined = _api(f"repos/{repo_slug}/commits/{sha}/status") or {}
    for st in combined.get("statuses", []) if isinstance(combined, dict) else []:
        state = st.get("state")
        if state in ("failure", "error"):
            ctx = st.get("context", "status")
            desc = st.get("description") or ""
            failing.append(f"### CI status failed: {ctx}\n{desc}".rstrip())
        elif state == "pending":
            any_pending = True
        elif state == "success":
            any_success = True

    if failing:
        return "FAILING", "\n\n".join(failing)[:_MAX_FEEDBACK_CHARS]
    if any_pending:
        return "PENDING", ""
    if any_success:
        return "PASSING", ""
    return "NONE", ""


def _review_state(repo_slug: str, pr_number: int) -> tuple[ReviewState, str]:
    reviews = _api(f"repos/{repo_slug}/pulls/{pr_number}/reviews") or []
    # Effective standing per reviewer: last APPROVED/CHANGES_REQUESTED, cleared
    # by a later DISMISSED; COMMENTED never changes standing.
    per_user: dict[str, str] = {}
    changes_bodies: list[str] = []
    for review in reviews if isinstance(reviews, list) else []:
        user = (review.get("user") or {}).get("login", "?")
        state = review.get("state", "")
        if state in ("APPROVED", "CHANGES_REQUESTED"):
            per_user[user] = state
        elif state == "DISMISSED":
            per_user.pop(user, None)
        if state == "CHANGES_REQUESTED" and (review.get("body") or "").strip():
            changes_bodies.append(f"@{user} requested changes:\n{review['body'].strip()}")

    standings = set(per_user.values())
    if "CHANGES_REQUESTED" in standings:
        decision: ReviewState = "CHANGES_REQUESTED"
    elif "APPROVED" in standings:
        decision = "APPROVED"
    else:
        decision = "REVIEW_REQUIRED" if reviews else "NONE"

    feedback = ""
    if decision == "CHANGES_REQUESTED":
        comments = _api(f"repos/{repo_slug}/pulls/{pr_number}/comments") or []
        inline: list[str] = []
        for c in comments if isinstance(comments, list) else []:
            body = (c.get("body") or "").strip()
            if body:
                path = c.get("path", "")
                inline.append(f"- {path}: {body}")
        parts = changes_bodies + (["Inline comments:", *inline] if inline else [])
        feedback = "\n\n".join(parts)[:_MAX_FEEDBACK_CHARS]
    return decision, feedback


def get_pr_state(repo_slug: str, pr_number: int) -> PRState:
    pull = _api(f"repos/{repo_slug}/pulls/{pr_number}") or {}
    lifecycle = _lifecycle(pull)
    head_sha = ((pull.get("head") or {}).get("sha")) or ""

    # No need to inspect CI/reviews on a terminal PR.
    if lifecycle != "OPEN":
        return PRState(lifecycle, "NONE", "NONE", head_sha, "", "")

    ci, failing_summary = _ci_state(repo_slug, head_sha) if head_sha else ("NONE", "")
    review, review_feedback = _review_state(repo_slug, pr_number)
    return PRState(
        lifecycle=lifecycle,
        ci=ci,
        review=review,
        head_sha=head_sha,
        failing_summary=failing_summary,
        review_feedback=review_feedback,
    )


def parse_pr_number(pr_url: str) -> int:
    """Extract the PR number from a github PR URL."""
    return int(pr_url.rstrip("/").rsplit("/", 1)[-1])
