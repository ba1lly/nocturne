"""systemd helper utilities - primarily for testing the unit template."""
from __future__ import annotations

import subprocess
from pathlib import Path

UNIT_TEMPLATE_PATH = Path(__file__).parent.parent / "scripts" / "systemd" / "nocturne.service"


def get_unit_template_path() -> Path:
    """Return the path to the systemd unit template."""
    return UNIT_TEMPLATE_PATH


def render_unit(template_path: Path = UNIT_TEMPLATE_PATH) -> str:
    """Read the unit file content. systemd's %h substitution happens at unit load time."""
    return template_path.read_text(encoding="utf-8")


def verify_unit(unit_path: Path) -> tuple[bool, str]:
    """Run `systemd-analyze verify <path>` and return (exit_code == 0, stderr).

    Returns (False, "systemd-analyze not available") if the binary is missing.
    """
    try:
        result = subprocess.run(
            ["systemd-analyze", "verify", str(unit_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (result.returncode == 0, result.stderr)
    except FileNotFoundError:
        return (False, "systemd-analyze not available")
    except subprocess.TimeoutExpired:
        return (False, "systemd-analyze timed out")
