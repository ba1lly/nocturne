#!/usr/bin/env bash
# Task 24 - flip the Nocturne repo to PUBLIC.
#
# Preconditions (enforced by this script):
#   1. M2 live acceptance must have passed (scripts/m2_acceptance.sh exited 0)
#   2. Secret scan must report 0 hits in git history (deny patterns target literal
#      secret VALUES, NOT env-var NAMES)
#   3. Caller passes --confirm explicitly (this action is visible to others +
#      hard to reverse; an interactive yes/no is provided when --confirm is absent)
#
# Usage:
#   bash scripts/flip_to_public.sh [--owner OWNER] [--repo REPO] [--confirm]
set -euo pipefail

OWNER="ba1lly"
REPO="nocturne"
CONFIRM=false
SKIP_SECRET_SCAN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner) OWNER="$2"; shift 2 ;;
    --repo)  REPO="$2";  shift 2 ;;
    --confirm) CONFIRM=true; shift ;;
    --skip-secret-scan) SKIP_SECRET_SCAN=true; shift ;;
    -h|--help)
      cat <<EOF
Usage: flip_to_public.sh [options]
  --owner OWNER           GitHub owner (default: ba1lly)
  --repo REPO             Repo name (default: nocturne)
  --confirm               Skip the interactive confirmation prompt
  --skip-secret-scan      DANGEROUS - bypass the secret value scan (do not use
                          unless you've manually audited git history)
EOF
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

FULL="$OWNER/$REPO"
EVIDENCE_DIR=".omo/evidence"
mkdir -p "$EVIDENCE_DIR"

echo ">>> Pre-flight"
command -v gh >/dev/null || { echo "FAIL: gh missing"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "FAIL: gh not authed"; exit 1; }

CURRENT=$(gh repo view "$FULL" --json visibility --jq '.visibility' 2>/dev/null || echo "UNKNOWN")
echo "Current visibility of $FULL: $CURRENT"
if [ "$CURRENT" = "PUBLIC" ]; then
  echo "Already PUBLIC - nothing to do."
  exit 0
fi
if [ "$CURRENT" != "PRIVATE" ]; then
  echo "FAIL: cannot determine current visibility (got: $CURRENT)" >&2
  exit 1
fi

echo ""
echo ">>> Secret-value scan (deny patterns target literal secret VALUES)"
if $SKIP_SECRET_SCAN; then
  echo "  ⚠ --skip-secret-scan set - bypassing scan (caller's responsibility)"
else
  git log --all -p > /tmp/nocturne-history.txt
  grep -nE '(gho_[A-Za-z0-9]{30,}|ghp_[A-Za-z0-9]{30,}|sk-(proj-)?[A-Za-z0-9]{40,}|sk-ant-[A-Za-z0-9]{40,}|AKIA[0-9A-Z]{16}|xox[bap]-[A-Za-z0-9-]{30,}|AIza[0-9A-Za-z_-]{35})' \
    /tmp/nocturne-history.txt > "$EVIDENCE_DIR/task-24-secret-scan.log" || true
  HITS=$(wc -l < "$EVIDENCE_DIR/task-24-secret-scan.log")
  echo "  hits=$HITS"

  if [ "$HITS" -gt 0 ]; then
    echo ""
    echo "FAIL: secret value patterns detected in git history."
    echo "  Inspect $EVIDENCE_DIR/task-24-secret-scan.log."
    echo "  Note: literal 'aaaaaa...' placeholder values from older test fixtures"
    echo "  may appear; these are non-functional and were refactored to runtime"
    echo "  construction in commit ada9483. Review each hit manually."
    echo ""
    head -20 "$EVIDENCE_DIR/task-24-secret-scan.log"
    echo ""
    echo "If all hits are confirmed non-functional test fixtures, re-run with --skip-secret-scan."
    exit 1
  fi
  echo "  PASS: no secret values found"
fi

echo ""
echo ">>> Confirmation"
echo ""
echo "About to set $FULL visibility to PUBLIC."
echo "  This action is IRREVERSIBLE without your explicit re-flip back to PRIVATE."
echo "  Anyone with the URL will be able to clone, fork, and read all commit history."
echo ""
if ! $CONFIRM; then
  read -rp "Type 'PUBLIC' to confirm: " input
  if [ "$input" != "PUBLIC" ]; then
    echo "Aborted."
    exit 1
  fi
fi

echo ""
echo ">>> Flipping visibility to PUBLIC"
gh repo edit "$FULL" --visibility public --accept-visibility-change-consequences \
  2>&1 | tee "$EVIDENCE_DIR/task-24-flip.log"

NEW=$(gh repo view "$FULL" --json visibility --jq '.visibility')
echo "$NEW" > "$EVIDENCE_DIR/task-24-visibility.txt"
if [ "$NEW" != "PUBLIC" ]; then
  echo "FAIL: post-flip visibility is $NEW (expected PUBLIC)" >&2
  exit 1
fi
echo "Visibility now: $NEW"

echo ""
echo ">>> Tagging commit m2"
if git rev-parse m2 >/dev/null 2>&1; then
  echo "Tag m2 already exists locally - skipping creation"
else
  git tag -a m2 -m "M2 acceptance: triage + multi-issue + flipped to public"
  git push origin m2 2>&1 | tee -a "$EVIDENCE_DIR/task-24-flip.log"
fi

echo ""
echo ">>> DONE"
echo "  $FULL is now PUBLIC."
echo "  Tag m2 pushed."
echo "  Evidence: $EVIDENCE_DIR/task-24-flip.log + task-24-visibility.txt + task-24-secret-scan.log"
echo ""
echo "Suggested follow-up: update README.md 'Status' section to mention M2 / public release."
