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
            # path exists but isn't a registered worktree — fall through to rmtree
            pass
        shutil.rmtree(worktree_path, ignore_errors=True)

    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", str(worktree_path), "-b", branch, base],
        check=True,
        capture_output=True,
        text=True,
    )

    _install_pre_push_hook(worktree_path)

    return worktree_path


def commit_push(wt: Path, message: str) -> None:
    subprocess.run(
        ["git", "-C", str(wt), "add", "-A"],
        check=True,
        capture_output=True,
        text=True,
    )

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
        # worktree already gone — swallow
        pass
