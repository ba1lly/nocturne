"""M1 acceptance test — runs the full end-to-end flow against the live sandbox.

Skipped by default; opt-in via env var NOCTURNE_RUN_M1=1.

This test exercises the complete M1 acceptance criteria:
  1. Pre-state cleanup (remove prior PRs)
  2. Run nocturne run-once against ba1lly/nocturne-playground Issue #1
  3. Assert PR created with correct metadata
  4. Assert PR diff includes test file(s)
  5. Assert pytest passes when applied to PR head
  6. Assert SQLite tasks row has status='done' and pr_url set
  7. Assert report file generated and mentions the issue
  8. Assert main branch untouched (no local commits ahead of origin/main)
"""

import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NOCTURNE_RUN_M1") != "1",
    reason="Set NOCTURNE_RUN_M1=1 to run live M1 acceptance",
)

REPO = os.environ.get("SANDBOX_REPO", "ba1lly/nocturne-playground")
ISSUE = int(os.environ.get("TARGET_ISSUE", "1"))
NOCTURNE_CONFIG = os.environ.get(
    "NOCTURNE_CONFIG", os.path.expanduser("~/.config/nocturne/config.yaml")
)


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory):
    """Create isolated state directory for this test run."""
    d = tmp_path_factory.mktemp("nocturne-m1")
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


def test_m1_precleanup():
    """Step 1: Clean up any prior nocturne/issue-N PRs."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--search",
            f"head:nocturne/issue-{ISSUE}",
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
    # Close any found PRs (may be none, which is fine)
    if result.stdout.strip():
        for url in result.stdout.strip().split("\n"):
            subprocess.run(
                ["gh", "pr", "close", url, "--delete-branch"],
                capture_output=True,
                check=False,
            )


def test_m1_run_nocturne(state_dir):
    """Step 2: Run nocturne run-once against the sandbox."""
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
            "--issue",
            str(ISSUE),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert result.returncode == 0, f"nocturne run-once failed:\n{result.stdout}\n{result.stderr}"


def test_m1_pr_created(state_dir):
    """Step 3: Assert PR created with correct metadata."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--search",
            f"head:nocturne/issue-{ISSUE}",
            "--state",
            "open",
            "--json",
            "url,body,state,number,headRefName",
            "--jq",
            ".[0]",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pr_json = result.stdout.strip()
    assert pr_json and pr_json != "null", "no PR created"

    pr = json.loads(pr_json)
    assert pr["state"] == "OPEN", f"PR state is {pr['state']}, expected OPEN"
    assert f"Closes #{ISSUE}" in pr["body"], f"PR body missing 'Closes #{ISSUE}'"

    # Store PR number for cleanup
    pytest.m1_pr_number = pr["number"]
    pytest.m1_pr_url = pr["url"]


def test_m1_pr_diff_includes_test(state_dir):
    """Step 4: Assert PR diff includes test file(s)."""
    pr_num = pytest.m1_pr_number
    result = subprocess.run(
        ["gh", "pr", "diff", str(pr_num), "--repo", REPO, "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    )
    files = result.stdout.strip().split("\n")
    test_files = [f for f in files if "test" in f and f.endswith(".py")]
    assert test_files, f"PR diff lacks test file(s). Files: {files}"


def test_m1_pytest_passes(state_dir):
    """Step 5: Assert pytest passes when applied to PR head."""
    pr_num = pytest.m1_pr_number
    pr_branch = None

    # Get PR branch name
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_num),
            "--repo",
            REPO,
            "--json",
            "headRefName",
            "--jq",
            ".headRefName",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pr_branch = result.stdout.strip().strip('"')

    # Clone PR branch to temp dir
    with tempfile.TemporaryDirectory(prefix="nocturne-m1-pr-") as tmp_checkout:
        clone_result = subprocess.run(
            [
                "gh",
                "repo",
                "clone",
                REPO,
                tmp_checkout,
                "--",
                "--depth",
                "1",
                "--branch",
                pr_branch,
            ],
            capture_output=True,
            text=True,
        )

        if clone_result.returncode != 0:
            # Fallback: use gh pr checkout
            subprocess.run(
                ["gh", "pr", "checkout", str(pr_num), "--repo", REPO, "-b", "m1-verify"],
                capture_output=True,
                check=False,
            )
            tmp_checkout = "."

        # Try pip install + pytest
        setup_result = subprocess.run(
            f"cd {tmp_checkout} && python3 -m venv .venv && .venv/bin/pip install -q -e . pytest",
            shell=True,
            capture_output=True,
            text=True,
        )

        if setup_result.returncode == 0:
            pytest_result = subprocess.run(
                f"cd {tmp_checkout} && .venv/bin/pytest -q",
                shell=True,
                capture_output=True,
                text=True,
            )
        else:
            # Fallback: direct pytest
            pytest_result = subprocess.run(
                f"cd {tmp_checkout} && python3 -m pytest -q",
                shell=True,
                capture_output=True,
                text=True,
            )

        assert (
            pytest_result.returncode == 0
        ), f"pytest failed:\n{pytest_result.stdout}\n{pytest_result.stderr}"


def test_m1_sqlite_row(state_dir):
    """Step 6: Assert SQLite tasks row has correct status and pr_url."""
    db_path = state_dir / "nocturne.db"
    assert db_path.exists(), f"nocturne.db not found at {db_path}"

    db = sqlite3.connect(str(db_path))
    row = db.execute(
        "SELECT status, pr_url, issue_number FROM tasks WHERE issue_number=?",
        (ISSUE,),
    ).fetchone()

    assert row is not None, f"no task row found for issue {ISSUE}"
    status, pr_url, issue_num = row
    assert status == "done", f"task status is {status}, expected 'done'"
    assert pr_url and "pull/" in pr_url, f"pr_url missing or invalid: {pr_url}"
    assert issue_num == ISSUE


def test_m1_report_exists(state_dir):
    """Step 7: Assert report file exists and mentions the issue."""
    reports_dir = state_dir / "reports"
    assert reports_dir.exists(), f"reports dir not found at {reports_dir}"

    reports = list(reports_dir.glob("*.md"))
    assert reports, "no report file generated"

    report_path = reports[0]
    report_text = report_path.read_text()
    assert f"Issue #{ISSUE}" in report_text, f"report missing 'Issue #{ISSUE}'"


def test_m1_main_untouched(state_dir):
    """Step 8: Assert main branch untouched in sandbox checkout."""
    checkout = Path(os.environ.get("SANDBOX_CHECKOUT", os.path.expanduser("~/projects/nocturne-playground-checkout")))

    if not (checkout / ".git").exists():
        pytest.skip("sandbox checkout not found; skipping main branch check")

    # Fetch origin/main
    subprocess.run(
        ["git", "-C", str(checkout), "fetch", "origin", "main"],
        capture_output=True,
        check=False,
    )

    # Check commits ahead
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-list", "--count", "origin/main..HEAD"],
        capture_output=True,
        text=True,
    )
    ahead = int(result.stdout.strip()) if result.returncode == 0 else 0
    assert ahead == 0, f"local main is {ahead} commit(s) ahead of origin/main"


@pytest.fixture(scope="module", autouse=True)
def cleanup_pr():
    """Cleanup: close the PR after all tests."""
    yield
    if hasattr(pytest, "m1_pr_number"):
        subprocess.run(
            [
                "gh",
                "pr",
                "close",
                str(pytest.m1_pr_number),
                "--repo",
                REPO,
                "--delete-branch",
            ],
            capture_output=True,
            check=False,
        )
