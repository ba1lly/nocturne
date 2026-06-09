#!/usr/bin/env bash
# Thin wrapper - delegates to the reliable Python wizard.
# All flags are forwarded; run `nocturne setup --help` for full options.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
NOCTURNE_BIN="$REPO_ROOT/.venv/bin/nocturne"

if [ ! -x "$NOCTURNE_BIN" ]; then
  echo "ERROR: nocturne CLI not built. Run: cd $REPO_ROOT && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

exec "$NOCTURNE_BIN" setup "$@"
