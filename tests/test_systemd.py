"""Tests for systemd unit template and installation scripts."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from nocturne._systemd import (
    UNIT_TEMPLATE_PATH,
    get_unit_template_path,
    render_unit,
    verify_unit,
)


class TestUnitTemplate:
    """Tests for the systemd unit template file."""

    def test_unit_template_exists(self) -> None:
        """Unit template file must exist."""
        assert UNIT_TEMPLATE_PATH.exists(), f"Unit template not found at {UNIT_TEMPLATE_PATH}"

    def test_unit_template_has_required_sections(self) -> None:
        """Unit template must have [Unit], [Service], and [Install] sections."""
        content = render_unit()
        assert "[Unit]" in content, "Missing [Unit] section"
        assert "[Service]" in content, "Missing [Service] section"
        assert "[Install]" in content, "Missing [Install] section"

    def test_unit_template_uses_user_level_path(self) -> None:
        """Unit template must use user-level paths (default.target, not multi-user.target)."""
        content = render_unit()
        assert "default.target" in content, "Must use default.target for user-level service"
        assert "multi-user.target" not in content, "Must not use multi-user.target"
        assert "/etc/systemd/" not in content, "Must not reference /etc/systemd/"

    def test_unit_template_restart_on_failure(self) -> None:
        """Unit template must have restart policy on-failure."""
        content = render_unit()
        assert "Restart=on-failure" in content, "Missing Restart=on-failure"
        assert "RestartSec=30s" in content, "Missing RestartSec=30s"
        assert "StartLimitBurst=5" in content, "Missing StartLimitBurst=5"

    def test_unit_template_no_secrets_inline(self) -> None:
        """Unit template must not contain inline secret patterns."""
        content = render_unit()
        # Check for common secret patterns
        assert "sk-" not in content, "Must not contain OpenAI API key pattern"
        assert "gho_" not in content, "Must not contain GitHub token pattern"
        assert "xox" not in content, "Must not contain Slack token pattern"
        assert "AKIA" not in content, "Must not contain AWS key pattern"

    def test_unit_template_uses_environment_file(self) -> None:
        """Unit template must use EnvironmentFile for secrets."""
        content = render_unit()
        assert "EnvironmentFile=" in content, "Must use EnvironmentFile for environment variables"
        assert "-%h/.config/nocturne/env" in content, "Must reference ~/.config/nocturne/env with - prefix"

    def test_unit_template_has_exec_start(self) -> None:
        """Unit template must have ExecStart directive."""
        content = render_unit()
        assert "ExecStart=" in content, "Missing ExecStart directive"
        assert "nocturne daemon" in content, "ExecStart must run 'nocturne daemon'"


class TestInstallScript:
    """Tests for the install script."""

    def test_install_script_exists(self) -> None:
        """Install script must exist."""
        script_path = Path(__file__).parent.parent / "scripts" / "install-systemd.sh"
        assert script_path.exists(), f"Install script not found at {script_path}"

    def test_install_script_is_executable(self) -> None:
        """Install script must be executable."""
        script_path = Path(__file__).parent.parent / "scripts" / "install-systemd.sh"
        assert script_path.stat().st_mode & 0o111, "Install script must be executable"

    def test_install_script_syntax_valid(self) -> None:
        """Install script must have valid bash syntax."""
        script_path = Path(__file__).parent.parent / "scripts" / "install-systemd.sh"
        result = subprocess.run(
            ["bash", "-n", str(script_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Install script syntax error: {result.stderr}"


class TestUninstallScript:
    """Tests for the uninstall script."""

    def test_uninstall_script_exists(self) -> None:
        """Uninstall script must exist."""
        script_path = Path(__file__).parent.parent / "scripts" / "uninstall-systemd.sh"
        assert script_path.exists(), f"Uninstall script not found at {script_path}"

    def test_uninstall_script_is_executable(self) -> None:
        """Uninstall script must be executable."""
        script_path = Path(__file__).parent.parent / "scripts" / "uninstall-systemd.sh"
        assert script_path.stat().st_mode & 0o111, "Uninstall script must be executable"

    def test_uninstall_script_syntax_valid(self) -> None:
        """Uninstall script must have valid bash syntax."""
        script_path = Path(__file__).parent.parent / "scripts" / "uninstall-systemd.sh"
        result = subprocess.run(
            ["bash", "-n", str(script_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Uninstall script syntax error: {result.stderr}"


class TestPythonHelpers:
    """Tests for Python helper functions."""

    def test_get_unit_template_path(self) -> None:
        """get_unit_template_path() must return the correct path."""
        path = get_unit_template_path()
        assert path == UNIT_TEMPLATE_PATH
        assert path.exists()

    def test_render_unit_returns_content(self) -> None:
        """render_unit() must return non-empty string starting with [Unit]."""
        content = render_unit()
        assert isinstance(content, str), "render_unit() must return a string"
        assert len(content) > 0, "render_unit() must return non-empty content"
        assert content.strip().startswith("[Unit]"), "Content must start with [Unit] section"

    def test_render_unit_with_custom_path(self) -> None:
        """render_unit() must accept custom template path."""
        content = render_unit(UNIT_TEMPLATE_PATH)
        assert "[Unit]" in content
        assert "[Service]" in content

    def test_verify_unit_handles_missing_binary(self) -> None:
        """verify_unit() must handle missing systemd-analyze gracefully."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            success, message = verify_unit(UNIT_TEMPLATE_PATH)
            assert success is False
            assert "systemd-analyze not available" in message
            assert isinstance(message, str)

    def test_verify_unit_handles_timeout(self) -> None:
        """verify_unit() must handle timeout gracefully."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("cmd", 10)
            success, message = verify_unit(UNIT_TEMPLATE_PATH)
            assert success is False
            assert "timed out" in message

    def test_verify_unit_returns_tuple(self) -> None:
        """verify_unit() must return a tuple of (bool, str)."""
        success, message = verify_unit(UNIT_TEMPLATE_PATH)
        assert isinstance(success, bool)
        assert isinstance(message, str)

    def test_verify_unit_with_mock_success(self) -> None:
        """verify_unit() must return (True, '') on successful verification."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["systemd-analyze", "verify", "path"],
                returncode=0,
                stdout="",
                stderr="",
            )
            success, message = verify_unit(UNIT_TEMPLATE_PATH)
            assert success is True
            assert message == ""

    def test_verify_unit_with_mock_failure(self) -> None:
        """verify_unit() must return (False, stderr) on verification failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["systemd-analyze", "verify", "path"],
                returncode=1,
                stdout="",
                stderr="Error: invalid unit",
            )
            success, message = verify_unit(UNIT_TEMPLATE_PATH)
            assert success is False
            assert "Error: invalid unit" in message
