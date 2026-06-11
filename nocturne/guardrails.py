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


# Path prefixes a generated PR must never touch. opencode acts on
# attacker-influenceable issue text, so a prompt injection could try to plant a
# malicious CI workflow (which runs with repo secrets on the next push) or drop
# a credential file. These paths are rarely legitimate for an autonomous bot to
# author, so we block the whole commit rather than ship them unreviewed.
_SENSITIVE_PATH_PREFIXES: tuple[str, ...] = (
    ".github/workflows/",
    ".github/actions/",
    ".ssh/",
    ".aws/",
)

# Exact basenames (anywhere in the tree) that indicate a secret/credential file.
_SENSITIVE_BASENAMES: frozenset[str] = frozenset({
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".pgpass",
    ".dockercfg",
    "credentials",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
})

# Suffixes that indicate private-key material regardless of basename.
_SENSITIVE_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx")


def _is_sensitive_path(path: str) -> bool:
    norm = path.replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    parts = norm.split("/")
    basename = parts[-1] if parts else norm
    if any(norm.startswith(prefix) for prefix in _SENSITIVE_PATH_PREFIXES):
        return True
    # A sensitive dir component anywhere in the path (e.g. config/.ssh/key).
    if ".ssh" in parts or ".aws" in parts:
        return True
    if basename in _SENSITIVE_BASENAMES:
        return True
    if basename == ".env" or basename.startswith(".env."):
        return True
    if any(basename.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES):
        return True
    return False


def find_sensitive_paths(paths: list[str]) -> list[str]:
    """Return the subset of ``paths`` that match a sensitive/credential pattern."""
    return [p for p in paths if _is_sensitive_path(p)]


def assert_no_sensitive_paths(paths: list[str]) -> None:
    """Raise GuardrailViolation if any staged path is sensitive (CI workflow,
    private key, credential file, ...). See ``_SENSITIVE_PATH_PREFIXES``.
    """
    offenders = find_sensitive_paths(paths)
    if offenders:
        raise GuardrailViolation(
            "sensitive paths blocked from PR: " + ", ".join(sorted(offenders))
        )


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
    ) -> None:
        if exc_type is None:
            assert_not_main_branch(self.worktree_path, self.expected_base)
