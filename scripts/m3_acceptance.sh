#!/bin/bash
# M3 Acceptance Test Framework
# End-to-end test against ba1lly/nocturne-playground sandbox
# Tests ask/park/resume flow + false-positive sentinel guard
# Usage: bash scripts/m3_acceptance.sh
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

echo "=== M3 Acceptance: Preflight Checks ==="

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

# Close any open PRs from prior runs on issues 3 and 5
gh pr list --repo "$REPO" --state open --json url,headRefName \
    --jq '.[] | select(.headRefName | startswith("nocturne/issue-")) | .url' \
    | xargs -r -I{} gh pr close {} --delete-branch 2>/dev/null || true
echo "✓ Cleaned up any prior nocturne/issue-* PRs"

# Remove any prior nocturne-question or nocturne-skip marker comments from Issues 3 and 5
echo "Attempting to remove prior question/skip comments on Issues 3 and 5..."
for ISSUE_NUM in 3 5; do
    COMMENT_IDS=$(gh issue view "$ISSUE_NUM" --repo "$REPO" --json comments \
        --jq '.comments[] | select(.body | contains("nocturne-question") or contains("##NOCTURNE_NEED_INPUT##") or contains("nocturne-skip")) | .id // empty' 2>/dev/null || echo "")
    
    if [ -n "$COMMENT_IDS" ]; then
        while IFS= read -r comment_id; do
            if [ -n "$comment_id" ]; then
                gh api -X DELETE "/repos/$REPO/issues/comments/$comment_id" 2>/dev/null || {
                    echo "⊘ Warning: could not delete comment $comment_id on Issue #$ISSUE_NUM"
                }
            fi
        done <<< "$COMMENT_IDS"
    fi
done
echo "✓ Attempted cleanup of prior question/skip comments"

echo "" > "$EVIDENCE_DIR/milestone-M3-precleanup.log"
echo ""

# ============================================================================
# STEP 2: RUN NOCTURNE RUN-ONCE (BATCH MODE)
# ============================================================================

echo "=== Step 2: Run nocturne run-once (batch mode) ==="
STATE_DIR=$(mktemp -d -t nocturne-m3-XXXXXX)
echo "State dir: $STATE_DIR"

RUN_EXIT=0
.venv/bin/nocturne --config "${NOCTURNE_CONFIG}" \
                   --state-dir "$STATE_DIR" \
                   run-once --repo "$REPO" 2>&1 \
    | tee "$EVIDENCE_DIR/milestone-M3-run1.log" || RUN_EXIT=$?

if [ $RUN_EXIT -ne 0 ]; then
    echo "FAIL: nocturne run-once exited with code $RUN_EXIT"
    exit 1
fi
echo "✓ nocturne run-once completed successfully"
echo ""

# ============================================================================
# STEP 3: ASSERT ISSUE #3 (AMBIGUOUS) IS PARKED
# ============================================================================

echo "=== Step 3: Assert Issue #3 (AMBIGUOUS) is parked ==="
DB_PATH="$STATE_DIR/nocturne.db"

if [ ! -f "$DB_PATH" ]; then
    echo "FAIL: nocturne.db not found at $DB_PATH"
    exit 1
fi

STATUS_3=$(DB_PATH="$DB_PATH" python3 << 'PYEOF'
import os, sqlite3
try:
    db = sqlite3.connect(os.environ['DB_PATH'])
    row = db.execute("SELECT status FROM tasks WHERE issue_number=3").fetchone()
    print(row[0] if row else 'absent')
except Exception as e:
    print(f"error: {e}")
PYEOF
)

if [ "$STATUS_3" != "parked" ]; then
    echo "FAIL: Issue #3 status is '$STATUS_3', expected 'parked'"
    exit 1
fi
echo "✓ Issue #3 status is 'parked'"
echo ""

# ============================================================================
# STEP 4: ASSERT ISSUE #3 HAS A QUESTION COMMENT
# ============================================================================

echo "=== Step 4: Assert Issue #3 has a question comment ==="
Q_COMMENT=$(gh issue view 3 --repo "$REPO" --json comments \
    --jq '[.comments[] | select(.body | contains("##NOCTURNE_NEED_INPUT##") or contains("nocturne-question") or contains("clarif") or contains("question"))] | length')

if [ "$Q_COMMENT" -lt 1 ]; then
    echo "FAIL: Issue #3 has no question comment"
    exit 1
fi
echo "✓ Issue #3 has $Q_COMMENT question comment(s)"
echo ""

# ============================================================================
# STEP 5: ASSERT ISSUE #5 (FALSE-POSITIVE SENTINEL) NOT PARKED
# ============================================================================

echo "=== Step 5: Assert Issue #5 (literal sentinel) NOT falsely parked ==="
STATUS_5=$(DB_PATH="$DB_PATH" python3 << 'PYEOF'
import os, sqlite3
try:
    db = sqlite3.connect(os.environ['DB_PATH'])
    row = db.execute("SELECT status FROM tasks WHERE issue_number=5").fetchone()
    print(row[0] if row else 'absent')
except Exception as e:
    print(f"error: {e}")
PYEOF
)

case "$STATUS_5" in
    parked)
        echo "FAIL: Issue #5 (literal sentinel) falsely parked"
        exit 1
        ;;
    done|failed|absent)
        echo "✓ Issue #5 not falsely parked (status=$STATUS_5)"
        ;;
    *)
        echo "WARN: Issue #5 unexpected status $STATUS_5"
        ;;
esac
echo "$STATUS_5" > "$EVIDENCE_DIR/milestone-M3-falsepos-issue5.txt"
echo ""

# ============================================================================
# STEP 6: RESUME ISSUE #3 WITH CONCRETE ANSWER
# ============================================================================

echo "=== Step 6: Resume Issue #3 with concrete answer ==="
ANSWER="Add a function median(values) that returns the median of a list of numbers, with tests covering empty list (ValueError), single value, even count, odd count."

RESUME_EXIT=0
.venv/bin/nocturne --config "${NOCTURNE_CONFIG}" \
                   --state-dir "$STATE_DIR" \
                   resume --task-id "${REPO}#3" --answer "$ANSWER" 2>&1 \
    | tee "$EVIDENCE_DIR/milestone-M3-resume.log" || RESUME_EXIT=$?

if [ $RESUME_EXIT -ne 0 ]; then
    echo "FAIL: nocturne resume exited with code $RESUME_EXIT"
    exit 1
fi
echo "✓ nocturne resume completed successfully"
echo ""

# ============================================================================
# STEP 7: RE-RUN NOCTURNE RUN-ONCE TO PROCESS RESUMED TASK
# ============================================================================

echo "=== Step 7: Re-run nocturne run-once to process resumed task ==="
RUN2_EXIT=0
.venv/bin/nocturne --config "${NOCTURNE_CONFIG}" \
                   --state-dir "$STATE_DIR" \
                   run-once --repo "$REPO" 2>&1 \
    | tee "$EVIDENCE_DIR/milestone-M3-run2.log" || RUN2_EXIT=$?

if [ $RUN2_EXIT -ne 0 ]; then
    echo "FAIL: nocturne run-once (2nd) exited with code $RUN2_EXIT"
    exit 1
fi
echo "✓ nocturne run-once (2nd) completed successfully"
echo ""

# ============================================================================
# STEP 8: POLL UP TO 120S FOR ISSUE #3 STATUS → DONE
# ============================================================================

echo "=== Step 8: Poll up to 120s for Issue #3 status → done ==="
DEADLINE=$((SECONDS + 120))
STATUS=""

while [ $SECONDS -lt $DEADLINE ]; do
    STATUS=$(DB_PATH="$DB_PATH" python3 << 'PYEOF'
import os, sqlite3
try:
    db = sqlite3.connect(os.environ['DB_PATH'])
    row = db.execute("SELECT status FROM tasks WHERE issue_number=3").fetchone()
    print(row[0] if row else 'absent')
except Exception as e:
    print(f"error: {e}")
PYEOF
)
    
    if [ "$STATUS" = "done" ]; then
        echo "✓ Issue #3 status transitioned to 'done'"
        break
    fi
    
    if [ "$STATUS" = "failed" ]; then
        echo "FAIL: Issue #3 went to failed after resume"
        exit 1
    fi
    
    echo "  (polling... status=$STATUS, elapsed=$((SECONDS - (DEADLINE - 120)))s)"
    sleep 5
done

if [ "$STATUS" != "done" ]; then
    echo "FAIL: Issue #3 still not done after 120s (status=$STATUS)"
    exit 1
fi
echo ""

# ============================================================================
# STEP 9: ASSERT PR CREATED FOR ISSUE #3
# ============================================================================

echo "=== Step 9: Assert PR created for Issue #3 ==="
PR_COUNT=$(gh pr list --repo "$REPO" --state open --json headRefName \
    --jq '[.[] | select(.headRefName | startswith("nocturne/issue-3-"))] | length')

if [ "$PR_COUNT" -lt 1 ]; then
    echo "FAIL: no PR for Issue #3 after resume"
    exit 1
fi
echo "✓ Found $PR_COUNT PR(s) for Issue #3"

gh pr list --repo "$REPO" --state open --json url,headRefName,body \
    --jq '[.[] | select(.headRefName | startswith("nocturne/issue-3-"))]' \
    > "$EVIDENCE_DIR/milestone-M3-resume.json"

OPENED_PRS=($(gh pr list --repo "$REPO" --state open --json number,headRefName \
    --jq '.[] | select(.headRefName | startswith("nocturne/issue-3-")) | .number'))
echo ""

# ============================================================================
# STEP 10: ASSERT NO DUPLICATE QUESTION COMMENTS ON ISSUE #3
# ============================================================================

echo "=== Step 10: Assert no duplicate question comments on Issue #3 ==="
DUP_COUNT=$(gh issue view 3 --repo "$REPO" --json comments \
    --jq '[.comments[] | select(.body | contains("nocturne-question") or contains("##NOCTURNE_NEED_INPUT##"))] | length')

if [ "$DUP_COUNT" -gt 1 ]; then
    echo "FAIL: duplicate question comments ($DUP_COUNT found, expected ≤1)"
    exit 1
fi
echo "✓ No duplicate question comments (count=$DUP_COUNT)"
echo "$DUP_COUNT" > "$EVIDENCE_DIR/milestone-M3-no-duplicates.txt"
echo ""

# ============================================================================
# SUCCESS
# ============================================================================

echo "=== M3 ACCEPTANCE PASSED ==="
echo ""
echo "Evidence files:"
echo "  - $EVIDENCE_DIR/milestone-M3-run1.log"
echo "  - $EVIDENCE_DIR/milestone-M3-run2.log"
echo "  - $EVIDENCE_DIR/milestone-M3-resume.log"
echo "  - $EVIDENCE_DIR/milestone-M3-resume.json"
echo "  - $EVIDENCE_DIR/milestone-M3-falsepos-issue5.txt"
echo "  - $EVIDENCE_DIR/milestone-M3-no-duplicates.txt"
echo ""

# Summary JSON
SUMMARY=$(cat << JSONEOF
{
  "status": "passed",
  "issue_3_status": "done",
  "issue_5_status": "$STATUS_5",
  "pr_count": $PR_COUNT,
  "duplicate_comments": $DUP_COUNT,
  "repo": "$REPO",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSONEOF
)
echo "$SUMMARY" > "$EVIDENCE_DIR/milestone-M3-summary.json"

echo "Summary: $SUMMARY"
echo ""
echo "✓ All M3 acceptance criteria passed"
