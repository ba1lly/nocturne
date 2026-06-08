"""Tests for nocturne CLI."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from nocturne.cli import app
from nocturne.config import Config, PersonaConfig, RepoConfig, ProviderConfig, ModelsConfig
from nocturne.config import GitHubConfig, SandboxConfig, OpenCodeConfig, GuardrailsConfig
from nocturne.config import DiscordConfig, DaemonConfig, ReviewConfig, HealthcheckConfig
from nocturne.models import Task


runner = CliRunner()


def _make_test_config(
    repo_slug: str = "ba1lly/playground",
    checkout_path: str | None = None,
) -> Config:
    """Build a minimal Config for testing."""
    if checkout_path is None:
        checkout_path = str(Path("/home/bailly/projects/nocturne").resolve())

    return Config(
        github=GitHubConfig(owner="ba1lly"),
        sandbox=SandboxConfig(repo_name="nocturne-playground"),
        providers={
            "openai": ProviderConfig(
                base_url="https://api.openai.com/v1",
                api_key_env="OPENAI_API_KEY",
            ),
            "dashscope": ProviderConfig(
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                api_key_env="DASHSCOPE_API_KEY",
            ),
        },
        models=ModelsConfig(
            reasoning="dashscope/qwen-max",
            coding="dashscope/qwen-coder-32b-latest",
            report="openai/gpt-4o-mini",
        ),
        opencode=OpenCodeConfig(command="opencode", timeout_min=25),
        repos=[
            RepoConfig(
                slug=repo_slug,
                checkout_path=checkout_path,
                label="agent",
                base="main",
                verify_cmd="pytest",
                require_new_test=True,
            ),
        ],
        guardrails=GuardrailsConfig(max_attempts=3),
        discord=DiscordConfig(enabled=False, channel_id=1, mention_user_id=1),
        daemon=DaemonConfig(poll_interval_sec=300),
        review=ReviewConfig(enabled=True),
        healthcheck=HealthcheckConfig(enabled=False),
        persona=PersonaConfig(soul_path="~/.config/nocturne/soul.md", enabled=True),
    )


def _make_test_task(
    repo_slug: str = "ba1lly/playground",
    issue_number: int = 1,
) -> Task:
    """Build a minimal Task for testing."""
    now = datetime.now(timezone.utc)
    return Task(
        id="task-1",
        status="selected",
        created_at=now,
        updated_at=now,
        repo_slug=repo_slug,
        checkout_path="/home/bailly/projects/nocturne",
        issue_number=issue_number,
        title="Test issue",
        body="Test body",
        base="main",
        verify_cmd="pytest",
        require_new_test=True,
        coding_model="dashscope/qwen-coder-32b-latest",
        branch="",
        attempts=0,
    )


class TestVersion:
    """Test version command."""

    def test_version(self) -> None:
        """Test --version prints version."""
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "nocturne 0.1.0" in result.stdout


class TestRunOnceHelp:
    """Test run-once help."""

    def test_run_once_help(self) -> None:
        """Test run-once --help shows options."""
        result = runner.invoke(app, ["run-once", "--help"])
        assert result.exit_code == 0
        assert "--repo" in result.stdout
        assert "--issue" in result.stdout
        assert "--dry-run" in result.stdout


class TestStatus:
    """Test status command."""

    def test_status_stub(self) -> None:
        """Test status prints stub message."""
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "not implemented" in result.stdout


class TestRunOnceErrors:
    """Test run-once error handling."""

    def test_missing_config_exits_2(self, tmp_path: Path) -> None:
        """Test missing config file exits 2."""
        nonexistent = tmp_path / "nonexistent" / "config.yaml"
        result = runner.invoke(app, ["--config", str(nonexistent), "run-once", "--repo", "x", "--issue", "1"])
        assert result.exit_code == 2
        assert "config file not found" in result.stdout or "config file not found" in result.stderr

    def test_repo_not_in_allowlist(self, tmp_path: Path) -> None:
        """Test repo not in allowlist exits 2."""
        cfg = _make_test_config()
        cfg_path = tmp_path / "config.yaml"

        # Write config
        import yaml

        cfg_dict = cfg.model_dump(mode="json")
        cfg_path.write_text(yaml.dump(cfg_dict))

        with patch("nocturne.cli._load_cfg") as mock_load, \
             patch("nocturne.cli.check_all_models_available"):
            mock_load.return_value = cfg

            result = runner.invoke(
                app,
                ["--config", str(cfg_path), "run-once", "--repo", "evil/repo", "--issue", "1"],
            )

        assert result.exit_code == 2
        assert "not in allowlist" in result.stdout or "not in allowlist" in result.stderr


class TestDryRun:
    """Test dry-run functionality."""

    def test_dry_run_skips_push(self, tmp_path: Path) -> None:
        """Test dry-run forwards dry_run=True to process_task."""
        cfg = _make_test_config()
        task = _make_test_task()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.check_all_models_available"), \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.github_issues.fetch_one") as mock_fetch, \
             patch("nocturne.cli.process_task") as mock_process, \
             patch("nocturne.cli.write_report") as mock_write, \
             patch("nocturne.cli.summarize") as mock_summarize, \
             patch("nocturne.cli.Store"):

            mock_load_cfg.return_value = cfg
            mock_fetch.return_value = task
            result_task = task.model_copy(update={"status": "done"})
            mock_process.return_value = result_task
            mock_write.return_value = tmp_path / "report.md"
            mock_summarize.return_value = "Test summary"

            result = runner.invoke(
                app,
                ["run-once", "--repo", "ba1lly/playground", "--issue", "1", "--dry-run"],
            )

            assert result.exit_code == 0
            # Verify process_task was called with dry_run=True
            mock_process.assert_called_once()
            call_kwargs = mock_process.call_args[1]
            assert call_kwargs.get("dry_run") is True


class TestSoulShow:
    """Test soul show command."""

    def test_soul_show_disabled(self, tmp_path: Path) -> None:
        """Test soul show when persona disabled."""
        cfg = _make_test_config()
        cfg.persona.enabled = False

        with patch("nocturne.cli._load_cfg") as mock_load:
            mock_load.return_value = cfg
            result = runner.invoke(app, ["soul", "show"])

        assert result.exit_code == 0
        assert "persona disabled" in result.stdout

    def test_soul_show_missing_file(self, tmp_path: Path) -> None:
        """Test soul show when file missing."""
        cfg = _make_test_config()
        cfg.persona.enabled = True
        cfg.persona.soul_path = str(tmp_path / "nonexistent.md")

        with patch("nocturne.cli._load_cfg") as mock_load:
            mock_load.return_value = cfg
            result = runner.invoke(app, ["soul", "show"])

        assert result.exit_code == 0
        assert "no soul.md configured" in result.stdout

    def test_soul_show_redacts_secrets(self, tmp_path: Path) -> None:
        """Test soul show redacts secrets."""
        soul_file = tmp_path / "soul.md"
        soul_file.write_text("GitHub token gho_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa some prose")

        cfg = _make_test_config()
        cfg.persona.enabled = True
        cfg.persona.soul_path = str(soul_file)

        with patch("nocturne.cli._load_cfg") as mock_load:
            mock_load.return_value = cfg
            result = runner.invoke(app, ["soul", "show"])

        assert result.exit_code == 0
        assert "***" in result.stdout
        assert "gho_aaa" not in result.stdout


class TestSoulSet:
    """Test soul set command."""

    def test_soul_set_oversize(self, tmp_path: Path) -> None:
        """Test soul set rejects oversized file."""
        source = tmp_path / "source.md"
        source.write_text("x" * 9000)

        cfg = _make_test_config()
        dest = tmp_path / "soul.md"
        cfg.persona.soul_path = str(dest)

        with patch("nocturne.cli._load_cfg") as mock_load:
            mock_load.return_value = cfg
            result = runner.invoke(app, ["soul", "set", str(source)])

        assert result.exit_code == 2
        assert "exceeds 8192" in result.stdout or "exceeds 8192" in result.stderr

    def test_soul_set_secret_denied(self, tmp_path: Path) -> None:
        """Test soul set rejects secret patterns."""
        source = tmp_path / "source.md"
        source.write_text("GitHub token gho_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

        cfg = _make_test_config()
        dest = tmp_path / "soul.md"
        cfg.persona.soul_path = str(dest)

        with patch("nocturne.cli._load_cfg") as mock_load:
            mock_load.return_value = cfg
            result = runner.invoke(app, ["soul", "set", str(source)])

        assert result.exit_code == 2
        assert "secret pattern" in result.stdout or "secret pattern" in result.stderr

    def test_soul_set_success(self, tmp_path: Path) -> None:
        """Test soul set succeeds with valid file."""
        source = tmp_path / "source.md"
        source.write_text("# My Soul\n\nThis is my persona.")

        cfg = _make_test_config()
        dest = tmp_path / "soul.md"
        cfg.persona.soul_path = str(dest)

        with patch("nocturne.cli._load_cfg") as mock_load:
            mock_load.return_value = cfg
            result = runner.invoke(app, ["soul", "set", str(source)])

        assert result.exit_code == 0
        assert "Installed soul.md" in result.stdout
        assert dest.exists()
        assert dest.read_text() == "# My Soul\n\nThis is my persona."


class TestSoulEdit:
    """Test soul edit command."""

    def test_soul_edit_creates_file(self, tmp_path: Path) -> None:
        """Test soul edit creates file if missing."""
        cfg = _make_test_config()
        dest = tmp_path / "soul.md"
        cfg.persona.soul_path = str(dest)

        with patch("nocturne.cli._load_cfg") as mock_load, \
             patch("subprocess.run") as mock_run:
            mock_load.return_value = cfg
            mock_run.return_value = None

            # Simulate editor writing content
            def write_content(*args, **kwargs):
                dest.write_text("# Edited soul")

            mock_run.side_effect = write_content

            result = runner.invoke(app, ["soul", "edit"])

        assert result.exit_code == 0
        assert "Edited" in result.stdout
        assert dest.exists()

    def test_soul_edit_validates_size(self, tmp_path: Path) -> None:
        """Test soul edit validates size on save."""
        cfg = _make_test_config()
        dest = tmp_path / "soul.md"
        dest.write_text("original")
        cfg.persona.soul_path = str(dest)

        with patch("nocturne.cli._load_cfg") as mock_load, \
             patch("subprocess.run") as mock_run:
            mock_load.return_value = cfg

            # Simulate editor writing oversized content
            def write_oversized(*args, **kwargs):
                dest.write_text("x" * 9000)

            mock_run.side_effect = write_oversized

            result = runner.invoke(app, ["soul", "edit"])

        assert result.exit_code == 2
        assert "exceeds 8192" in result.stdout or "exceeds 8192" in result.stderr
        # Should revert to original
        assert dest.read_text() == "original"

    def test_soul_edit_validates_secrets(self, tmp_path: Path) -> None:
        """Test soul edit validates secrets on save."""
        cfg = _make_test_config()
        dest = tmp_path / "soul.md"
        dest.write_text("original")
        cfg.persona.soul_path = str(dest)

        with patch("nocturne.cli._load_cfg") as mock_load, \
             patch("subprocess.run") as mock_run:
            mock_load.return_value = cfg

            # Simulate editor writing secret
            def write_secret(*args, **kwargs):
                dest.write_text("gho_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

            mock_run.side_effect = write_secret

            result = runner.invoke(app, ["soul", "edit"])

        assert result.exit_code == 2
        assert "secret pattern" in result.stdout or "secret pattern" in result.stderr
        # Should revert to original
        assert dest.read_text() == "original"
