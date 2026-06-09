"""Shared retry helper for `gh` CLI invocations.

Classifies gh stderr into auth / rate-limit / not-found / generic and retries
ONLY on rate-limit, with exponential backoff. Auth + not-found + generic failures
raise immediately. Reused by github_issues, skip-comments (Task 22), and
issue-state checks (Task 28).
"""

from __future__ import annotations

import subprocess
import time
from typing import Callable


class GhError(Exception):
    """Base for all gh CLI errors raised by run_gh."""


class GhRateLimited(GhError):
    """gh hit the API rate limit (HTTP 403 / rate limit exceeded)."""


class GhAuthError(GhError):
    """gh authentication failed (HTTP 401 / bad credentials)."""


class IssueNotFound(GhError):
    """gh could not locate the requested resource (HTTP 404)."""


class GhSubprocessError(GhError):
    """gh failed with an unrecognized error pattern."""


RATE_LIMIT_PATTERNS = ("api rate limit exceeded", "http 403", "rate limit")
AUTH_PATTERNS = ("http 401", "authentication required", "bad credentials")
NOT_FOUND_PATTERNS = ("http 404", "could not resolve to", "not found")


def _classify_error(stderr: str) -> type[GhError] | None:
    """Inspect stderr (case-insensitive) and return matching exception class.

    Auth check runs FIRST (most-specific) since a 401 payload could in principle
    contain other tokens. Returns None for unrecognized failures (generic).
    """
    lowered = stderr.lower()
    if any(p in lowered for p in AUTH_PATTERNS):
        return GhAuthError
    if any(p in lowered for p in RATE_LIMIT_PATTERNS):
        return GhRateLimited
    if any(p in lowered for p in NOT_FOUND_PATTERNS):
        return IssueNotFound
    return None


def run_gh(
    args: list[str],
    *,
    retry: bool = True,
    max_attempts: int = 3,
    backoff_base: float = 1.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """Invoke a `gh` subprocess with retry on rate-limit.

    Retries ONLY on rate-limit (exponential: backoff_base * 2**attempt → 1s, 2s, 4s).
    Auth / not-found / generic failures raise immediately. sleep_fn is injectable
    for test purposes.
    """
    if not args or args[0] != "gh":
        raise ValueError(f"run_gh args must start with 'gh', got: {args[:1]!r}")

    last_stderr = ""
    for attempt in range(max_attempts):
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout

        last_stderr = completed.stderr or ""
        cls = _classify_error(last_stderr)

        if cls is GhAuthError:
            raise GhAuthError(last_stderr)
        if cls is IssueNotFound:
            raise IssueNotFound(last_stderr)
        if cls is GhRateLimited:
            if retry and attempt < max_attempts - 1:
                sleep_fn(backoff_base * (2 ** attempt))
                continue
            raise GhRateLimited(last_stderr)
        # Generic / unknown failure: no retry
        raise GhSubprocessError(last_stderr)

    # Defensive: exhausted attempts while seeing only rate-limits
    raise GhRateLimited(last_stderr)
