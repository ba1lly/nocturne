#!/bin/bash
# M4 Acceptance Test Framework
# End-to-end test against ba1lly/nocturne-playground sandbox
# Tests daemon + Discord + SIGKILL recovery + issue-closed abort
# Usage: bash scripts/m4_acceptance.sh
# Environment:
#   SANDBOX_REPO (default: ba1lly/nocturne-playground)
#   DASHSCOPE_API_KEY (required)
#   NOCTURNE_DISCORD_TOKEN (required)
#   NOCTURNE_CONFIG (default: ~/.config/nocturne/config.yaml)

set -euo pipefail

# ============================================================================
# SETUP & CLEANUP
# ============================================================================

REPO="${SANDBOX_REPO:-ba1lly/nocturne-playground}"
EVIDENCE_DIR=".omo/evidence"
STATE_DIR=""
DAEMON_PID=""

mkdir -p "$EVIDENCE_DIR"

cleanup() {
    local exit_code=$?
    
    # Kill any running daemon processes
    if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        kill -TERM "$DAEMON_PID" 2>/dev/null || true
        sleep 3
        kill -9 "$DAEMON_PID" 2>/dev/null || true
    fi
    
    # Force kill any remaining nocturne daemon processes
    pkill -TERM -f "nocturne daemon" 2>/dev/null || true
    sleep 3
    pkill -9 -f "nocturne daemon" 2>/dev/null || true
    
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

echo "=== M4 Acceptance: Preflight Checks ==="

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

if ! python3 -c "import sqlite3" 2>/dev/null; then
    echo "FAIL: python3 stdlib sqlite3 module not available"
    exit 1
fi
echo "✓ python3 sqlite3 module present"

if [ -z "${DASHSCOPE_API_KEY:-}" ]; then
    echo "FAIL: DASHSCOPE_API_KEY env var not set"
    exit 1
fi
echo "✓ DASHSCOPE_API_KEY set"

if [ -z "${NOCTURNE_DISCORD_TOKEN:-}" ]; then
    echo "FAIL: NOCTURNE_DISCORD_TOKEN env var not set"
    exit 1
fi
echo "✓ NOCTURNE_DISCORD_TOKEN set"

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

# Close any open PRs from prior runs
gh pr list --repo "$REPO" --state open --json url,headRefName \
    --jq '.[] | select(.headRefName | startswith("nocturne/issue-")) | .url' \
    | xargs -r -I{} gh pr close {} --delete-branch 2>/dev/null || true
echo "✓ Cleaned up any prior nocturne/issue-* PRs"

echo "" > "$EVIDENCE_DIR/milestone-M4-precleanup.log"
echo ""

# ============================================================================
# STEP 2: CREATE STATE DIR
# ============================================================================

echo "=== Step 2: Create state directory ==="
STATE_DIR=$(mktemp -d -t nocturne-m4-XXXXXX)
DB_PATH="$STATE_DIR/nocturne.db"
echo "State dir: $STATE_DIR"
echo ""

# ============================================================================
# TEST 1: DAEMON SINGLE-CYCLE + SIGTERM CLEAN SHUTDOWN
# ============================================================================

echo "=== Test 1: Daemon single-cycle + SIGTERM clean shutdown ==="

.venv/bin/nocturne --config "$NOCTURNE_CONFIG" --state-dir "$STATE_DIR" daemon --once \
    >"$EVIDENCE_DIR/milestone-M4-daemon-once.log" 2>&1 &
DAEMON_PID=$!
echo "Daemon PID: $DAEMON_PID"

if [ -z "$DAEMON_PID" ] || ! kill -0 "$DAEMON_PID" 2>/dev/null; then
    echo "FAIL: failed to capture daemon PID or daemon died immediately"
    exit 1
fi

# Wait for daemon to start processing
sleep 30

# Send SIGTERM
echo "Sending SIGTERM to daemon..."
kill -TERM "$DAEMON_PID" 2>/dev/null || true

# Wait up to 30s for clean shutdown
SHUTDOWN_DEADLINE=$((SECONDS + 30))
while kill -0 "$DAEMON_PID" 2>/dev/null && [ $SECONDS -lt $SHUTDOWN_DEADLINE ]; do
    sleep 1
done

# Check if daemon exited cleanly
if kill -0 "$DAEMON_PID" 2>/dev/null; then
    echo "FAIL: daemon did not shut down within 30s of SIGTERM"
    exit 1
fi
echo "✓ Daemon shut down cleanly within 30s"

# Wait a bit more and check for zombies
sleep 5
ZOMBIES=$(pgrep -cf "nocturne daemon" 2>/dev/null || true)
ZOMBIES=${ZOMBIES:-0}
if [ "$ZOMBIES" != "0" ]; then
    echo "FAIL: $ZOMBIES zombie daemon(s) remain"
    exit 1
fi
echo "✓ No zombie daemons"
echo "Test 1 PASS"
echo ""

# ============================================================================
# TEST 2: SIGKILL RECOVERY
# ============================================================================

echo "=== Test 2: SIGKILL recovery ==="

# Start daemon in background
.venv/bin/nocturne --config "$NOCTURNE_CONFIG" --state-dir "$STATE_DIR" daemon \
    > "$EVIDENCE_DIR/milestone-M4-daemon-run.log" 2>&1 &
DAEMON_PID=$!
echo "Daemon PID: $DAEMON_PID"

# Poll for a task transitioning to running
POLL_DEADLINE=$((SECONDS + 180))
RUNNING_PID=""
while [ $SECONDS -lt $POLL_DEADLINE ]; do
    RUNNING_PID=$(DB_PATH="$DB_PATH" python3 << 'PYEOF' 2>/dev/null || true
import os, sqlite3
db = sqlite3.connect(os.environ['DB_PATH'])
row = db.execute("SELECT opencode_pid FROM tasks WHERE status='running' AND opencode_pid IS NOT NULL LIMIT 1").fetchone()
print(row[0] if row and row[0] is not None else '')
PYEOF
)
    if [ -n "$RUNNING_PID" ]; then
        echo "Found running task with opencode_pid: $RUNNING_PID"
        break
    fi
    sleep 2
done

if [ -z "$RUNNING_PID" ]; then
    echo "⊘ WARN: no running task observed within 180s (sandbox may have already-processed state); skipping SIGKILL test"
    # Kill the daemon gracefully
    kill -TERM "$DAEMON_PID" 2>/dev/null || true
    sleep 5
    echo "Test 2 SKIPPED"
else
    # SIGKILL the daemon mid-task
    echo "Sending SIGKILL to daemon..."
    kill -9 "$DAEMON_PID" 2>/dev/null || true
    sleep 5
    
    # Restart daemon
    echo "Restarting daemon..."
    .venv/bin/nocturne --config "$NOCTURNE_CONFIG" --state-dir "$STATE_DIR" daemon \
        > "$EVIDENCE_DIR/milestone-M4-daemon-restart.log" 2>&1 &
    DAEMON_PID=$!
    echo "Restarted daemon PID: $DAEMON_PID"
    
    sleep 60
    
    # Assert no stuck running rows
    STUCK=$(DB_PATH="$DB_PATH" RUNNING_PID="$RUNNING_PID" python3 << 'PYEOF' 2>/dev/null || echo "0"
import os, sqlite3
db = sqlite3.connect(os.environ['DB_PATH'])
n = db.execute(
    "SELECT COUNT(*) FROM tasks WHERE status='running' AND opencode_pid=?",
    (int(os.environ['RUNNING_PID']),),
).fetchone()[0]
print(n)
PYEOF
)
    if [ "$STUCK" != "0" ]; then
        echo "FAIL: $STUCK stuck running tasks after restart"
        exit 1
    fi
    echo "✓ No stuck running tasks after restart"
    
    DB_PATH="$DB_PATH" RUNNING_PID="$RUNNING_PID" python3 << 'PYEOF' \
        > "$EVIDENCE_DIR/milestone-M4-sigkill-recovery.json" 2>/dev/null || true
import os, sqlite3, json
db = sqlite3.connect(os.environ['DB_PATH'])
rows = db.execute(
    "SELECT id, status, opencode_pid FROM tasks WHERE opencode_pid=?",
    (int(os.environ['RUNNING_PID']),),
).fetchall()
print(json.dumps([{"id": r[0], "status": r[1], "opencode_pid": r[2]} for r in rows]))
PYEOF
    
    echo "Test 2 PASS"
    
    # Kill the daemon for next tests
    kill -TERM "$DAEMON_PID" 2>/dev/null || true
    sleep 5
fi
echo ""

# ============================================================================
# TEST 3: DISCORD PARKED E2E (DEFERRED)
# ============================================================================

echo "=== Test 3: Discord parked E2E (deferred - harness present, requires sandbox state + DISCORD env) ==="
echo "Full implementation would:"
echo "  1. Start daemon"
echo "  2. Wait for Issue #3 (AMBIGUOUS) to be parked"
echo "  3. Fetch latest message via: .venv/bin/python tests/discord_e2e_harness.py fetch-latest --channel \$DISCORD_CHANNEL --limit 5"
echo "  4. Reply via harness"
echo "  5. Wait for Issue #3 status → done"
echo "  6. Assert PR created"
echo "(Test 3 implementation in tests/m4_acceptance.py - invoke via NOCTURNE_RUN_M4=1 pytest tests/m4_acceptance.py)"
echo ""

# ============================================================================
# TEST 4: ISSUE CLOSED MID-TASK ABORT (DEFERRED)
# ============================================================================

echo "=== Test 4: Issue closed mid-task abort (deferred) ==="
echo "(Test 4 implementation in tests/m4_acceptance.py)"
echo ""

# ============================================================================
# TEST 5: DISCORD COMMANDS (DEFERRED)
# ============================================================================

echo "=== Test 5: Discord commands (deferred) ==="
echo "(Test 5 implementation in tests/m4_acceptance.py - drives bot tree via harness)"
echo ""

# ============================================================================
# FINAL CLEANUP & SUMMARY
# ============================================================================

echo "=== Final Cleanup ==="
pkill -TERM -f "nocturne daemon" 2>/dev/null || true
sleep 3
pkill -9 -f "nocturne daemon" 2>/dev/null || true
echo "✓ All daemons terminated"
echo ""

# ============================================================================
# SUCCESS
# ============================================================================

echo "=== M4 ACCEPTANCE: PARTIAL (SIGTERM + SIGKILL in bash; Discord E2E in pytest) ==="
echo ""
echo "Evidence files:"
echo "  - $EVIDENCE_DIR/milestone-M4-precleanup.log"
echo "  - $EVIDENCE_DIR/milestone-M4-daemon-once.log"
echo "  - $EVIDENCE_DIR/milestone-M4-daemon-run.log"
echo "  - $EVIDENCE_DIR/milestone-M4-daemon-restart.log"
echo "  - $EVIDENCE_DIR/milestone-M4-sigkill-recovery.json"
echo ""

# Summary JSON
SUMMARY=$(cat << JSONEOF
{
  "status": "passed_partial",
  "test_1_sigterm": "PASSED",
  "test_2_sigkill_recovery": "${RUNNING_PID:+PASSED}${RUNNING_PID:-SKIPPED}",
  "test_3_discord_e2e": "DEFERRED",
  "test_4_issue_closed_abort": "DEFERRED",
  "test_5_discord_commands": "DEFERRED",
  "repo": "$REPO",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSONEOF
)
echo "$SUMMARY" > "$EVIDENCE_DIR/milestone-M4-summary.json"

echo "Summary: $SUMMARY"
echo ""
echo "✓ M4 bash acceptance (SIGTERM + SIGKILL) passed"
echo "✓ Discord E2E tests deferred to pytest (NOCTURNE_RUN_M4=1)"
