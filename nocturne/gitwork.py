from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from nocturne.guardrails import enforce_no_force_push


class GitworkError(Exception):
    pass


_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+")

_NOCTURNE_ENV = {
    "GIT_AUTHOR_NAME": "Nocturne",
    "GIT_AUTHOR_EMAIL": "nocturne@noreply.localhost",
    "GIT_COMMITTER_NAME": "Nocturne",
    "GIT_COMMITTER_EMAIL": "nocturne@noreply.localhost",
}


def _run_git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    full_args = ["git", "-C", str(repo), *args]
    run_env: dict[str, str] | None = None
    if env is not None:
        run_env = {**os.environ, **env}
    return subprocess.run(
        full_args,
        check=check,
        capture_output=capture,
        text=True,
        env=run_env,
    )


def prune_worktrees(repo_path: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "prune"],
        check=True,
    )


def branch_name(issue_id: int, attempt: int) -> str:
    return f"nocturne/issue-{issue_id}-{attempt}"


def _install_pre_push_hook(worktree_path: Path) -> None:
    hooks_dir_result = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "--git-path", "hooks"],
        check=True,
        capture_output=True,
        text=True,
    )
    rel_hooks = hooks_dir_result.stdout.strip()
    hooks_dir = Path(rel_hooks)
    if not hooks_dir.is_absolute():
        hooks_dir = (worktree_path / rel_hooks).resolve()
    hooks_dir.mkdir(parents=True, exist_ok=True)

    src = Path(__file__).parent / "_hooks" / "pre-push"
    dst = hooks_dir / "pre-push"
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o755)


def make_worktree(
    repo_path: Path,
    branch: str,
    base: str,
    worktree_path: Path,
) -> Path:
    prune_worktrees(repo_path)

    if worktree_path.exists():
        try:
            subprocess.run(
                ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree_path)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            # path exists but isn't a registered worktree - fall through to rmtree
            pass
        shutil.rmtree(worktree_path, ignore_errors=True)

    # Force-delete any pre-existing local branch with this name (leftover from a prior
    # failed attempt). check=False because the branch usually does NOT exist; we only
    # care that `worktree add -b` below sees a clean slate.
    subprocess.run(
        ["git", "-C", str(repo_path), "branch", "-D", branch],
        check=False,
        capture_output=True,
        text=True,
    )

    result = subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", str(worktree_path), "-b", branch, base],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitworkError(
            f"git worktree add failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    _install_pre_push_hook(worktree_path)
    _add_nocturne_local_excludes(worktree_path)

    return worktree_path


# Patterns nocturne adds to each worktree's local .git/info/exclude so that
# commit_push's `git add -A` never sweeps them into a real commit.
#
#   - nocturne-internal artifacts it writes itself (PR body, review output)
#   - universally-non-committable build/cache junk that verify_cmd (pytest,
#     npm, etc.) generates. A target repo with a complete .gitignore already
#     ignores these; this is defense-in-depth for repos that don't, so a stray
#     __pycache__/*.pyc never lands in a generated PR.
#
# .git/info/exclude is a local-only ignore (never committed) and is shared
# across a repo's worktrees, so this is the right scope: nocturne keeps its
# commits clean without imposing a .gitignore on the user's repo.
_NOCTURNE_EXCLUDE_PATTERNS: list[str] = [
    # nocturne-internal
    ".nocturne-pr-body.md",  # opencode writes the PR title+body here (task.md.jinja2)
    ".reviews/",             # /review-pr persists multi-agent review output here
    # build / test / tooling caches (never legitimately committed)
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage",
    "node_modules/",
]


def _add_nocturne_local_excludes(worktree_path: Path) -> None:
    """Ensure every pattern in ``_NOCTURNE_EXCLUDE_PATTERNS`` is present in the
    worktree's local .git/info/exclude.

    Appends only the patterns that are missing (per-line), so it is idempotent
    across repeated worktrees AND correctly upgrades exclude files written by
    an older nocturne version that listed fewer patterns. User-added rules are
    preserved untouched.
    """
    git_dir_result = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "--git-path", "info/exclude"],
        check=False,
        capture_output=True,
        text=True,
    )
    if git_dir_result.returncode != 0:
        return

    exclude_rel = git_dir_result.stdout.strip()
    exclude_path = Path(exclude_rel)
    if not exclude_path.is_absolute():
        exclude_path = (worktree_path / exclude_rel).resolve()
    exclude_path.parent.mkdir(parents=True, exist_ok=True)

    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    existing_lines = {line.strip() for line in existing.splitlines()}
    missing = [p for p in _NOCTURNE_EXCLUDE_PATTERNS if p not in existing_lines]
    if not missing:
        return

    additions = ["# nocturne-internal + build artifacts (auto-added by nocturne/gitwork)", *missing]
    sep = "" if existing.endswith("\n") or not existing else "\n"
    exclude_path.write_text(existing + sep + "\n".join(additions) + "\n", encoding="utf-8")


def commit_push(wt: Path, message: str, base: str) -> None:
    subprocess.run(
        ["git", "-C", str(wt), "reset", "--soft", f"origin/{base}"],
        check=True,
        capture_output=True,
        text=True,
    )

    subprocess.run(
        ["git", "-C", str(wt), "add", "-A"],
        check=True,
        capture_output=True,
        text=True,
    )

    diff_check = subprocess.run(
        ["git", "-C", str(wt), "diff", "--cached", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
    )
    if diff_check.returncode == 0:
        raise GitworkError("commit_push: no changes to commit after reset to base")

    commit_args = [
        "git",
        "-C",
        str(wt),
        "-c",
        "user.name=Nocturne",
        "-c",
        "user.email=nocturne@noreply.localhost",
        "commit",
        "-m",
        message,
    ]
    subprocess.run(
        commit_args,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_NOCTURNE_ENV},
    )

    push_args = ["git", "-C", str(wt), "push", "-u", "origin", "HEAD"]
    enforce_no_force_push(push_args)
    subprocess.run(
        push_args,
        check=True,
        capture_output=True,
        text=True,
    )


def open_pr(repo: str, branch: str, base: str, title: str, body: str) -> str:
    args = [
        "gh",
        "pr",
        "create",
        "--repo",
        repo,
        "--base",
        base,
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
    ]
    last_stderr = ""
    for attempt in range(3):
        result = subprocess.run(args, check=False, capture_output=True, text=True)
        last_stderr = result.stderr or ""

        if result.returncode == 0:
            match = _PR_URL_RE.search(result.stdout or "")
            if match:
                return match.group(0)
            raise GitworkError(f"gh succeeded but no PR URL in stdout: {result.stdout!r}")

        combined = last_stderr + "\n" + (result.stdout or "")
        match = _PR_URL_RE.search(combined)
        if match:
            return match.group(0)

        lower = last_stderr.lower()
        if "rate limit" not in lower and "http 403" not in lower:
            break
        if attempt < 2:
            time.sleep(1.0 * (2 ** attempt))

    raise GitworkError(f"failed to open PR: {last_stderr}")


def cleanup(wt: Path, repo_path: Path) -> None:
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(wt)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        # worktree already gone - swallow
        pass
