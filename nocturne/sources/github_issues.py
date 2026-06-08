"""GitHub issue source: fetch eligible issues, comment, and check state via `gh` CLI.

All gh invocations route through `nocturne._gh_retry.run_gh` so rate-limit retries
and error classification are uniform across the codebase.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

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

# Re-export for convenience (callers shouldn't need to know about _gh_retry)
__all__ = [
    "GhAuthError",
    "GhError",
    "GhRateLimited",
    "GhSubprocessError",
    "IssueNotFound",
    "comment",
    "fetch_eligible",
    "fetch_one",
    "get_issue_state",
    "run_gh",
]


_JSON_FIELDS = "number,title,body,labels,assignees"
_JSON_FIELDS_WITH_STATE = "number,title,body,labels,assignees,state"


def _build_task(item: dict[str, Any], repo_cfg: RepoConfig) -> Task:
    """Construct a Task from a raw gh JSON item.

    coding_model and branch are left empty — the orchestrator fills them in
    from cfg.models.coding and branch_name(...). status starts as 'selected'.
    """
    now = datetime.now(timezone.utc)
    return Task(
        id=f"{repo_cfg.slug}#{item['number']}",
        repo_slug=repo_cfg.slug,
        checkout_path=repo_cfg.checkout_path,
        issue_number=item["number"],
        title=item["title"],
        body=item.get("body") or "",
        base=repo_cfg.base,
        verify_cmd=repo_cfg.verify_cmd,
        require_new_test=repo_cfg.require_new_test,
        coding_model="",
        branch="",
        status="selected",
        attempts=0,
        created_at=now,
        updated_at=now,
        pr_url=None,
        question=None,
        answer=None,
        opencode_pid=None,
    )


def fetch_eligible(repo_cfg: RepoConfig) -> list[Task]:
    """List open issues with the configured label, dropping assigned ones."""
    args = [
        "gh", "issue", "list",
        "--repo", repo_cfg.slug,
        "--label", repo_cfg.label,
        "--state", "open",
        "--json", _JSON_FIELDS,
    ]
    output = run_gh(args)
    items = json.loads(output) if output.strip() else []

    tasks: list[Task] = []
    for item in items:
        if item.get("assignees"):
            continue
        tasks.append(_build_task(item, repo_cfg))
    return tasks


def fetch_one(repo_slug: str, issue_number: int, repo_cfg: RepoConfig) -> Task:
    """Fetch a single issue by number. Raises GhError if the issue is CLOSED."""
    args = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo_slug,
        "--json", _JSON_FIELDS_WITH_STATE,
    ]
    output = run_gh(args)
    item = json.loads(output)
    state = item.get("state", "")
    if state != "OPEN":
        raise GhError(f"issue {repo_slug}#{issue_number} not open: state={state!r}")
    return _build_task(item, repo_cfg)


def comment(repo_slug: str, issue_number: int, body: str) -> None:
    """Post a comment on an issue."""
    run_gh([
        "gh", "issue", "comment", str(issue_number),
        "--repo", repo_slug,
        "--body", body,
    ])


def get_issue_state(repo_slug: str, issue_number: int) -> str:
    """Return 'OPEN' or 'CLOSED' for the given issue."""
    output = run_gh([
        "gh", "issue", "view", str(issue_number),
        "--repo", repo_slug,
        "--json", "state",
        "--jq", ".state",
    ])
    return output.strip()
