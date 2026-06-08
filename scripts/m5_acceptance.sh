#!/usr/bin/env bash
# M5 Acceptance Test Framework
# End-to-end test: skill install + setup.sh + reviewer loop + systemd + healthcheck
# Usage: bash scripts/m5_acceptance.sh
# Environment:
#   DASHSCOPE_API_KEY (required)
#   NOCTURNE_DISCORD_TOKEN (required)
#   NOCTURNE_CONFIG (default: ~/.config/nocturne/config.yaml)

set -euo pipefail

# ============================================================================
# SETUP & CLEANUP
# ============================================================================

REPO=""
EVIDENCE_DIR=".omo/evidence"
STATE_DIR=""
DAEMON_PID=""
SYSTEMD_INSTALLED=false
CONFIG="${NOCTURNE_CONFIG:-$HOME/.config/nocturne/config.yaml}"

mkdir -p "$EVIDENCE_DIR"

cleanup() {
    local exit_code=$?
    echo ">>> Cleanup"
    
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
    
    # Stop systemd service if installed
    if $SYSTEMD_INSTALLED; then
        systemctl --user stop nocturne.service 2>/dev/null || true
        bash scripts/uninstall-systemd.sh 2>/dev/null || true
    fi
    
    # Close any open PRs from this test run
    if [ -n "$REPO" ]; then
        gh pr list --repo "$REPO" --search "head:nocturne/" --state open --json url --jq '.[].url' 2>/dev/null \
            | xargs -r -I{} gh pr close {} --delete-branch 2>/dev/null || true
    fi
    
    # Clean up temp state dirs
    if [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ]; then
        rm -rf "$STATE_DIR" 2>/dev/null || true
    fi
    rm -rf /tmp/m5-config-test /tmp/m5-review-state /tmp/m5-stale-test.yaml /tmp/m5-state 2>/dev/null || true
    
    exit $exit_code
}

trap cleanup EXIT

# ============================================================================
# PREFLIGHT CHECKS
# ============================================================================

echo ">>> Preflight"

if ! command -v gh >/dev/null 2>&1; then
    echo "FAIL: gh CLI not found"
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "FAIL: jq not found"
    exit 1
fi

if ! python3 -c "import sqlite3" 2>/dev/null; then
    echo "FAIL: python3 stdlib sqlite3 module not available"
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "FAIL: curl not found"
    exit 1
fi

if [ -z "${DASHSCOPE_API_KEY:-}" ]; then
    echo "FAIL: DASHSCOPE_API_KEY not set"
    exit 1
fi

if [ -z "${NOCTURNE_DISCORD_TOKEN:-}" ]; then
    echo "FAIL: NOCTURNE_DISCORD_TOKEN not set"
    exit 1
fi

if [ ! -f "$CONFIG" ]; then
    echo "FAIL: config not found: $CONFIG"
    exit 1
fi

if [ ! -f ~/.agents/skills/reviewer/SKILL.md ]; then
    echo "FAIL: reviewer skill not at ~/.agents/skills/reviewer/SKILL.md"
    exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
    echo "FAIL: gh not authenticated"
    exit 1
fi

if ! .venv/bin/nocturne version >/dev/null 2>&1; then
    echo "FAIL: nocturne CLI broken"
    exit 1
fi

# Resolve config values
OWNER=$(.venv/bin/python -c "from nocturne.config import load_config; print(load_config('$CONFIG').github.owner)")
CHANNEL_ID=$(.venv/bin/python -c "from nocturne.config import load_config; print(load_config('$CONFIG').discord.channel_id)")
USER_ID=$(.venv/bin/python -c "from nocturne.config import load_config; print(load_config('$CONFIG').discord.mention_user_id)")
SANDBOX_NAME=$(.venv/bin/python -c "from nocturne.config import load_config; print(load_config('$CONFIG').sandbox.repo_name)")
REPO="$OWNER/$SANDBOX_NAME"

if [ -z "$OWNER" ] || [ -z "$CHANNEL_ID" ] || [ "$CHANNEL_ID" = "0" ] || [ -z "$USER_ID" ] || [ "$USER_ID" = "0" ]; then
    echo "FAIL: config has zero/empty owner/channel/user"
    exit 1
fi

echo "✓ Preflight passed (REPO=$REPO)"

# ============================================================================
# RESET SANDBOX BASELINE
# ============================================================================

echo ">>> Resetting sandbox baseline"
bash scripts/bootstrap_sandbox.sh > "$EVIDENCE_DIR/milestone-M5-bootstrap.log" 2>&1 || true

# ============================================================================
# TEST 1: SKILL INSTALL + LIST + FORCE-BACKUP
# ============================================================================

echo ">>> Test 1: Skill install"

# Uninstall first to ensure clean state
.venv/bin/nocturne skill uninstall reviewer --yes 2>/dev/null || true

# Install
.venv/bin/nocturne skill install ~/.agents/skills/reviewer/ 2>&1 | tee "$EVIDENCE_DIR/milestone-M5-skill-install.log"

# Verify listed
.venv/bin/nocturne skill list | grep -q reviewer || { echo "FAIL: reviewer skill not listed"; exit 1; }

# Re-install rejected without --force
DUPE_OUTPUT=$(.venv/bin/nocturne skill install ~/.agents/skills/reviewer/ 2>&1) || true
echo "$DUPE_OUTPUT" | grep -qi "already installed" \
    || { echo "FAIL: re-install not rejected; output was:"; echo "$DUPE_OUTPUT"; exit 1; }

# Force backup
.venv/bin/nocturne skill install ~/.agents/skills/reviewer/ --force 2>&1 | tee -a "$EVIDENCE_DIR/milestone-M5-skill-install.log"

# Verify backup dir created
ls ~/.config/opencode/skills/.backup/reviewer-* 2>/dev/null | head -1 \
    || { echo "FAIL: backup dir not created with --force"; exit 1; }

echo "Test 1 PASS"

# ============================================================================
# TEST 2: SETUP.SH NON-INTERACTIVE
# ============================================================================

echo ">>> Test 2: setup.sh non-interactive"

rm -rf /tmp/m5-config-test
bash scripts/setup.sh --non-interactive --owner "$OWNER" --sandbox-repo "$SANDBOX_NAME" \
    --discord-channel "$CHANNEL_ID" --discord-user "$USER_ID" \
    --api-key-env DASHSCOPE_API_KEY --config-dir /tmp/m5-config-test 2>&1 \
    | tee "$EVIDENCE_DIR/milestone-M5-setup.log"

# Verify config loads
DASHSCOPE_API_KEY=x NOCTURNE_DISCORD_TOKEN=y .venv/bin/python -c \
    "from nocturne.config import load_config; cfg=load_config('/tmp/m5-config-test/config.yaml'); print(cfg.github.owner)" \
    | grep -q "^$OWNER$" || { echo "FAIL: setup.sh produced invalid config"; exit 1; }

echo "Test 2 PASS"

# ============================================================================
# TEST 3: REVIEWER LOOP END-TO-END (DAEMON-DRIVEN)
# ============================================================================

echo ">>> Test 3: Reviewer post-PR loop"

# Reset sandbox
bash scripts/bootstrap_sandbox.sh > /dev/null 2>&1 || true

# Close any prior PRs
gh pr list --repo "$REPO" --state open --json url,headRefName \
    --jq '.[] | select(.headRefName | startswith("nocturne/")) | .url' 2>/dev/null \
    | xargs -r -I{} gh pr close {} --delete-branch 2>/dev/null || true

# Create state dir for this test
mkdir -p /tmp/m5-review-state

# Start daemon
.venv/bin/nocturne --config "$CONFIG" --state-dir /tmp/m5-review-state daemon \
    > "$EVIDENCE_DIR/milestone-M5-daemon-run.log" 2>&1 &
DAEMON_PID=$!

# Wait for healthcheck 200
until curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; do sleep 1; done

# Phase A: wait for Issue #1 to be done with PR
echo "  Phase A: waiting for Issue #1 to be processed..."
PHASE_A_DEADLINE=$((SECONDS + 600))
PR_URL=""
while [ $SECONDS -lt $PHASE_A_DEADLINE ]; do
    PR_URL=$(DB_PATH=/tmp/m5-review-state/nocturne.db python3 << 'PYEOF' 2>/dev/null || true
import os, sqlite3
db = sqlite3.connect(os.environ['DB_PATH'])
row = db.execute("SELECT pr_url FROM tasks WHERE issue_number=1 AND status='done' AND pr_url IS NOT NULL").fetchone()
print(row[0] if row else '')
PYEOF
)
    [ -n "$PR_URL" ] && break
    sleep 10
done

if [ -z "$PR_URL" ]; then
    echo "FAIL: Issue #1 not done after 10min"
    DB_PATH=/tmp/m5-review-state/nocturne.db python3 << 'PYEOF' 2>/dev/null || true
import os, sqlite3
db = sqlite3.connect(os.environ['DB_PATH'])
for row in db.execute("SELECT issue_number, status, pr_url FROM tasks").fetchall():
    print('\t'.join(str(c) if c is not None else 'NULL' for c in row))
PYEOF
    exit 1
fi

PR_NUM=$(echo "$PR_URL" | grep -oE '[0-9]+$')
echo "  Phase A complete: PR_URL=$PR_URL, PR_NUM=$PR_NUM"

# Phase B: wait for review_runs row to complete
echo "  Phase B: waiting for review_runs to complete..."
PHASE_B_DEADLINE=$((SECONDS + 600))
ENDED=""
while [ $SECONDS -lt $PHASE_B_DEADLINE ]; do
    ENDED=$(DB_PATH=/tmp/m5-review-state/nocturne.db PR_URL="$PR_URL" python3 << 'PYEOF' 2>/dev/null || true
import os, sqlite3
db = sqlite3.connect(os.environ['DB_PATH'])
row = db.execute(
    "SELECT ended_at FROM review_runs WHERE pr_url=? ORDER BY started_at DESC LIMIT 1",
    (os.environ['PR_URL'],),
).fetchone()
print(row[0] if row and row[0] is not None else '')
PYEOF
)
    [ -n "$ENDED" ] && break
    sleep 10
done

if [ -z "$ENDED" ]; then
    echo "FAIL: review_runs not completed after 10min"
    DB_PATH=/tmp/m5-review-state/nocturne.db python3 << 'PYEOF' 2>/dev/null || true
import os, sqlite3
db = sqlite3.connect(os.environ['DB_PATH'])
for row in db.execute("SELECT pr_url, started_at, ended_at FROM review_runs").fetchall():
    print('\t'.join(str(c) if c is not None else 'NULL' for c in row))
PYEOF
    exit 1
fi

echo "  Phase B complete: review_runs ended_at=$ENDED"

# Assertion 1: append-only (no force-push)
echo "  Assertion 1: checking commit parents (append-only)..."
gh api "repos/$REPO/pulls/$PR_NUM/commits" --jq '.[].parents | length' | sort -u \
    > "$EVIDENCE_DIR/milestone-M5-review-parents.txt"
grep -q "^1$" "$EVIDENCE_DIR/milestone-M5-review-parents.txt" \
    || { echo "FAIL: not all commits have 1 parent (history rewrite)"; exit 1; }

# Assertion 2: no force-push observed
echo "  Assertion 2: checking for force-push..."
gh api "repos/$REPO/events" --jq '.[] | select(.payload.forced==true)' | head -1 > /tmp/m5-forced
[ ! -s /tmp/m5-forced ] || { echo "FAIL: force-push detected"; exit 1; }

# Assertion 3: review_runs row exists
echo "  Assertion 3: checking review_runs row..."
REVIEW_COUNT=$(DB_PATH=/tmp/m5-review-state/nocturne.db PR_URL="$PR_URL" python3 << 'PYEOF'
import os, sqlite3
db = sqlite3.connect(os.environ['DB_PATH'])
print(db.execute("SELECT COUNT(*) FROM review_runs WHERE pr_url=?", (os.environ['PR_URL'],)).fetchone()[0])
PYEOF
)
[ "$REVIEW_COUNT" -ge 1 ] || { echo "FAIL: no review_runs row"; exit 1; }

# Cleanup daemon
kill -TERM "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
DAEMON_PID=""

# Close PR
gh pr close "$PR_URL" --delete-branch 2>/dev/null || true

echo "Test 3 PASS (review_runs=$REVIEW_COUNT)"

# ============================================================================
# TEST 4: SYSTEMD INSTALL + START + HEALTH
# ============================================================================

echo ">>> Test 4: systemd"

bash scripts/install-systemd.sh --force 2>&1 | tee "$EVIDENCE_DIR/milestone-M5-systemd.log"
SYSTEMD_INSTALLED=true

systemctl --user start nocturne.service
sleep 30

systemctl --user is-active nocturne.service > /tmp/m5-systemd-active
grep -q "^active$" /tmp/m5-systemd-active || { echo "FAIL: systemd not active"; exit 1; }

curl -fsS http://127.0.0.1:8765/health > "$EVIDENCE_DIR/milestone-M5-systemd-health.json"
jq -r '.status' "$EVIDENCE_DIR/milestone-M5-systemd-health.json" | grep -q "^healthy$" \
    || { echo "FAIL: systemd-launched daemon not healthy"; exit 1; }

systemctl --user stop nocturne.service
bash scripts/uninstall-systemd.sh 2>&1 | tee -a "$EVIDENCE_DIR/milestone-M5-systemd.log"
SYSTEMD_INSTALLED=false

echo "Test 4 PASS"

# ============================================================================
# TEST 5: HEALTHCHECK STALE 503
# ============================================================================

echo ">>> Test 5: Healthcheck stale 503"

# Create temp config with aggressive staleness settings
cat > /tmp/m5-stale-test.yaml <<EOF
$(cat "$CONFIG" | sed -e 's/poll_interval_sec:.*/poll_interval_sec: 2/' \
                       -e 's/staleness_factor:.*/staleness_factor: 1/')
EOF

mkdir -p /tmp/m5-state

# Start daemon
.venv/bin/nocturne --config /tmp/m5-stale-test.yaml --state-dir /tmp/m5-state daemon \
    > "$EVIDENCE_DIR/milestone-M5-stale-daemon.log" 2>&1 &
DAEMON_PID=$!

# Wait for healthcheck endpoint
until curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; do sleep 1; done

# Verify healthy first
curl -sS http://127.0.0.1:8765/health | jq -r '.status' | grep -q "^healthy$" \
    || { echo "FAIL: daemon not healthy initially"; exit 1; }

# Pause via separate process
.venv/bin/nocturne --config /tmp/m5-stale-test.yaml --state-dir /tmp/m5-state pause 2>&1 \
    | tee "$EVIDENCE_DIR/milestone-M5-pause-invoke.log" \
    | grep -qi "pause flag set" || { echo "FAIL: pause command output unexpected"; exit 1; }

# Verify flag persisted
DB_PATH=/tmp/m5-state/nocturne.db python3 << 'PYEOF' | grep -q "^1$" \
    || { echo "FAIL: pause flag not persisted"; exit 1; }
import os, sqlite3
db = sqlite3.connect(os.environ['DB_PATH'])
row = db.execute("SELECT value FROM daemon_state WHERE key='paused'").fetchone()
print(row[0] if row else '')
PYEOF

# Wait for staleness threshold
sleep 4

# Check for 503
STATUS=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health)
[ "$STATUS" = "503" ] || { echo "FAIL: expected 503 after pause (got $STATUS)"; exit 1; }
echo "$STATUS" > "$EVIDENCE_DIR/milestone-M5-health-503.log"

# Unpause
.venv/bin/nocturne --config /tmp/m5-stale-test.yaml --state-dir /tmp/m5-state unpause 2>&1

# Wait for recovery
RECOVERY_DEADLINE=$((SECONDS + 15))
S="000"
while [ $SECONDS -lt $RECOVERY_DEADLINE ]; do
    S=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health 2>/dev/null || echo "000")
    [ "$S" = "200" ] && break
    sleep 1
done

[ "$S" = "200" ] || { echo "FAIL: daemon did not recover after unpause (got $S)"; exit 1; }

# Cleanup
kill -TERM "$DAEMON_PID" 2>/dev/null || true
wait "$DAEMON_PID" 2>/dev/null || true
DAEMON_PID=""

echo "Test 5 PASS"

# ============================================================================
# TEST 6: MULTI-PROVIDER VALIDATION
# ============================================================================

echo ">>> Test 6: Multi-provider validation"

cat > /tmp/m5-bad-config.yaml <<EOF
$(sed -e 's|reasoning:.*|reasoning: "openai/gpt-5"|' "$CONFIG")
EOF

PROV_OUTPUT=$(.venv/bin/nocturne --config /tmp/m5-bad-config.yaml run-once --repo "$REPO" --issue 1 2>&1) || true
echo "$PROV_OUTPUT" > "$EVIDENCE_DIR/milestone-M5-multi-provider-error.log"
echo "$PROV_OUTPUT" | grep -qi "openai" \
    || { echo "FAIL: openai missing-provider not mentioned; output was:"; echo "$PROV_OUTPUT"; exit 1; }

echo "Test 6 PASS"

# ============================================================================
# SUMMARY
# ============================================================================

cat > "$EVIDENCE_DIR/milestone-M5-full.log" <<EOF
M5 ACCEPTANCE PASSED
test_1_skill_install: PASSED
test_2_setup_sh: PASSED
test_3_reviewer_loop: PASSED (review_runs=$REVIEW_COUNT, PR=$PR_URL)
test_4_systemd: PASSED
test_5_healthcheck_stale: PASSED (200→503→200)
test_6_multi_provider: PASSED
EOF

echo "M5 ACCEPTANCE: ALL 6 TESTS PASSED"
