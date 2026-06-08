from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType

from nocturne.config import Config, RepoConfig


class GuardrailViolation(Exception):
    pass


def enforce_no_force_push(args: list[str]) -> None:
    if not args or args[0] != "git" or "push" not in args:
        return

    for arg in args:
        if arg in {"--force", "-f"} or arg.startswith("--force-with-lease") or arg.startswith("+"):
            raise GuardrailViolation(f"force push blocked: {arg}")


def enforce_no_auto_merge(args: list[str]) -> None:
    for idx in range(len(args) - 2):
        if args[idx : idx + 3] == ["gh", "pr", "merge"]:
            raise GuardrailViolation("auto merge blocked")


def enforce_no_dangerous_opencode_flags(args: list[str]) -> None:
    if "--dangerously-skip-permissions" in args:
        raise GuardrailViolation("dangerous opencode flag blocked")


def assert_not_main_branch(worktree_path: Path, expected_base: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    )
    branch = result.stdout.strip()
    if branch == expected_base:
        raise GuardrailViolation(f"worktree is on protected base branch: {expected_base}")


def check_repo_allowed(repo_slug: str, cfg: Config) -> RepoConfig:
    for repo in cfg.repos:
        if repo.slug == repo_slug:
            return repo
    raise GuardrailViolation(f"repo not allowlisted: {repo_slug}")


def check_wallclock(run_started: datetime, cfg: Config) -> timedelta:
    now = datetime.now(timezone.utc)
    elapsed = now - run_started
    budget = timedelta(hours=cfg.guardrails.global_wallclock_hours)
    remaining = budget - elapsed
    if remaining < timedelta(0):
        raise GuardrailViolation("wallclock budget exceeded")
    return remaining


def check_token_budget(tokens_used: int, cfg: Config) -> None:
    if tokens_used >= cfg.guardrails.token_budget:
        raise GuardrailViolation("token budget exceeded")


@dataclass
class WorktreeContext:
    worktree_path: Path
    expected_base: str

    def __enter__(self) -> "WorktreeContext":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        if exc_type is None:
            assert_not_main_branch(self.worktree_path, self.expected_base)
        return False
