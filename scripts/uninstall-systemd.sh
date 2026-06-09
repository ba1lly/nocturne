#!/usr/bin/env bash
set -euo pipefail

# Uninstall nocturne systemd user service
# Usage: bash scripts/uninstall-systemd.sh

user_unit_path="$HOME/.config/systemd/user/nocturne.service"

# Stop the service (allow failure if not running)
echo "Stopping nocturne.service..."
systemctl --user stop nocturne.service 2>/dev/null || true

# Disable the service (allow failure)
echo "Disabling nocturne.service..."
systemctl --user disable nocturne.service 2>/dev/null || true

# Remove unit file if it exists
if [[ -f "$user_unit_path" ]]; then
  rm -f "$user_unit_path"
  echo "Removed $user_unit_path"
fi

# Reload systemd
systemctl --user daemon-reload
echo "Reloaded systemd user daemon"

echo "Uninstalled nocturne.service"
