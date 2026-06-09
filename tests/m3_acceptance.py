"""M3 acceptance test - runs the full ask/park/resume flow against the live sandbox.

Skipped by default; opt-in via env var NOCTURNE_RUN_M3=1.

This test exercises the complete M3 acceptance criteria:
  1. Pre-state cleanup (remove prior PRs and question comments)
  2. Run nocturne run-once against ba1lly/nocturne-playground (batch mode)
  3. Assert Issue #3 (AMBIGUOUS) parks with a question comment
  4. Assert Issue #5 (literal sentinel in body) is NOT falsely parked
  5. Resume Issue #3 with concrete answer
  6. Poll up to 120s for Issue #3 status → done
  7. Assert PR created for Issue #3
  8. Assert no duplicate question comments on Issue #3 (Metis invariant)
"""

import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NOCTURNE_RUN_M3") != "1",
    reason="Set NOCTURNE_RUN_M3=1 to run live M3 acceptance",
)

REPO = os.environ.get("SANDBOX_REPO", "ba1lly/nocturne-playground")
NOCTURNE_CONFIG = os.environ.get(
    "NOCTURNE_CONFIG", os.path.expanduser("~/.config/nocturne/config.yaml")
)


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory):
    """Create isolated state directory for this test run."""
    d = tmp_path_factory.mktemp("nocturne-m3")
    yield d
    # cleanup handled by tmp_path_factory


@pytest.fixture(scope="module", autouse=True)
def preflight_checks():
    """Verify all preconditions before running tests."""
    # gh CLI
    result = subprocess.run(["command", "-v", "gh"], shell=True, capture_output=True)
    assert result.returncode == 0, "gh CLI not found"

    # jq
    result = subprocess.run(["command", "-v", "jq"], shell=True, capture_output=True)
    assert result.returncode == 0, "jq not found"

    # DASHSCOPE_API_KEY
    assert os.environ.get("DASHSCOPE_API_KEY"), "DASHSCOPE_API_KEY not set"

    # config file
    assert Path(NOCTURNE_CONFIG).exists(), f"config file not found at {NOCTURNE_CONFIG}"

    # nocturne CLI
    result = subprocess.run(
        [".venv/bin/nocturne", "version"], capture_output=True, text=True
    )
    assert result.returncode == 0, "nocturne CLI not working"

    # gh auth
    result = subprocess.run(["gh", "auth", "status"], capture_output=True)
    assert result.returncode == 0, "gh not authenticated"


def test_m3_precleanup():
    """Step 1: Clean up any prior nocturne/issue-* PRs and question comments."""
    # Close any open PRs from prior runs
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--search",
            "head:nocturne/issue-",
            "--state",
            "open",
            "--json",
            "url",
            "--jq",
            ".[].url",
        ],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        for url in result.stdout.strip().split("\n"):
            subprocess.run(
                ["gh", "pr", "close", url, "--delete-branch"],
                capture_output=True,
                check=False,
            )

    # Attempt to remove prior question/skip comments on Issues 3 and 5
    for issue_num in [3, 5]:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_num),
                "--repo",
                REPO,
                "--json",
                "comments",
                "--jq",
                '.comments[] | select(.body | contains("nocturne-question") or contains("##NOCTURNE_NEED_INPUT##") or contains("nocturne-skip")) | .id // empty',
            ],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            for comment_id in result.stdout.strip().split("\n"):
                if comment_id:
                    subprocess.run(
                        ["gh", "api", "-X", "DELETE", f"/repos/{REPO}/issues/comments/{comment_id}"],
                        capture_output=True,
                        check=False,
                    )


def test_m3_run_nocturne(state_dir):
    """Step 2: Run nocturne run-once in batch mode (no --issue)."""
    result = subprocess.run(
        [
            ".venv/bin/nocturne",
            "--config",
            NOCTURNE_CONFIG,
            "--state-dir",
            str(state_dir),
            "run-once",
            "--repo",
            REPO,
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert result.returncode == 0, f"nocturne run-once failed:\n{result.stdout}\n{result.stderr}"


def test_m3_issue_3_parked(state_dir):
    """Step 3: Assert Issue #3 (AMBIGUOUS) is parked."""
    db_path = state_dir / "nocturne.db"
    assert db_path.exists(), f"nocturne.db not found at {db_path}"

    db = sqlite3.connect(str(db_path))
    row = db.execute(
        "SELECT status FROM tasks WHERE issue_number=3"
    ).fetchone()

    assert row is not None, "no task row found for issue 3"
    status = row[0]
    assert status == "parked", f"Issue #3 status is {status}, expected 'parked'"


def test_m3_issue_3_has_question_comment(state_dir):
    """Step 4: Assert Issue #3 has a question comment."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            "3",
            "--repo",
            REPO,
            "--json",
            "comments",
            "--jq",
            '[.comments[] | select(.body | contains("##NOCTURNE_NEED_INPUT##") or contains("nocturne-question") or contains("clarif") or contains("question"))] | length',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    q_count = int(result.stdout.strip())
    assert q_count >= 1, f"Issue #3 has no question comment (count={q_count})"


def test_m3_issue_5_not_falsely_parked(state_dir):
    """Step 5: Assert Issue #5 (literal sentinel) is NOT falsely parked.

    CRITICAL: This is the false-positive guard. Issue #5 contains a literal
    sentinel string in its body but should NOT be parked (Task 13's last-event-only
    detector should prevent this).
    """
    db_path = state_dir / "nocturne.db"
    assert db_path.exists(), f"nocturne.db not found at {db_path}"

    db = sqlite3.connect(str(db_path))
    row = db.execute(
        "SELECT status FROM tasks WHERE issue_number=5"
    ).fetchone()

    if row is None:
        status = "absent"
    else:
        status = row[0]

    # Issue #5 should be done, failed, or absent - but NOT parked
    assert status != "parked", f"CRITICAL: Issue #5 falsely parked (status={status})"
    assert status in ["done", "failed", "absent"], f"Issue #5 unexpected status: {status}"


def test_m3_resume_issue_3(state_dir):
    """Step 6: Resume Issue #3 with concrete answer."""
    answer = "Add a function median(values) that returns the median of a list of numbers, with tests covering empty list (ValueError), single value, even count, odd count."

    result = subprocess.run(
        [
            ".venv/bin/nocturne",
            "--config",
            NOCTURNE_CONFIG,
            "--state-dir",
            str(state_dir),
            "resume",
            "--task-id",
            f"{REPO}#3",
            "--answer",
            answer,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"nocturne resume failed:\n{result.stdout}\n{result.stderr}"


def test_m3_run_nocturne_after_resume(state_dir):
    """Step 7: Re-run nocturne run-once to process resumed task."""
    result = subprocess.run(
        [
            ".venv/bin/nocturne",
            "--config",
            NOCTURNE_CONFIG,
            "--state-dir",
            str(state_dir),
            "run-once",
            "--repo",
            REPO,
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert result.returncode == 0, f"nocturne run-once (2nd) failed:\n{result.stdout}\n{result.stderr}"


def test_m3_issue_3_transitions_to_done(state_dir):
    """Step 8: Poll up to 120s for Issue #3 status → done."""
    db_path = state_dir / "nocturne.db"
    deadline = time.time() + 120
    status = "unknown"

    while time.time() < deadline:
        db = sqlite3.connect(str(db_path))
        row = db.execute(
            "SELECT status FROM tasks WHERE issue_number=3"
        ).fetchone()

        if row is None:
            status = "absent"
        else:
            status = row[0]

        if status == "done":
            break

        if status == "failed":
            pytest.fail("Issue #3 went to failed after resume")

        time.sleep(5)

    assert status == "done", f"Issue #3 not done after 120s (status={status})"


def test_m3_pr_created_for_issue_3(state_dir):
    """Step 9: Assert PR created for Issue #3."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--search",
            "head:nocturne/issue-3",
            "--state",
            "open",
            "--json",
            "url,body,number",
            "--jq",
            "length",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pr_count = int(result.stdout.strip())
    assert pr_count >= 1, f"no PR for Issue #3 (count={pr_count})"

    # Store PR numbers for cleanup
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--search",
            "head:nocturne/issue-3",
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            ".[].number",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pytest.m3_pr_numbers = [int(n) for n in result.stdout.strip().split("\n") if n]


def test_m3_no_duplicate_question_comments(state_dir):
    """Step 10: Assert no duplicate question comments on Issue #3 (Metis invariant).

    CRITICAL: Metis flagged this as a regression risk. We must ensure that
    the question comment is posted exactly once, not duplicated on retries.
    """
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            "3",
            "--repo",
            REPO,
            "--json",
            "comments",
            "--jq",
            '[.comments[] | select(.body | contains("nocturne-question") or contains("##NOCTURNE_NEED_INPUT##"))] | length',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    dup_count = int(result.stdout.strip())
    assert dup_count <= 1, f"duplicate question comments ({dup_count} found, expected ≤1)"


@pytest.fixture(scope="module", autouse=True)
def cleanup_prs():
    """Cleanup: close any opened PRs after all tests."""
    yield
    if hasattr(pytest, "m3_pr_numbers"):
        for pr_num in pytest.m3_pr_numbers:
            subprocess.run(
                [
                    "gh",
                    "pr",
                    "close",
                    str(pr_num),
                    "--repo",
                    REPO,
                    "--delete-branch",
                ],
                capture_output=True,
                check=False,
            )
