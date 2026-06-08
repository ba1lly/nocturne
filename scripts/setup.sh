#!/usr/bin/env bash
# Nocturne interactive setup — writes ~/.config/nocturne/config.yaml
set -euo pipefail

# Defaults
OWNER=""
SANDBOX_REPO="nocturne-playground"
DISCORD_CHANNEL="0"
DISCORD_USER="0"
API_KEY_ENV="DASHSCOPE_API_KEY"
CONFIG_DIR="$HOME/.config/nocturne"
NON_INTERACTIVE=false
FORCE=false
INSTALL_REVIEWER=false

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    --force) FORCE=true; shift ;;
    --owner) OWNER="$2"; shift 2 ;;
    --sandbox-repo) SANDBOX_REPO="$2"; shift 2 ;;
    --discord-channel) DISCORD_CHANNEL="$2"; shift 2 ;;
    --discord-user) DISCORD_USER="$2"; shift 2 ;;
    --api-key-env) API_KEY_ENV="$2"; shift 2 ;;
    --config-dir) CONFIG_DIR="$2"; shift 2 ;;
    -h|--help)
      cat <<EOF
Usage: setup.sh [options]
  --non-interactive       Don't prompt; use provided/default values
  --force                 Overwrite existing config
  --owner OWNER           GitHub owner (required)
  --sandbox-repo NAME     Sandbox repo name (default: nocturne-playground)
  --discord-channel ID    Discord channel ID (required for daemon)
  --discord-user ID       Discord mention user ID (required for daemon)
  --api-key-env NAME      Env var name holding the provider API key (default: DASHSCOPE_API_KEY)
  --config-dir PATH       Config dir (default: ~/.config/nocturne)
EOF
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

# Interactive prompts (only if not --non-interactive)
if ! $NON_INTERACTIVE; then
  echo "=== Nocturne Setup ==="
  if [ -z "$OWNER" ]; then
    read -rp "GitHub owner: " OWNER
  fi
  read -rp "Sandbox repo name [$SANDBOX_REPO]: " input
  SANDBOX_REPO="${input:-$SANDBOX_REPO}"
  read -rp "Discord channel ID (or 0 to skip) [$DISCORD_CHANNEL]: " input
  DISCORD_CHANNEL="${input:-$DISCORD_CHANNEL}"
  read -rp "Discord mention user ID (or 0 to skip) [$DISCORD_USER]: " input
  DISCORD_USER="${input:-$DISCORD_USER}"
  read -rp "Provider API key env var name [$API_KEY_ENV]: " input
  API_KEY_ENV="${input:-$API_KEY_ENV}"
  
  read -rp "Install reviewer skill from ~/.agents/skills/reviewer/? [y/N]: " input
  if [[ "$input" =~ ^[Yy]$ ]]; then
    INSTALL_REVIEWER=true
  fi
fi

# Validate
if [ -z "$OWNER" ]; then
  echo "ERROR: --owner is required" >&2
  exit 2
fi

# Create config dir
mkdir -p "$CONFIG_DIR"
CONFIG_FILE="$CONFIG_DIR/config.yaml"

# Check for existing config
if [ -f "$CONFIG_FILE" ] && ! $FORCE; then
  echo "ERROR: $CONFIG_FILE already exists (pass --force to overwrite)" >&2
  exit 2
fi

# Substitute into config.example.yaml using sed
REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
EXAMPLE="$REPO_ROOT/config.example.yaml"

if [ ! -f "$EXAMPLE" ]; then
  echo "ERROR: config.example.yaml not found at $EXAMPLE" >&2
  exit 2
fi

sed \
  -e "s|owner: \"ba1lly\"|owner: \"$OWNER\"|" \
  -e "s|repo_name: \"nocturne-playground\"|repo_name: \"$SANDBOX_REPO\"|" \
  -e "s|channel_id: 0|channel_id: $DISCORD_CHANNEL|" \
  -e "s|mention_user_id: 0|mention_user_id: $DISCORD_USER|" \
  -e "s|api_key_env: \"DASHSCOPE_API_KEY\"|api_key_env: \"$API_KEY_ENV\"|" \
  -e "s|ba1lly/nocturne-playground|$OWNER/$SANDBOX_REPO|g" \
  "$EXAMPLE" > "$CONFIG_FILE"

# Install reviewer skill if requested
if $INSTALL_REVIEWER; then
  SKILL_PATH="$HOME/.agents/skills/reviewer"
  if [ -d "$SKILL_PATH" ]; then
    echo "Installing reviewer skill..."
    nocturne skill install "$SKILL_PATH"
  else
    echo "Warning: Reviewer skill not found at $SKILL_PATH. Skipping installation."
  fi
fi

# Validate env vars
WARNINGS=()
if [ -z "${!API_KEY_ENV:-}" ]; then
  WARNINGS+=("$API_KEY_ENV not set in environment")
fi
if [ -z "${NOCTURNE_DISCORD_TOKEN:-}" ]; then
  WARNINGS+=("NOCTURNE_DISCORD_TOKEN not set in environment")
fi

# Summary
echo ""
echo "=== Setup Complete ==="
echo "Config written to: $CONFIG_FILE"
echo ""
echo "Settings:"
echo "  GitHub owner: $OWNER"
echo "  Sandbox repo: $OWNER/$SANDBOX_REPO"
echo "  Discord channel: $DISCORD_CHANNEL"
echo "  Discord user: $DISCORD_USER"
echo "  API key env: $API_KEY_ENV"
echo ""
if [ ${#WARNINGS[@]} -gt 0 ]; then
  echo "Warnings:"
  for w in "${WARNINGS[@]}"; do
    echo "  ⚠ $w"
  done
  echo ""
fi
echo "Next steps:"
echo "  1. Set required env vars (see warnings above)"
echo "  2. Bootstrap sandbox: GITHUB_OWNER=$OWNER SANDBOX_REPO=$SANDBOX_REPO bash scripts/bootstrap_sandbox.sh"
echo "  3. First run: nocturne run-once --repo $OWNER/$SANDBOX_REPO --issue 1"
echo "  4. Continuous: nocturne daemon"
