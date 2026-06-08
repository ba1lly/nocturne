from __future__ import annotations

import re
import subprocess
from pathlib import Path

from nocturne.models import Task, VerifyResult


TEST_PATH_REGEX = re.compile(r"(^|/)(test_[^/]+\.py$|[^/]+_test\.py$|tests?/)")


def is_test_file(path: str) -> bool:
    return TEST_PATH_REGEX.search(path) is not None


def _run_git_diff(worktree: Path, ref: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), "diff", "--name-only", ref],
        capture_output=True,
        text=True,
        check=False,
    )


def diff_includes_test(worktree: Path, base: str = "main") -> bool:
    for ref in (f"origin/{base}..HEAD", f"{base}..HEAD"):
        try:
            proc = _run_git_diff(worktree, ref)
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        for line in (proc.stdout or "").splitlines():
            if is_test_file(line.strip()):
                return True
    return False


def _collect_diagnostics(worktree: Path, verify_cmd: str) -> str:
    outputs: list[str] = [f"verify_cmd={verify_cmd}"]
    commands = [
        ["python3", "-m", "pytest", "--version"],
        ["python3", "--version"],
        ["ls", "-la"],
    ]
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(worktree), check=False)
            outputs.append(f"$ {' '.join(cmd)}")
            if proc.stdout:
                outputs.append(proc.stdout.rstrip())
            if proc.stderr:
                outputs.append(proc.stderr.rstrip())
        except Exception as exc:
            outputs.append(f"$ {' '.join(cmd)}")
            outputs.append(f"<diagnostic failed: {exc}>")
    return "\n".join(part for part in outputs if part)


def verify(task: Task, worktree: Path) -> VerifyResult:
    timeout = getattr(task, "verify_timeout", None) or 600
    try:
        proc = subprocess.run(
            task.verify_cmd,
            shell=True,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(
            passed=False,
            exit_code=-1,
            stdout="",
            stderr="",
            new_test_added=False,
            reason="verify_cmd timed out",
        )

    exit_code = proc.returncode
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    if exit_code != 0:
        reason = f"verify_cmd failed (exit {exit_code})"
        if len(stdout + stderr) < 50:
            diagnostics = _collect_diagnostics(worktree, task.verify_cmd)
            stderr = "\n".join(part for part in [stderr.rstrip(), diagnostics] if part)
        return VerifyResult(
            passed=False,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            new_test_added=False,
            reason=reason,
        )

    if task.require_new_test:
        has_test = diff_includes_test(worktree, task.base)
        if not has_test:
            return VerifyResult(
                passed=False,
                exit_code=0,
                stdout=stdout,
                stderr=stderr,
                new_test_added=False,
                reason="no test added",
            )
        return VerifyResult(
            passed=True,
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            new_test_added=True,
            reason=None,
        )

    return VerifyResult(
        passed=True,
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        new_test_added=False,
        reason=None,
    )
