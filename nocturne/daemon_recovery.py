"""Daemon crash recovery — worktree prune + PID-based task reconciliation.

Called on daemon startup (Task 32 wires this). Handles:
1. Pruning stale worktree registrations in each cfg.repos[].checkout_path
2. Marking tasks as 'failed' if their tracked PID is no longer alive
3. Removing ghost worktree entries (registered but missing on disk)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from nocturne._logging import get_logger
from nocturne.config import Config
from nocturne.store import Store

logger = get_logger("nocturne.daemon_recovery")


def pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is alive.

    Uses os.kill(pid, 0): 0 is the null signal, raises ProcessLookupError if no such PID.
    PermissionError means the process exists but is owned by a different uid — still alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # different uid, but the process exists
    except OSError:
        return False


def _parse_worktree_porcelain(output: str) -> list[Path]:
    """Parse ``git worktree list --porcelain`` output → list of worktree Paths.

    Porcelain format: blocks separated by blank lines, each block has lines like::

        worktree /abs/path
        HEAD <sha>
        branch refs/heads/<name>
    """
    paths: list[Path] = []
    for line in output.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree "):].strip()))
    return paths


def reconcile_worktrees(cfg: Config) -> list[Path]:
    """Per-repo: ``git worktree prune`` + remove ghost (registered-but-missing-on-disk) entries.

    Returns the list of worktree paths that were cleaned up.
    """
    cleaned: list[Path] = []
    for repo_cfg in cfg.repos:
        repo_path = Path(repo_cfg.checkout_path).expanduser()
        if not (repo_path / ".git").exists():
            logger.warning(
                "repo %s has no .git/ at %s; skipping prune",
                repo_cfg.slug,
                repo_path,
            )
            continue
        # Step 1: standard prune (removes registrations for missing dirs that were force-removed)
        try:
            subprocess.run(
                ["git", "-C", str(repo_path), "worktree", "prune", "--verbose"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as e:
            logger.warning("worktree prune failed in %s: %s", repo_path, e.stderr)
            continue
        # Step 2: list remaining worktrees + check disk presence
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "worktree", "list", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as e:
            logger.warning("worktree list failed in %s: %s", repo_path, e.stderr)
            continue
        registered_paths = _parse_worktree_porcelain(result.stdout)
        # The main repo itself is in the list; skip it
        for wt_path in registered_paths:
            try:
                same_as_repo = wt_path == repo_path or wt_path.resolve() == repo_path.resolve()
            except OSError:
                same_as_repo = wt_path == repo_path
            if same_as_repo:
                continue
            if not wt_path.exists():
                # Ghost entry — force-remove from worktree registry
                try:
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repo_path),
                            "worktree",
                            "remove",
                            "--force",
                            str(wt_path),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    cleaned.append(wt_path)
                    logger.info("removed ghost worktree entry: %s", wt_path)
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning("could not remove ghost worktree %s: %s", wt_path, e)
    return cleaned


def reconcile_tasks(store: Store) -> dict[str, int]:
    """Scan for tasks with status 'running' or 'selected' (with PID); mark failed if PID dead.

    Also handles the 'running with no PID' case (interrupted before PID write) — marks failed.
    Parked tasks are NEVER touched.
    Returns a summary dict: {'killed_running', 'killed_selected', 'unchanged'}
    """
    summary: dict[str, int] = {"killed_running": 0, "killed_selected": 0, "unchanged": 0}
    for status in ("running", "selected"):
        tasks = store.list_by_status(status)
        for task in tasks:
            pid = task.opencode_pid
            # Case 1: no PID stored but status is 'running' → interrupted before PID write
            if status == "running" and pid is None:
                store.update_status(task.id, "failed")
                logger.info(
                    "task %s status=running with no PID; marked failed (interrupted before PID write)",
                    task.id,
                )
                summary["killed_running"] += 1
                continue
            # Case 2: PID stored, check if alive
            if pid is not None:
                if not pid_alive(pid):
                    store.update_status(task.id, "failed")
                    key = "killed_running" if status == "running" else "killed_selected"
                    summary[key] += 1
                    logger.info(
                        "task %s pid=%s not alive; marked failed (daemon_restart_recovery)",
                        task.id,
                        pid,
                    )
                    continue
            summary["unchanged"] += 1
    return summary


def reconcile(cfg: Config, store: Store) -> dict[str, object]:
    """Run both worktree + task reconciliation. Returns combined summary."""
    logger.info("starting daemon recovery reconciliation")
    cleaned_worktrees = reconcile_worktrees(cfg)
    task_summary = reconcile_tasks(store)
    combined: dict[str, object] = {
        "worktrees_cleaned": [str(p) for p in cleaned_worktrees],
        "worktrees_cleaned_count": len(cleaned_worktrees),
        **task_summary,
    }
    logger.info(
        "reconciliation complete: worktrees_cleaned=%s killed_running=%s killed_selected=%s unchanged=%s",
        combined["worktrees_cleaned_count"],
        combined["killed_running"],
        combined["killed_selected"],
        combined["unchanged"],
    )
    return combined
