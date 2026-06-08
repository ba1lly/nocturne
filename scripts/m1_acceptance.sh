#!/bin/bash
# M1 Acceptance Test Framework
# End-to-end test against ba1lly/nocturne-playground sandbox
# Usage: bash scripts/m1_acceptance.sh
# Environment:
#   SANDBOX_REPO (default: ba1lly/nocturne-playground)
#   TARGET_ISSUE (default: 1)
#   DASHSCOPE_API_KEY (required)
#   NOCTURNE_CONFIG (default: ~/.config/nocturne/config.yaml)
#   SANDBOX_CHECKOUT (default: ~/projects/nocturne-playground-checkout)

set -euo pipefail

# ============================================================================
# SETUP & CLEANUP
# ============================================================================

REPO="${SANDBOX_REPO:-ba1lly/nocturne-playground}"
ISSUE="${TARGET_ISSUE:-1}"
EVIDENCE_DIR=".omo/evidence"
CHECKOUT="${SANDBOX_CHECKOUT:-$HOME/projects/nocturne-playground-checkout}"
STATE_DIR=""
TMP_CHECKOUT=""
PR_NUM=""

mkdir -p "$EVIDENCE_DIR"

cleanup() {
    local exit_code=$?
    if [ -n "$PR_NUM" ]; then
        gh pr close "$PR_NUM" --repo "$REPO" --delete-branch 2>/dev/null || true
    fi
    if [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ]; then
        rm -rf "$STATE_DIR" 2>/dev/null || true
    fi
    if [ -n "$TMP_CHECKOUT" ] && [ -d "$TMP_CHECKOUT" ]; then
        rm -rf "$TMP_CHECKOUT" 2>/dev/null || true
    fi
    exit $exit_code
}

trap cleanup EXIT

# ============================================================================
# PREFLIGHT CHECKS
# ============================================================================

echo "=== M1 Acceptance: Preflight Checks ==="

if ! command -v gh >/dev/null 2>&1; then
    echo "FAIL: gh CLI not found. Install via: https://cli.github.com/"
    exit 1
fi
echo "✓ gh CLI present"

if ! command -v jq >/dev/null 2>&1; then
    echo "FAIL: jq not found. Install via: apt-get install jq (or brew install jq)"
    exit 1
fi
echo "✓ jq present"

if [ -z "${DASHSCOPE_API_KEY:-}" ]; then
    echo "FAIL: DASHSCOPE_API_KEY env var not set"
    exit 1
fi
echo "✓ DASHSCOPE_API_KEY set"

NOCTURNE_CONFIG="${NOCTURNE_CONFIG:-$HOME/.config/nocturne/config.yaml}"
if [ ! -f "$NOCTURNE_CONFIG" ]; then
    echo "FAIL: config file not found at $NOCTURNE_CONFIG"
    echo "  Create it from config.example.yaml and fill in Discord IDs"
    exit 1
fi
echo "✓ config file exists at $NOCTURNE_CONFIG"

if ! .venv/bin/nocturne version >/dev/null 2>&1; then
    echo "FAIL: nocturne CLI not working"
    exit 1
fi
echo "✓ nocturne CLI works"

if ! gh auth status >/dev/null 2>&1; then
    echo "FAIL: gh not authenticated. Run: gh auth login"
    exit 1
fi
echo "✓ gh authenticated"

echo ""

# ============================================================================
# STEP 1: PRE-STATE CLEANUP
# ============================================================================

echo "=== Step 1: Pre-state Cleanup ==="
gh pr list --repo "$REPO" --search "head:nocturne/issue-${ISSUE}" --state open --json url --jq '.[].url' \
    | xargs -r -I{} gh pr close {} --delete-branch 2>/dev/null || true
echo "✓ Cleaned up any prior nocturne/issue-${ISSUE} PRs"
echo "" > "$EVIDENCE_DIR/milestone-M1-precleanup.log"

# ============================================================================
# STEP 2: RUN NOCTURNE RUN-ONCE
# ============================================================================

echo "=== Step 2: Run nocturne run-once ==="
STATE_DIR=$(mktemp -d -t nocturne-m1-XXXXXX)
echo "State dir: $STATE_DIR"

RUN_EXIT=0
.venv/bin/nocturne --config "$NOCTURNE_CONFIG" \
                  --state-dir "$STATE_DIR" \
                  run-once --repo "$REPO" --issue "$ISSUE" 2>&1 \
    | tee "$EVIDENCE_DIR/milestone-M1-run.log" || RUN_EXIT=$?

if [ $RUN_EXIT -ne 0 ]; then
    echo "FAIL: nocturne run-once exited with code $RUN_EXIT"
    exit 1
fi
echo "✓ nocturne run-once completed successfully"
echo ""

# ============================================================================
# STEP 3: ASSERT PR CREATED
# ============================================================================

echo "=== Step 3: Assert PR Created ==="
PR_JSON=$(gh pr list --repo "$REPO" --search "head:nocturne/issue-${ISSUE}" --state open \
                      --json url,body,state,number,headRefName --jq '.[0]')
echo "$PR_JSON" > "$EVIDENCE_DIR/milestone-M1-pr.json"

if [ -z "$PR_JSON" ] || [ "$PR_JSON" = "null" ]; then
    echo "FAIL: no PR created for nocturne/issue-${ISSUE}"
    exit 1
fi

PR_URL=$(echo "$PR_JSON" | jq -r '.url')
PR_NUM=$(echo "$PR_JSON" | jq -r '.number')
PR_STATE=$(echo "$PR_JSON" | jq -r '.state')
PR_BODY=$(echo "$PR_JSON" | jq -r '.body')

if [ "$PR_STATE" != "OPEN" ]; then
    echo "FAIL: PR state is $PR_STATE, expected OPEN"
    exit 1
fi
echo "✓ PR #$PR_NUM is OPEN"

if ! echo "$PR_BODY" | grep -q "Closes #${ISSUE}"; then
    echo "FAIL: PR body does not contain 'Closes #${ISSUE}'"
    exit 1
fi
echo "✓ PR body contains 'Closes #${ISSUE}'"
echo "✓ PR URL: $PR_URL"
echo ""

# ============================================================================
# STEP 4: ASSERT PR DIFF INCLUDES TEST FILE
# ============================================================================

echo "=== Step 4: Assert PR Diff Includes Test File ==="
gh pr diff "$PR_NUM" --repo "$REPO" --name-only \
    | tee "$EVIDENCE_DIR/milestone-M1-diff-files.log" > /tmp/pr_files.txt

if ! grep -qE "tests?/.*\.py" /tmp/pr_files.txt; then
    echo "FAIL: PR diff does not include a test file (tests/*.py)"
    echo "Files in diff:"
    cat /tmp/pr_files.txt
    exit 1
fi
echo "✓ PR diff includes test file(s)"
echo ""

# ============================================================================
# STEP 5: ASSERT PYTEST PASSES WHEN APPLIED
# ============================================================================

echo "=== Step 5: Assert pytest Passes Against PR Head ==="
TMP_CHECKOUT=$(mktemp -d -t nocturne-m1-pr-XXXXXX)
PR_BRANCH=$(echo "$PR_JSON" | jq -r '.headRefName')

# Try to clone the PR branch
if ! gh repo clone "$REPO" "$TMP_CHECKOUT" -- --depth 1 --branch "$PR_BRANCH" 2>/dev/null; then
    echo "  (clone failed, trying gh pr checkout)"
    gh pr checkout "$PR_NUM" --repo "$REPO" -b m1-verify 2>/dev/null || true
    TMP_CHECKOUT="$CHECKOUT"
fi

# Run pytest in the temp checkout
if ! ( cd "$TMP_CHECKOUT" && python3 -m venv .venv 2>/dev/null && .venv/bin/pip install -q -e . pytest 2>/dev/null && .venv/bin/pytest -q ) \
    | tee "$EVIDENCE_DIR/milestone-M1-pytest.log"; then
    echo "  (pip install or pytest failed, trying direct pytest)"
    if ! ( cd "$TMP_CHECKOUT" && python3 -m pytest -q ) \
        | tee "$EVIDENCE_DIR/milestone-M1-pytest.log"; then
        echo "FAIL: pytest failed against PR head"
        exit 1
    fi
fi
echo "✓ pytest passes against PR head"
echo ""

# ============================================================================
# STEP 6: ASSERT SQLITE TASKS ROW
# ============================================================================

echo "=== Step 6: Assert SQLite Tasks Row ==="
DB_PATH="$STATE_DIR/nocturne.db"

if [ ! -f "$DB_PATH" ]; then
    echo "FAIL: nocturne.db not found at $DB_PATH"
    exit 1
fi
echo "✓ nocturne.db exists"

SQLITE_ROW=$(python3 << 'PYEOF'
import sqlite3
import json
try:
    db = sqlite3.connect('DBPATH')
    row = db.execute("SELECT status, pr_url, issue_number FROM tasks WHERE issue_number=ISSUE").fetchone()
    if row:
        print(json.dumps({"status": row[0], "pr_url": row[1], "issue_number": row[2]}))
    else:
        print("null")
except Exception as e:
    print(f"error: {e}")
PYEOF
)
SQLITE_ROW="${SQLITE_ROW//DBPATH/$DB_PATH}"
SQLITE_ROW="${SQLITE_ROW//ISSUE/$ISSUE}"

echo "$SQLITE_ROW" > "$EVIDENCE_DIR/milestone-M1-sqlite.json"

if [ "$SQLITE_ROW" = "null" ] || [ "$SQLITE_ROW" = "" ]; then
    echo "FAIL: no task row found in DB for issue $ISSUE"
    exit 1
fi

if ! echo "$SQLITE_ROW" | grep -q '"status": "done"'; then
    echo "FAIL: task status is not 'done' in DB"
    echo "Row: $SQLITE_ROW"
    exit 1
fi
echo "✓ task status is 'done'"

if ! echo "$SQLITE_ROW" | grep -q "pull/"; then
    echo "FAIL: pr_url missing or invalid in DB"
    echo "Row: $SQLITE_ROW"
    exit 1
fi
echo "✓ pr_url present in DB"
echo ""

# ============================================================================
# STEP 7: ASSERT REPORT FILE EXISTS
# ============================================================================

echo "=== Step 7: Assert Report File Exists ==="
REPORT=$(ls "$STATE_DIR"/reports/*.md 2>/dev/null | head -1)

if [ -z "$REPORT" ]; then
    echo "FAIL: no report file generated in $STATE_DIR/reports/"
    exit 1
fi
echo "✓ report file exists: $REPORT"

cp "$REPORT" "$EVIDENCE_DIR/milestone-M1-report.md"

if ! grep -q "Issue #${ISSUE}" "$REPORT"; then
    echo "FAIL: report does not mention Issue #${ISSUE}"
    exit 1
fi
echo "✓ report mentions Issue #${ISSUE}"
echo ""

# ============================================================================
# STEP 8: ASSERT MAIN BRANCH UNTOUCHED
# ============================================================================

echo "=== Step 8: Assert Main Branch Untouched ==="
if [ -d "$CHECKOUT/.git" ]; then
    ( cd "$CHECKOUT" && git fetch origin main 2>/dev/null ) || true
    AHEAD=$( cd "$CHECKOUT" && git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
    
    if [ "$AHEAD" != "0" ]; then
        echo "FAIL: local main is ahead of origin/main by $AHEAD commit(s)"
        ( cd "$CHECKOUT" && git log origin/main..HEAD --oneline ) \
            | tee "$EVIDENCE_DIR/milestone-M1-main-untouched.log"
        exit 1
    fi
    echo "✓ main branch untouched (0 commits ahead of origin/main)"
else
    echo "⊘ sandbox checkout not found at $CHECKOUT; skipping main check"
fi
echo "" > "$EVIDENCE_DIR/milestone-M1-main-untouched.log"
echo ""

# ============================================================================
# SUCCESS
# ============================================================================

echo "=== M1 ACCEPTANCE PASSED ==="
echo ""
echo "Evidence files:"
echo "  - $EVIDENCE_DIR/milestone-M1-run.log"
echo "  - $EVIDENCE_DIR/milestone-M1-pr.json"
echo "  - $EVIDENCE_DIR/milestone-M1-diff-files.log"
echo "  - $EVIDENCE_DIR/milestone-M1-pytest.log"
echo "  - $EVIDENCE_DIR/milestone-M1-sqlite.json"
echo "  - $EVIDENCE_DIR/milestone-M1-report.md"
echo "  - $EVIDENCE_DIR/milestone-M1-main-untouched.log"
echo ""

# Summary JSON
SUMMARY=$(cat << JSONEOF
{
  "status": "passed",
  "pr_url": "$PR_URL",
  "pr_number": $PR_NUM,
  "issue": $ISSUE,
  "repo": "$REPO",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSONEOF
)
echo "$SUMMARY" > "$EVIDENCE_DIR/milestone-M1-summary.json"

echo "Summary: $SUMMARY"
echo ""
echo "✓ All M1 acceptance criteria passed"
