#!/usr/bin/env bash
set -euo pipefail

# Install nocturne systemd user service
# Usage: bash scripts/install-systemd.sh [--dry-run] [--force]

DRY_RUN=0
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

repo_root="$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel)"
template_path="$repo_root/scripts/systemd/nocturne.service"
user_unit_dir="$HOME/.config/systemd/user"
user_unit_path="$user_unit_dir/nocturne.service"

if [[ ! -f "$template_path" ]]; then
  echo "ERROR: Unit template not found at $template_path" >&2
  exit 1
fi

# Read template content
template_content="$(cat "$template_path")"

# Check if unit already exists and has same content
if [[ -f "$user_unit_path" ]]; then
  existing_content="$(cat "$user_unit_path")"
  if [[ "$template_content" == "$existing_content" ]] && [[ $FORCE -eq 0 ]]; then
    echo "Unit already installed at $user_unit_path (no changes needed)"
    exit 0
  fi
  if [[ $FORCE -eq 0 ]]; then
    echo "Unit exists at $user_unit_path with different content. Use --force to overwrite." >&2
    exit 1
  fi
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "DRY RUN: Would install nocturne.service to $user_unit_path"
  echo "DRY RUN: Would run: loginctl enable-linger $USER"
  echo "DRY RUN: Would run: systemctl --user daemon-reload"
  echo "DRY RUN: Would run: systemctl --user enable nocturne.service"
  exit 0
fi

# Create directory
mkdir -p "$user_unit_dir"

# Install unit file
cp "$template_path" "$user_unit_path"
echo "Installed nocturne.service at $user_unit_path"

# Enable linger (daemon survives logout/reboot)
echo "Enabling linger for user $USER (daemon will survive logout/reboot)..."
if loginctl enable-linger "$USER" 2>/dev/null || true; then
  echo "Linger enabled"
else
  echo "WARNING: Could not enable linger (loginctl may not be available or already enabled)"
fi

# Reload systemd
systemctl --user daemon-reload
echo "Reloaded systemd user daemon"

# Enable service (but don't start)
systemctl --user enable nocturne.service
echo "Enabled nocturne.service (not started)"

# Print next steps
cat <<EOF

Installed nocturne.service at $user_unit_path

Next steps:
  1. Edit ~/.config/nocturne/env to add DASHSCOPE_API_KEY and NOCTURNE_DISCORD_TOKEN
  2. systemctl --user start nocturne
  3. journalctl --user -u nocturne -f

EOF
