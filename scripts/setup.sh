#!/usr/bin/env bash
# Nocturne interactive setup — writes ~/.config/nocturne/config.yaml
#
# Note: this script writes ENV VAR NAMES (not values) into config.yaml.
# The actual API keys and Discord bot token MUST be exported in your shell
# (or stored in ~/.config/nocturne/env for systemd, prompted optionally below).
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
NOCTURNE_BIN="$REPO_ROOT/.venv/bin/nocturne"

# -- Defaults --
OWNER=""
SANDBOX_REPO="nocturne-playground"
DISCORD_CHANNEL="0"
DISCORD_USER="0"
PROVIDER="alibaba-coding-plan"
API_KEY_ENV=""        # auto-set per provider
REASONING_MODEL=""
CODING_MODEL=""
REPORT_MODEL=""
CONFIG_DIR="$HOME/.config/nocturne"
NON_INTERACTIVE=false
FORCE=false
INSTALL_REVIEWER=false
WRITE_ENV_FILE=false   # writes ~/.config/nocturne/env (KEY=VALUE pairs for systemd)

# -- Provider catalog (name|base_url|api_key_env|reasoning|coding|report) --
declare -A PROVIDER_BASE_URL=(
  [alibaba-coding-plan]="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
  [anthropic]="https://api.anthropic.com/v1"
  [openai]="https://api.openai.com/v1"
  [kimi]="https://api.moonshot.cn/v1"
  [glm]="https://open.bigmodel.cn/api/paas/v4"
)
declare -A PROVIDER_ENV=(
  [alibaba-coding-plan]="DASHSCOPE_API_KEY"
  [anthropic]="ANTHROPIC_API_KEY"
  [openai]="OPENAI_API_KEY"
  [kimi]="MOONSHOT_API_KEY"
  [glm]="ZHIPUAI_API_KEY"
)
declare -A PROVIDER_REASONING=(
  [alibaba-coding-plan]="qwen3.6-plus"
  [anthropic]="claude-opus-4-5"
  [openai]="gpt-5"
  [kimi]="moonshot-v1-32k"
  [glm]="glm-4.6"
)
declare -A PROVIDER_CODING=(
  [alibaba-coding-plan]="qwen3-coder-plus"
  [anthropic]="claude-sonnet-4-5"
  [openai]="gpt-5"
  [kimi]="moonshot-v1-32k"
  [glm]="glm-4.6"
)

PROVIDER_ORDER=(alibaba-coding-plan anthropic openai kimi glm)

# -- Parse args --
while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    --force) FORCE=true; shift ;;
    --owner) OWNER="$2"; shift 2 ;;
    --sandbox-repo) SANDBOX_REPO="$2"; shift 2 ;;
    --discord-channel) DISCORD_CHANNEL="$2"; shift 2 ;;
    --discord-user) DISCORD_USER="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --api-key-env) API_KEY_ENV="$2"; shift 2 ;;
    --reasoning-model) REASONING_MODEL="$2"; shift 2 ;;
    --coding-model) CODING_MODEL="$2"; shift 2 ;;
    --report-model) REPORT_MODEL="$2"; shift 2 ;;
    --config-dir) CONFIG_DIR="$2"; shift 2 ;;
    --install-reviewer) INSTALL_REVIEWER=true; shift ;;
    --write-env-file) WRITE_ENV_FILE=true; shift ;;
    -h|--help)
      cat <<EOF
Usage: setup.sh [options]

  --non-interactive       Don't prompt; use provided/default values
  --force                 Overwrite existing config
  --owner OWNER           GitHub owner (required)
  --sandbox-repo NAME     Sandbox repo name (default: nocturne-playground)
  --provider NAME         One of: alibaba-coding-plan | anthropic | openai | kimi | glm
  --api-key-env NAME      Override the env var name (default depends on provider)
  --reasoning-model X     Override the reasoning model (default depends on provider)
  --coding-model X        Override the coding model (default depends on provider)
  --report-model X        Override the report model (default: same as reasoning)
  --discord-channel ID    Discord channel ID (required for Discord HITL)
  --discord-user ID       Discord mention user ID (required for Discord HITL)
  --install-reviewer      Install ~/.agents/skills/reviewer/ after writing config
  --write-env-file        Prompt to write ~/.config/nocturne/env (for systemd)
  --config-dir PATH       Config dir (default: ~/.config/nocturne)

This script writes ENV VAR NAMES (e.g. "DASHSCOPE_API_KEY") to config.yaml.
The actual TOKENS / KEYS must be exported in your shell, or stored in
~/.config/nocturne/env (KEY=VALUE pairs, used by the systemd unit).
EOF
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# -- Interactive prompts --
if ! $NON_INTERACTIVE; then
  echo ""
  echo "================================================================"
  echo "  Nocturne Setup"
  echo "================================================================"
  echo ""
  echo "This wizard will generate ~/.config/nocturne/config.yaml."
  echo "Note: API keys + Discord token live in your SHELL ENV, NOT this file."
  echo ""

  CONFIRMED=false
  while ! $CONFIRMED; do
    API_KEY_VALUE=""
    DISCORD_TOKEN_VALUE=""

    echo "[1/6] GitHub repository"
    read -erp "  owner (your username or org) [${OWNER:-}]: " input
    OWNER="${input:-$OWNER}"
    while [ -z "$OWNER" ]; do
      read -erp "  → required. owner: " OWNER
    done
    read -erp "  sandbox repo name [$SANDBOX_REPO]: " input
    SANDBOX_REPO="${input:-$SANDBOX_REPO}"

    echo ""
    echo "[2/6] LLM provider"
    i=1
    for p in "${PROVIDER_ORDER[@]}"; do
      echo "  $i) $p  →  env: ${PROVIDER_ENV[$p]}, reasoning: ${PROVIDER_REASONING[$p]}, coding: ${PROVIDER_CODING[$p]}"
      i=$((i+1))
    done
    while true; do
      read -erp "  pick [1=$PROVIDER, or name]: " input
      input="${input:-1}"
      if [[ "$input" =~ ^[0-9]+$ ]]; then
        idx=$((input-1))
        if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#PROVIDER_ORDER[@]}" ]; then
          PROVIDER="${PROVIDER_ORDER[$idx]}"
          break
        fi
      elif [ -n "${PROVIDER_BASE_URL[$input]:-}" ]; then
        PROVIDER="$input"
        break
      fi
      echo "  invalid: '$input'. valid: ${PROVIDER_ORDER[*]}"
    done

    API_KEY_ENV="${PROVIDER_ENV[$PROVIDER]}"

    echo ""
    echo "[3/6] Models for $PROVIDER (Enter to accept defaults)"
    read -erp "  reasoning [${PROVIDER_REASONING[$PROVIDER]}]: " input
    REASONING_MODEL="${input:-${PROVIDER_REASONING[$PROVIDER]}}"
    read -erp "  coding    [${PROVIDER_CODING[$PROVIDER]}]: " input
    CODING_MODEL="${input:-${PROVIDER_CODING[$PROVIDER]}}"
    read -erp "  report    [$REASONING_MODEL]: " input
    REPORT_MODEL="${input:-$REASONING_MODEL}"

    # Best-effort opencode catalog check — warn-only, never blocks the flow.
    if command -v opencode >/dev/null 2>&1; then
      AVAILABLE=$(opencode models 2>/dev/null || true)
      for MODEL in "$PROVIDER/$REASONING_MODEL" "$PROVIDER/$CODING_MODEL" "$PROVIDER/$REPORT_MODEL"; do
        if [ -n "$AVAILABLE" ] && ! grep -Fxq "$MODEL" <<< "$AVAILABLE"; then
          echo "  ⚠ '$MODEL' not found in 'opencode models' output — verify with: bash scripts/check_opencode_provider.sh"
        fi
      done
    fi

    echo ""
    echo "[4/6] Discord HITL (parked-task notifications + slash commands)"
    echo "  Set channel ID + user ID to 0 to skip Discord entirely."
    read -erp "  channel ID (0 to skip) [$DISCORD_CHANNEL]: " input
    DISCORD_CHANNEL="${input:-$DISCORD_CHANNEL}"
    read -erp "  mention user ID (0 to skip) [$DISCORD_USER]: " input
    DISCORD_USER="${input:-$DISCORD_USER}"

    echo ""
    echo "[5/6] Secret tokens — input hidden; press Enter to skip and export in shell later."
    echo "      Values stored in ~/.config/nocturne/env (mode 600) for systemd + shell sourcing."
    echo ""
    if [ -n "${!API_KEY_ENV:-}" ]; then
      echo "  $API_KEY_ENV already set in your current shell."
      read -erp "  → reuse it in env file? [Y/n]: " input
      [[ ! "$input" =~ ^[Nn]$ ]] && API_KEY_VALUE="${!API_KEY_ENV}"
    else
      read -srp "  $API_KEY_ENV (input hidden, Enter to skip): " API_KEY_VALUE; echo
    fi
    if [ "$DISCORD_CHANNEL" != "0" ] && [ "$DISCORD_USER" != "0" ]; then
      if [ -n "${NOCTURNE_DISCORD_TOKEN:-}" ]; then
        echo "  NOCTURNE_DISCORD_TOKEN already set in your current shell."
        read -erp "  → reuse it in env file? [Y/n]: " input
        [[ ! "$input" =~ ^[Nn]$ ]] && DISCORD_TOKEN_VALUE="$NOCTURNE_DISCORD_TOKEN"
      else
        read -srp "  NOCTURNE_DISCORD_TOKEN (input hidden, Enter to skip): " DISCORD_TOKEN_VALUE; echo
      fi
    fi
    if [ -n "${API_KEY_VALUE:-}" ] || [ -n "${DISCORD_TOKEN_VALUE:-}" ]; then
      WRITE_ENV_FILE=true
    else
      WRITE_ENV_FILE=false
    fi

    echo ""
    echo "[6/6] Reviewer skill"
    INSTALL_REVIEWER=false
    if [ -d "$HOME/.agents/skills/reviewer" ]; then
      read -erp "  install from ~/.agents/skills/reviewer/? [y/N]: " input
      [[ "$input" =~ ^[Yy]$ ]] && INSTALL_REVIEWER=true
    else
      echo "  (skipped — ~/.agents/skills/reviewer/ not found)"
    fi

    echo ""
    echo "─────────────────────────────────────────────────────────"
    echo "  Review your choices"
    echo "─────────────────────────────────────────────────────────"
    echo "  GitHub owner:    $OWNER"
    echo "  Sandbox repo:    $OWNER/$SANDBOX_REPO"
    echo "  Provider:        $PROVIDER"
    echo "  Reasoning model: $PROVIDER/$REASONING_MODEL"
    echo "  Coding model:    $PROVIDER/$CODING_MODEL"
    echo "  Report model:    $PROVIDER/$REPORT_MODEL"
    echo "  Discord channel: $DISCORD_CHANNEL"
    echo "  Discord user:    $DISCORD_USER"
    echo "  API key value:   $([ -n "${API_KEY_VALUE:-}" ] && echo 'provided (will write env file)' || echo 'skipped (export $API_KEY_ENV in shell later)')"
    echo "  Discord token:   $([ -n "${DISCORD_TOKEN_VALUE:-}" ] && echo 'provided (will write env file)' || echo 'skipped')"
    echo "  Reviewer skill:  $($INSTALL_REVIEWER && echo 'will install' || echo 'skipped')"
    echo ""
    read -erp "Proceed and write config? [Y/n/edit]: " input
    case "$input" in
      ""|[Yy]*) CONFIRMED=true ;;
      [Ee]*)    echo ""; echo "─── re-entering wizard ───"; echo "" ;;
      [Nn]*)    echo "Aborted."; exit 1 ;;
      *)        echo "Unrecognized — type Y to proceed, N to abort, edit to redo."; ;;
    esac
  done
fi

# -- Resolve API_KEY_ENV from provider if not explicitly set --
if [ -z "$API_KEY_ENV" ]; then
  API_KEY_ENV="${PROVIDER_ENV[$PROVIDER]:-DASHSCOPE_API_KEY}"
fi
if [ -z "$REASONING_MODEL" ]; then
  REASONING_MODEL="${PROVIDER_REASONING[$PROVIDER]:-qwen3.6-plus}"
fi
if [ -z "$CODING_MODEL" ]; then
  CODING_MODEL="${PROVIDER_CODING[$PROVIDER]:-qwen3-coder-plus}"
fi
if [ -z "$REPORT_MODEL" ]; then
  REPORT_MODEL="$REASONING_MODEL"
fi

# -- Validate --
if [ -z "$OWNER" ]; then
  echo "ERROR: --owner is required" >&2
  exit 2
fi
if [ -z "${PROVIDER_BASE_URL[$PROVIDER]:-}" ]; then
  echo "ERROR: unknown provider: $PROVIDER" >&2
  echo "Valid: ${PROVIDER_ORDER[*]}" >&2
  exit 2
fi
if [[ ! "$API_KEY_ENV" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
  echo "ERROR: --api-key-env must be an ENV VAR NAME like DASHSCOPE_API_KEY," >&2
  echo "       not an actual key value. Got: $API_KEY_ENV" >&2
  exit 2
fi

# -- Create config dir --
mkdir -p "$CONFIG_DIR"
CONFIG_FILE="$CONFIG_DIR/config.yaml"

if [ -f "$CONFIG_FILE" ]; then
  # Detect leaked api_key_env from a previous bad run (secret value instead of env name).
  EXISTING_BAD=$(grep -E '^\s+api_key_env:' "$CONFIG_FILE" 2>/dev/null \
    | grep -vE 'api_key_env: "[A-Z_][A-Z0-9_]*"' || true)
  if [ -n "$EXISTING_BAD" ]; then
    echo "" >&2
    echo "⚠️  SECURITY WARNING — existing config has a suspicious api_key_env value:" >&2
    echo "$EXISTING_BAD" >&2
    echo "    This may be an actual API key. If so, rotate it before continuing." >&2
    echo "" >&2
  fi

  if $FORCE; then
    cp -p "$CONFIG_FILE" "$CONFIG_FILE.bak.$(date +%Y%m%d-%H%M%S)"
    echo "  → backed up existing config to ${CONFIG_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
  elif $NON_INTERACTIVE || [ ! -t 0 ]; then
    echo "ERROR: $CONFIG_FILE already exists. Refusing to overwrite in non-interactive mode." >&2
    echo "       Rerun with --force to overwrite (a timestamped .bak will be created)." >&2
    exit 2
  else
    echo ""
    echo "Config already exists: $CONFIG_FILE"
    read -rp "Overwrite (a .bak will be created)? [y/N]: " input
    if [[ ! "$input" =~ ^[Yy]$ ]]; then
      echo "Aborted. To force overwrite later, run: bash $0 --force"
      exit 1
    fi
    cp -p "$CONFIG_FILE" "$CONFIG_FILE.bak.$(date +%Y%m%d-%H%M%S)"
    echo "  → backed up existing config"
    FORCE=true
  fi
fi

# Discord enabled flag — false when both IDs are 0 (loader will then skip the channel/user validators).
DISCORD_ENABLED=true
if [ "$DISCORD_CHANNEL" = "0" ] && [ "$DISCORD_USER" = "0" ]; then
  DISCORD_ENABLED=false
fi

PROVIDER_BASE_URL_VAL="${PROVIDER_BASE_URL[$PROVIDER]}"

# -- Write config.yaml from scratch (cleaner than sed on the multi-provider example) --
cat > "$CONFIG_FILE" <<EOF
# Nocturne configuration — generated by scripts/setup.sh
github:
  owner: "$OWNER"

sandbox:
  repo_name: "$SANDBOX_REPO"
  checkout_path: "~/projects/${SANDBOX_REPO}-checkout"

providers:
  $PROVIDER:
    base_url: "$PROVIDER_BASE_URL_VAL"
    api_key_env: "$API_KEY_ENV"

models:
  reasoning: "$PROVIDER/$REASONING_MODEL"
  report: "$PROVIDER/$REPORT_MODEL"
  coding: "$PROVIDER/$CODING_MODEL"

opencode:
  command: "opencode"
  timeout_min: 25
  worktree_root: "/tmp/nocturne"

repos:
  - slug: "$OWNER/$SANDBOX_REPO"
    checkout_path: "~/projects/${SANDBOX_REPO}-checkout"
    label: "agent"
    base: "main"
    verify_cmd: "pytest -q"
    require_new_test: true

guardrails:
  max_attempts: 3
  per_task_timeout_min: 25
  global_wallclock_hours: 8
  token_budget: 2_000_000
  allow_force_push: false
  allow_auto_merge: false

discord:
  enabled: $DISCORD_ENABLED
  bot_token_env: "NOCTURNE_DISCORD_TOKEN"
  channel_id: $DISCORD_CHANNEL
  mention_user_id: $DISCORD_USER

daemon:
  poll_interval_sec: 300
  quiet_hours: []

review:
  enabled: true
  budget_attempts: 2
  severity_floor: "info"
  skill_name: "reviewer"
  append_only: true

healthcheck:
  enabled: true
  bind_host: "127.0.0.1"
  bind_port: 8765
  staleness_factor: 2

persona:
  enabled: true
  soul_path: "~/.config/nocturne/soul.md"
EOF

chmod 600 "$CONFIG_FILE"

# -- Install reviewer skill if requested --
if $INSTALL_REVIEWER; then
  SKILL_PATH="$HOME/.agents/skills/reviewer"
  if [ -d "$SKILL_PATH" ]; then
    echo ""
    echo "Installing reviewer skill..."
    if [ -x "$NOCTURNE_BIN" ]; then
      "$NOCTURNE_BIN" skill install "$SKILL_PATH" || \
        echo "  (skill install failed — install manually with: $NOCTURNE_BIN skill install $SKILL_PATH)"
    else
      echo "  ⚠ $NOCTURNE_BIN not found — install nocturne first (pip install -e .[dev])"
      echo "    then run: $NOCTURNE_BIN skill install $SKILL_PATH"
    fi
  else
    echo "  ⚠ Reviewer skill not at $SKILL_PATH — skipping"
  fi
fi

# -- Write ~/.config/nocturne/env (for systemd + shell sourcing) --
# Merge with existing entries instead of clobbering, so e.g. the user can run setup
# twice — once to set the API key, once to set the Discord token — without losing either.
ENV_FILE="$CONFIG_DIR/env"
if $WRITE_ENV_FILE; then
  TMP_ENV=$(mktemp)
  if [ -f "$ENV_FILE" ]; then
    cp -p "$ENV_FILE" "${ENV_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
    grep -vE "^(${API_KEY_ENV}|NOCTURNE_DISCORD_TOKEN)=" "$ENV_FILE" > "$TMP_ENV" || true
  fi
  [ -n "${API_KEY_VALUE:-}" ]      && printf '%s=%s\n' "$API_KEY_ENV" "$API_KEY_VALUE" >> "$TMP_ENV"
  [ -n "${DISCORD_TOKEN_VALUE:-}" ] && printf 'NOCTURNE_DISCORD_TOKEN=%s\n' "$DISCORD_TOKEN_VALUE" >> "$TMP_ENV"
  install -m 600 "$TMP_ENV" "$ENV_FILE"
  rm -f "$TMP_ENV"
  echo ""
  echo "  → wrote $ENV_FILE (mode 600)"
  echo "    ⚠️  Contains secret token values. Do NOT commit, paste, or share this file."
  echo "    To load in your current shell:  set -a; source $ENV_FILE; set +a"
fi

# -- Validate env vars in current shell --
WARNINGS=()
if [ -z "${!API_KEY_ENV:-}" ]; then
  WARNINGS+=("$API_KEY_ENV not set in environment — export it in your shell")
fi
if [ "$DISCORD_CHANNEL" != "0" ] && [ -z "${NOCTURNE_DISCORD_TOKEN:-}" ]; then
  WARNINGS+=("NOCTURNE_DISCORD_TOKEN not set — Discord won't connect until you export it")
fi

# -- Summary --
echo ""
echo "================================================================"
echo "  Setup Complete"
echo "================================================================"
echo ""
echo "Config: $CONFIG_FILE"
echo ""
echo "  GitHub owner:    $OWNER"
echo "  Sandbox repo:    $OWNER/$SANDBOX_REPO"
echo "  Provider:        $PROVIDER  (env: $API_KEY_ENV)"
echo "  Reasoning model: $PROVIDER/$REASONING_MODEL"
echo "  Coding model:    $PROVIDER/$CODING_MODEL"
echo "  Discord channel: $DISCORD_CHANNEL"
echo "  Discord user:    $DISCORD_USER"
echo ""
if [ ${#WARNINGS[@]} -gt 0 ]; then
  echo "Warnings:"
  for w in "${WARNINGS[@]}"; do
    echo "  ⚠ $w"
  done
  echo ""
fi
echo "Next steps:"
echo "  1. Export env vars in your shell (if warnings above):"
echo "       export $API_KEY_ENV='<your-key>'"
if [ "$DISCORD_CHANNEL" != "0" ]; then
  echo "       export NOCTURNE_DISCORD_TOKEN='<your-bot-token>'"
fi
echo "  2. Verify provider/model registered with OpenCode:"
echo "       bash $REPO_ROOT/scripts/check_opencode_provider.sh"
echo "  3. Bootstrap sandbox:"
echo "       GITHUB_OWNER=$OWNER SANDBOX_REPO=$SANDBOX_REPO bash $REPO_ROOT/scripts/bootstrap_sandbox.sh"
echo "  4. First run:"
echo "       $NOCTURNE_BIN run-once --repo $OWNER/$SANDBOX_REPO --issue 1"
echo "  5. Continuous daemon:"
echo "       $NOCTURNE_BIN daemon"
