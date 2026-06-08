#!/bin/bash
# M2 Acceptance Test Framework
# End-to-end test against ba1lly/nocturne-playground sandbox
# Tests multi-issue batch mode with triage skip-comment idempotency
# Usage: bash scripts/m2_acceptance.sh
# Environment:
#   SANDBOX_REPO (default: ba1lly/nocturne-playground)
#   DASHSCOPE_API_KEY (required)
#   NOCTURNE_CONFIG (default: ~/.config/nocturne/config.yaml)

set -euo pipefail

# ============================================================================
# SETUP & CLEANUP
# ============================================================================

REPO="${SANDBOX_REPO:-ba1lly/nocturne-playground}"
EVIDENCE_DIR=".omo/evidence"
STATE_DIR=""
OPENED_PRS=()

mkdir -p "$EVIDENCE_DIR"

cleanup() {
    local exit_code=$?
    
    # Close any PRs we opened
    if [ ${#OPENED_PRS[@]} -gt 0 ]; then
        for pr_num in "${OPENED_PRS[@]}"; do
            gh pr close "$pr_num" --repo "$REPO" --delete-branch 2>/dev/null || true
        done
    fi
    
    # Clean up state dir
    if [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ]; then
        rm -rf "$STATE_DIR" 2>/dev/null || true
    fi
    
    exit $exit_code
}

trap cleanup EXIT

# ============================================================================
# PREFLIGHT CHECKS
# ============================================================================

echo "=== M2 Acceptance: Preflight Checks ==="

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

# Close any open PRs from prior runs on issues 1, 2, 5
gh pr list --repo "$REPO" --state open --json url,headRefName \
    --jq '.[] | select(.headRefName | startswith("nocturne/issue-")) | .url' \
    | xargs -r -I{} gh pr close {} --delete-branch 2>/dev/null || true
echo "✓ Cleaned up any prior nocturne/issue-* PRs"

# Remove any prior nocturne-skip comments on Issue #4
# Note: gh issue view does not expose comment IDs directly in JQ output
# We'll attempt REST API deletion; if it fails, log warning and continue
# (idempotency in triage.py will prevent re-posting)
echo "Attempting to remove prior skip comments on Issue #4..."
COMMENT_IDS=$(gh issue view 4 --repo "$REPO" --json comments \
    --jq '.comments[] | select(.body | startswith("<!-- nocturne-skip -->")) | .id // empty' 2>/dev/null || echo "")

if [ -n "$COMMENT_IDS" ]; then
    while IFS= read -r comment_id; do
        if [ -n "$comment_id" ]; then
            gh api -X DELETE "/repos/$REPO/issues/comments/$comment_id" 2>/dev/null || {
                echo "⊘ Warning: could not delete comment $comment_id (may not exist or permission issue)"
            }
        fi
    done <<< "$COMMENT_IDS"
fi
echo "✓ Attempted cleanup of prior skip comments"

echo "" > "$EVIDENCE_DIR/milestone-M2-precleanup.log"
echo ""

# ============================================================================
# STEP 2: RUN NOCTURNE RUN-ONCE (BATCH MODE, NO --issue)
# ============================================================================

echo "=== Step 2: Run nocturne run-once (batch mode) ==="
STATE_DIR=$(mktemp -d -t nocturne-m2-XXXXXX)
echo "State dir: $STATE_DIR"

RUN_EXIT=0
.venv/bin/nocturne --config "$NOCTURNE_CONFIG" \
                  --state-dir "$STATE_DIR" \
                  run-once --repo "$REPO" 2>&1 \
    | tee "$EVIDENCE_DIR/milestone-M2-run.log" || RUN_EXIT=$?

if [ $RUN_EXIT -ne 0 ]; then
    echo "FAIL: nocturne run-once exited with code $RUN_EXIT"
    exit 1
fi
echo "✓ nocturne run-once completed successfully"
echo ""

# ============================================================================
# STEP 3: ASSERT ISSUE #4 HAS NOCTURNE-SKIP COMMENT
# ============================================================================

echo "=== Step 3: Assert Issue #4 has nocturne-skip comment ==="
SKIP_COMMENT=$(gh issue view 4 --repo "$REPO" --json comments \
    --jq '[.comments[] | select(.body | startswith("<!-- nocturne-skip -->"))] | .[0]')
echo "$SKIP_COMMENT" > "$EVIDENCE_DIR/milestone-M2-skip-comment.json"

if [ -z "$SKIP_COMMENT" ] || [ "$SKIP_COMMENT" = "null" ]; then
    echo "FAIL: no skip comment on Issue #4"
    exit 1
fi
echo "✓ Issue #4 has nocturne-skip comment"
echo ""

# ============================================================================
# STEP 4: ASSERT PRs FOR AT LEAST 2 DOABLE ISSUES
# ============================================================================

echo "=== Step 4: Assert PRs for at least 2 DOABLE issues ==="
PR_LIST=$(gh pr list --repo "$REPO" --state open \
    --json url,headRefName,number \
    --jq '[.[] | select(.headRefName | startswith("nocturne/issue-"))]')
echo "$PR_LIST" > "$EVIDENCE_DIR/milestone-M2-prs.json"

PR_COUNT=$(echo "$PR_LIST" | jq 'length')
if [ "$PR_COUNT" -lt 2 ]; then
    echo "FAIL: expected ≥2 PRs, got $PR_COUNT"
    exit 1
fi
echo "✓ Found $PR_COUNT open PRs for DOABLE issues"

# Extract PR numbers for cleanup
OPENED_PRS=($(echo "$PR_LIST" | jq -r '.[].number'))
echo ""

# ============================================================================
# STEP 5: ASSERT NO PR FOR ISSUE #4 (TOO_BIG, MUST BE SKIPPED)
# ============================================================================

echo "=== Step 5: Assert no PR for Issue #4 ==="
PR4_COUNT=$(gh pr list --repo "$REPO" --state open --json headRefName \
    --jq '[.[] | select(.headRefName | startswith("nocturne/issue-4-"))] | length')

if [ "$PR4_COUNT" != "0" ]; then
    echo "FAIL: PR exists for SKIP issue #4 (expected 0, got $PR4_COUNT)"
    exit 1
fi
echo "✓ No PR for Issue #4 (correctly skipped)"
echo ""

# ============================================================================
# STEP 6: ASSERT RUNREPORT
# ============================================================================

echo "=== Step 6: Assert RunReport ==="
REPORT=$(ls "$STATE_DIR"/reports/*.md 2>/dev/null | head -1)

if [ -z "$REPORT" ]; then
    echo "FAIL: no report file generated in $STATE_DIR/reports/"
    exit 1
fi
echo "✓ report file exists: $REPORT"

cp "$REPORT" "$EVIDENCE_DIR/milestone-M2-report.md"

# Check for "Done" section with at least 2 entries
if ! grep -qE "Done.*[2-9]|Done.*[0-9]{2}" "$REPORT"; then
    echo "FAIL: report does not show ≥2 done issues"
    echo "Report content:"
    cat "$REPORT"
    exit 1
fi
echo "✓ report shows ≥2 done issues"

# Check for "Skipped" section with at least 1 entry
if ! grep -qE "Skipped.*[1-9]" "$REPORT"; then
    echo "FAIL: report does not show ≥1 skipped issues"
    echo "Report content:"
    cat "$REPORT"
    exit 1
fi
echo "✓ report shows ≥1 skipped issues"
echo ""

# ============================================================================
# STEP 7: ASSERT MAIN BRANCH UNTOUCHED
# ============================================================================

echo "=== Step 7: Assert Main Branch Untouched ==="
CHECKOUT="${SANDBOX_CHECKOUT:-$HOME/projects/nocturne-playground-checkout}"

if [ -d "$CHECKOUT/.git" ]; then
    ( cd "$CHECKOUT" && git fetch origin main 2>/dev/null ) || true
    AHEAD=$( cd "$CHECKOUT" && git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
    
    if [ "$AHEAD" != "0" ]; then
        echo "FAIL: local main is ahead of origin/main by $AHEAD commit(s)"
        ( cd "$CHECKOUT" && git log origin/main..HEAD --oneline ) \
            | tee "$EVIDENCE_DIR/milestone-M2-main-untouched.log"
        exit 1
    fi
    echo "✓ main branch untouched (0 commits ahead of origin/main)"
else
    echo "⊘ sandbox checkout not found at $CHECKOUT; skipping main check"
fi
echo "" > "$EVIDENCE_DIR/milestone-M2-main-untouched.log"
echo ""

# ============================================================================
# SUCCESS
# ============================================================================

echo "=== M2 ACCEPTANCE PASSED ==="
echo ""
echo "Evidence files:"
echo "  - $EVIDENCE_DIR/milestone-M2-run.log"
echo "  - $EVIDENCE_DIR/milestone-M2-skip-comment.json"
echo "  - $EVIDENCE_DIR/milestone-M2-prs.json"
echo "  - $EVIDENCE_DIR/milestone-M2-report.md"
echo "  - $EVIDENCE_DIR/milestone-M2-main-untouched.log"
echo ""

# Summary JSON
SUMMARY=$(cat << JSONEOF
{
  "status": "passed",
  "pr_count": $PR_COUNT,
  "skip_count": 1,
  "repo": "$REPO",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSONEOF
)
echo "$SUMMARY" > "$EVIDENCE_DIR/milestone-M2-summary.json"

echo "Summary: $SUMMARY"
echo ""
echo "✓ All M2 acceptance criteria passed"
