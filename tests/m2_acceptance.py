"""M2 acceptance test — runs the full end-to-end batch flow against the live sandbox.

Skipped by default; opt-in via env var NOCTURNE_RUN_M2=1.

This test exercises the complete M2 acceptance criteria:
  1. Pre-state cleanup (remove prior PRs and skip comments)
  2. Run nocturne run-once against ba1lly/nocturne-playground (batch mode, no --issue)
  3. Assert Issue #4 (TOO_BIG) has nocturne-skip comment
  4. Assert PRs created for at least 2 DOABLE issues (#1, #2, #5)
  5. Assert no PR for Issue #4 (correctly skipped)
  6. Assert RunReport generated with ≥2 done and ≥1 skipped
  7. Assert main branch untouched (no local commits ahead of origin/main)
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NOCTURNE_RUN_M2") != "1",
    reason="Set NOCTURNE_RUN_M2=1 to run live M2 acceptance",
)

REPO = os.environ.get("SANDBOX_REPO", "ba1lly/nocturne-playground")
NOCTURNE_CONFIG = os.environ.get(
    "NOCTURNE_CONFIG", os.path.expanduser("~/.config/nocturne/config.yaml")
)


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory):
    """Create isolated state directory for this test run."""
    d = tmp_path_factory.mktemp("nocturne-m2")
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


def test_m2_precleanup():
    """Step 1: Clean up any prior nocturne/issue-* PRs and skip comments."""
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

    # Attempt to remove prior skip comments on Issue #4
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            "4",
            "--repo",
            REPO,
            "--json",
            "comments",
            "--jq",
            '.comments[] | select(.body | startswith("<!-- nocturne-skip -->")) | .id // empty',
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


def test_m2_run_nocturne(state_dir):
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


def test_m2_skip_comment_on_issue_4(state_dir):
    """Step 3: Assert Issue #4 (TOO_BIG) has nocturne-skip comment."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            "4",
            "--repo",
            REPO,
            "--json",
            "comments",
            "--jq",
            '[.comments[] | select(.body | startswith("<!-- nocturne-skip -->"))] | .[0]',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    skip_comment = result.stdout.strip()
    assert skip_comment and skip_comment != "null", "no skip comment on Issue #4"


def test_m2_prs_for_doable_issues(state_dir):
    """Step 4: Assert PRs created for at least 2 DOABLE issues."""
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
            "url,headRefName,number",
            "--jq",
            "length",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pr_count = int(result.stdout.strip())
    assert pr_count >= 2, f"expected ≥2 PRs, got {pr_count}"

    # Store PR numbers for cleanup
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
            "number",
            "--jq",
            ".[].number",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pytest.m2_pr_numbers = [int(n) for n in result.stdout.strip().split("\n") if n]


def test_m2_no_pr_for_issue_4(state_dir):
    """Step 5: Assert no PR for Issue #4 (correctly skipped)."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--search",
            "head:nocturne/issue-4",
            "--state",
            "open",
            "--json",
            "url",
            "--jq",
            "length",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pr4_count = int(result.stdout.strip())
    assert pr4_count == 0, f"PR exists for SKIP issue #4 (expected 0, got {pr4_count})"


def test_m2_report_exists(state_dir):
    """Step 6: Assert RunReport generated with ≥2 done and ≥1 skipped."""
    reports_dir = state_dir / "reports"
    assert reports_dir.exists(), f"reports dir not found at {reports_dir}"

    reports = list(reports_dir.glob("*.md"))
    assert reports, "no report file generated"

    report_path = reports[0]
    report_text = report_path.read_text()

    # Check for "Done" section with at least 2 entries
    assert any(
        int(m.group(1)) >= 2
        for m in __import__("re").finditer(r"Done.*?(\d+)", report_text)
    ), "report does not show ≥2 done issues"

    # Check for "Skipped" section with at least 1 entry
    assert any(
        int(m.group(1)) >= 1
        for m in __import__("re").finditer(r"Skipped.*?(\d+)", report_text)
    ), "report does not show ≥1 skipped issues"


def test_m2_main_untouched(state_dir):
    """Step 7: Assert main branch untouched in sandbox checkout."""
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
def cleanup_prs():
    """Cleanup: close any opened PRs after all tests."""
    yield
    if hasattr(pytest, "m2_pr_numbers"):
        for pr_num in pytest.m2_pr_numbers:
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
