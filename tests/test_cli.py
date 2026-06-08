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
from nocturne.config import (
    Config,
    DaemonConfig,
    DiscordConfig,
    GitHubConfig,
    GuardrailsConfig,
    HealthcheckConfig,
    ModelsConfig,
    OpenCodeConfig,
    PersonaConfig,
    ProviderConfig,
    RepoConfig,
    ReviewConfig,
    SandboxConfig,
)
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
        # Build the deny-pattern at runtime so the secret-scan in Task 24 doesn't
        # match this test fixture as a real leaked secret.
        fake_token = "gho_" + ("a" * 30)
        soul_file = tmp_path / "soul.md"
        soul_file.write_text(f"GitHub token {fake_token} some prose")

        cfg = _make_test_config()
        cfg.persona.enabled = True
        cfg.persona.soul_path = str(soul_file)

        with patch("nocturne.cli._load_cfg") as mock_load:
            mock_load.return_value = cfg
            result = runner.invoke(app, ["soul", "show"])

        assert result.exit_code == 0
        assert "***" in result.stdout
        assert fake_token[:7] not in result.stdout


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
        # Build pattern at runtime (see redact test rationale).
        fake_token = "gho_" + ("a" * 30)
        source = tmp_path / "source.md"
        source.write_text(f"GitHub token {fake_token}")

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

            # Simulate editor writing secret (pattern built at runtime).
            fake_token = "gho_" + ("a" * 30)

            def write_secret(*args, **kwargs):
                dest.write_text(fake_token)

            mock_run.side_effect = write_secret

            result = runner.invoke(app, ["soul", "edit"])

        assert result.exit_code == 2
        assert "secret pattern" in result.stdout or "secret pattern" in result.stderr
        # Should revert to original
        assert dest.read_text() == "original"


class TestRunOnceBatch:
    """Task 22: --issue is optional; omitted triggers run_batch."""

    def _make_empty_report(self) -> Any:
        from nocturne.models import RunReport

        now = datetime.now(timezone.utc)
        return RunReport(
            started_at=now,
            ended_at=now,
            done=[],
            parked=[],
            skipped=[],
            errors=[],
            summary="",
            token_usage=0,
        )

    def test_run_once_without_issue_calls_run_batch(self, tmp_path: Path) -> None:
        cfg = _make_test_config()
        fake_report = self._make_empty_report()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.check_all_models_available"), \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.run_batch") as mock_run_batch, \
             patch("nocturne.cli.process_task") as mock_process, \
             patch("nocturne.cli.github_issues.fetch_one") as mock_fetch, \
             patch("nocturne.cli.write_report") as mock_write, \
             patch("nocturne.cli.summarize") as mock_summarize, \
             patch("nocturne.cli.Store"):

            mock_load_cfg.return_value = cfg
            mock_run_batch.return_value = fake_report
            mock_write.return_value = tmp_path / "report.md"
            mock_summarize.return_value = "Empty run."

            result = runner.invoke(app, ["run-once", "--repo", "ba1lly/playground"])

        assert result.exit_code == 0, result.stdout
        mock_run_batch.assert_called_once()
        mock_process.assert_not_called()
        mock_fetch.assert_not_called()

    def test_run_once_without_issue_dry_run_forwards_flag(self, tmp_path: Path) -> None:
        cfg = _make_test_config()
        fake_report = self._make_empty_report()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.check_all_models_available"), \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.run_batch") as mock_run_batch, \
             patch("nocturne.cli.write_report") as mock_write, \
             patch("nocturne.cli.summarize") as mock_summarize, \
             patch("nocturne.cli.Store"):

            mock_load_cfg.return_value = cfg
            mock_run_batch.return_value = fake_report
            mock_write.return_value = tmp_path / "report.md"
            mock_summarize.return_value = "Empty run."

            result = runner.invoke(
                app, ["run-once", "--repo", "ba1lly/playground", "--dry-run"]
            )

        assert result.exit_code == 0, result.stdout
        mock_run_batch.assert_called_once()
        assert mock_run_batch.call_args.kwargs.get("dry_run") is True

    def test_run_once_with_issue_still_calls_process_task(self, tmp_path: Path) -> None:
        cfg = _make_test_config()
        task = _make_test_task()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.check_all_models_available"), \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.run_batch") as mock_run_batch, \
             patch("nocturne.cli.process_task") as mock_process, \
             patch("nocturne.cli.github_issues.fetch_one") as mock_fetch, \
             patch("nocturne.cli.write_report") as mock_write, \
             patch("nocturne.cli.summarize") as mock_summarize, \
             patch("nocturne.cli.Store"):

            mock_load_cfg.return_value = cfg
            mock_fetch.return_value = task
            mock_process.return_value = task.model_copy(update={"status": "done"})
            mock_write.return_value = tmp_path / "report.md"
            mock_summarize.return_value = "Done."

            result = runner.invoke(
                app, ["run-once", "--repo", "ba1lly/playground", "--issue", "1"]
            )

        assert result.exit_code == 0, result.stdout
        mock_process.assert_called_once()
        mock_run_batch.assert_not_called()

    def test_run_once_batch_errors_exits_1(self, tmp_path: Path) -> None:
        cfg = _make_test_config()
        bad_report = self._make_empty_report()
        bad_report.errors = ["fetch_eligible: gh auth"]

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.check_all_models_available"), \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.run_batch") as mock_run_batch, \
             patch("nocturne.cli.write_report") as mock_write, \
             patch("nocturne.cli.summarize") as mock_summarize, \
             patch("nocturne.cli.Store"):

            mock_load_cfg.return_value = cfg
            mock_run_batch.return_value = bad_report
            mock_write.return_value = tmp_path / "report.md"
            mock_summarize.return_value = "1 error."

            result = runner.invoke(app, ["run-once", "--repo", "ba1lly/playground"])

        assert result.exit_code == 1


class TestResume:
    """Test resume command."""

    def test_resume_list_empty(self, tmp_path: Path) -> None:
        """Test resume --list with no parked tasks."""
        cfg = _make_test_config()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.Store"), \
             patch("nocturne.askflow.list_parked") as mock_list_parked:

            mock_load_cfg.return_value = cfg
            mock_list_parked.return_value = []

            result = runner.invoke(app, ["resume", "--list"])

        assert result.exit_code == 0
        assert "no parked tasks" in result.stdout

    def test_resume_list_populated(self, tmp_path: Path) -> None:
        """Test resume --list with parked tasks."""
        from nocturne.models import ParkedTask

        cfg = _make_test_config()
        now = datetime.now(timezone.utc)
        parked_task = ParkedTask(
            id="ba1lly/playground#1",
            status="parked",
            created_at=now,
            updated_at=now,
            repo_slug="ba1lly/playground",
            checkout_path="/home/bailly/projects/nocturne",
            issue_number=1,
            title="Test issue",
            body="Test body",
            base="main",
            verify_cmd="pytest",
            require_new_test=True,
            coding_model="dashscope/qwen-coder-32b-latest",
            branch="",
            attempts=0,
            question="What should I do?",
            parked_at=now,
        )

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.Store"), \
             patch("nocturne.askflow.list_parked") as mock_list_parked:

            mock_load_cfg.return_value = cfg
            mock_list_parked.return_value = [parked_task]

            result = runner.invoke(app, ["resume", "--list"])

        assert result.exit_code == 0
        assert "ba1lly/playground#1" in result.stdout
        assert "What should I do?" in result.stdout

    def test_resume_missing_task_id_exits_2(self, tmp_path: Path) -> None:
        """Test resume without --task-id and without --list exits 2."""
        cfg = _make_test_config()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.Store"):

            mock_load_cfg.return_value = cfg

            result = runner.invoke(app, ["resume"])

        assert result.exit_code == 2
        assert "--task-id required" in result.stdout or "--task-id required" in result.stderr

    def test_resume_invalid_task_id_format_exits_2(self, tmp_path: Path) -> None:
        """Test resume with invalid task_id format exits 2."""
        cfg = _make_test_config()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.Store"):

            mock_load_cfg.return_value = cfg

            result = runner.invoke(app, ["resume", "--task-id", "foo"])

        assert result.exit_code == 2
        assert "invalid task_id format" in result.stdout or "invalid task_id format" in result.stderr

    def test_resume_task_not_found_exits_1(self, tmp_path: Path) -> None:
        """Test resume with non-existent task exits 1."""
        cfg = _make_test_config()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.Store") as mock_store_class:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()
            mock_store.get_task.return_value = None
            mock_store_class.return_value = mock_store

            result = runner.invoke(app, ["resume", "--task-id", "ba1lly/playground#1", "--answer", "go ahead"])

        assert result.exit_code == 1
        assert "task not found" in result.stdout or "task not found" in result.stderr

    def test_resume_task_not_parked_exits_1(self, tmp_path: Path) -> None:
        """Test resume with non-parked task exits 1."""
        cfg = _make_test_config()
        task = _make_test_task()
        task.status = "selected"

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.Store") as mock_store_class:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()
            mock_store.get_task.return_value = task
            mock_store_class.return_value = mock_store

            result = runner.invoke(app, ["resume", "--task-id", "ba1lly/playground#1", "--answer", "go ahead"])

        assert result.exit_code == 1
        assert "is not parked" in result.stdout or "is not parked" in result.stderr

    def test_resume_invokes_askflow_with_answer(self, tmp_path: Path) -> None:
        """Test resume invokes askflow.resume_with_answer with correct args."""
        cfg = _make_test_config()
        task = _make_test_task()
        task.status = "parked"
        task.question = "What should I do?"
        result_task = task.model_copy(update={"status": "selected"})

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.Store") as mock_store_class, \
             patch("nocturne.askflow.resume_with_answer") as mock_resume:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()
            mock_store.get_task.return_value = task
            mock_store_class.return_value = mock_store
            mock_resume.return_value = result_task

            result = runner.invoke(app, ["resume", "--task-id", "ba1lly/playground#1", "--answer", "go ahead"])

        assert result.exit_code == 0
        assert "Resumed task" in result.stdout
        mock_resume.assert_called_once_with("ba1lly/playground#1", "go ahead", cfg, mock_store)

    def test_resume_interactive_prompt_when_no_answer(self, tmp_path: Path) -> None:
        """Test resume prompts interactively when --answer not provided."""
        cfg = _make_test_config()
        task = _make_test_task()
        task.status = "parked"
        task.question = "What should I do?"
        result_task = task.model_copy(update={"status": "selected"})

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.Store") as mock_store_class, \
             patch("nocturne.askflow.resume_with_answer") as mock_resume, \
             patch("nocturne.cli.typer.prompt") as mock_prompt:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()
            mock_store.get_task.return_value = task
            mock_store_class.return_value = mock_store
            mock_prompt.return_value = "interactive answer"
            mock_resume.return_value = result_task

            result = runner.invoke(app, ["resume", "--task-id", "ba1lly/playground#1"])

        assert result.exit_code == 0
        assert "Resumed task" in result.stdout
        mock_prompt.assert_called_once()
        mock_resume.assert_called_once_with("ba1lly/playground#1", "interactive answer", cfg, mock_store)

    def test_resume_empty_answer_exits_2(self, tmp_path: Path) -> None:
        """Test resume with empty answer exits 2."""
        cfg = _make_test_config()
        task = _make_test_task()
        task.status = "parked"
        task.question = "What should I do?"

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.Store") as mock_store_class, \
             patch("nocturne.cli.typer.prompt") as mock_prompt:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()
            mock_store.get_task.return_value = task
            mock_store_class.return_value = mock_store
            mock_prompt.return_value = ""

            result = runner.invoke(app, ["resume", "--task-id", "ba1lly/playground#1"])

        assert result.exit_code == 2
        assert "cannot be empty" in result.stdout or "cannot be empty" in result.stderr


class TestDaemon:
    """Test daemon command."""

    def test_daemon_help_shows_once_flag(self) -> None:
        """Test daemon --help shows --once flag."""
        result = runner.invoke(app, ["daemon", "--help"])
        assert result.exit_code == 0
        assert "--once" in result.stdout

    def test_daemon_once_calls_run_one_cycle(self, tmp_path: Path) -> None:
        """Test daemon --once calls run_one_cycle and exits."""
        from unittest.mock import AsyncMock
        cfg = _make_test_config()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.setup_logging"), \
             patch("nocturne.cli.check_all_models_available"), \
             patch("nocturne.cli.Store") as mock_store_class, \
             patch("nocturne.daemon.Daemon") as mock_daemon_class, \
             patch("nocturne.daemon_recovery.reconcile") as mock_reconcile:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()
            mock_store_class.return_value = mock_store
            mock_reconcile.return_value = {}

            mock_daemon = MagicMock()
            mock_daemon.run_one_cycle = AsyncMock(return_value={"fetched": 0})
            mock_daemon_class.return_value = mock_daemon

            result = runner.invoke(app, ["daemon", "--once"])

            assert result.exit_code == 0
            mock_daemon.run_one_cycle.assert_called_once()
            assert "One cycle complete" in result.stdout


class TestStatus:
    """Test status command (real implementation)."""

    def test_status_empty_db(self, tmp_path: Path) -> None:
        """Test status with empty DB."""
        cfg = _make_test_config()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.Store") as mock_store_class:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()
            mock_store.list_by_status.return_value = []
            mock_store.get_daemon_flag.return_value = None
            mock_store._conn.execute.return_value.fetchall.return_value = []
            mock_store_class.return_value = mock_store

            result = runner.invoke(app, ["status"])

            assert result.exit_code == 0
            assert "Tasks by Status" in result.stdout

    def test_status_with_parked_task(self, tmp_path: Path) -> None:
        """Test status shows parked tasks."""
        cfg = _make_test_config()
        parked_task = _make_test_task()
        parked_task.status = "parked"
        parked_task.question = "What should I do?"

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.Store") as mock_store_class:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()

            def list_by_status_side_effect(status: str) -> list:
                if status == "parked":
                    return [parked_task]
                return []

            mock_store.list_by_status.side_effect = list_by_status_side_effect
            mock_store.get_daemon_flag.return_value = None
            mock_store._conn.execute.return_value.fetchall.return_value = []
            mock_store_class.return_value = mock_store

            result = runner.invoke(app, ["status"])

            assert result.exit_code == 0
            assert "Parked" in result.stdout
            assert "What should I do?" in result.stdout

    def test_status_shows_paused_flag(self, tmp_path: Path) -> None:
        """Test status shows paused flag."""
        cfg = _make_test_config()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.Store") as mock_store_class:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()
            mock_store.list_by_status.return_value = []
            mock_store.get_daemon_flag.return_value = "1"
            mock_store._conn.execute.return_value.fetchall.return_value = []
            mock_store_class.return_value = mock_store

            result = runner.invoke(app, ["status"])

            assert result.exit_code == 0
            assert "paused: yes" in result.stdout


class TestPause:
    """Test pause command."""

    def test_pause_sets_flag(self, tmp_path: Path) -> None:
        """Test pause sets the paused flag."""
        cfg = _make_test_config()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.Store") as mock_store_class:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()
            mock_store_class.return_value = mock_store

            result = runner.invoke(app, ["pause"])

            assert result.exit_code == 0
            mock_store.set_daemon_flag.assert_called_once_with("paused", "1")
            assert "pause flag set" in result.stdout


class TestUnpause:
    """Test unpause command."""

    def test_unpause_clears_flag(self, tmp_path: Path) -> None:
        """Test unpause clears the paused flag."""
        cfg = _make_test_config()

        with patch("nocturne.cli._load_cfg") as mock_load_cfg, \
             patch("nocturne.cli.Store") as mock_store_class:

            mock_load_cfg.return_value = cfg
            mock_store = MagicMock()
            mock_store_class.return_value = mock_store

            result = runner.invoke(app, ["unpause"])

            assert result.exit_code == 0
            mock_store.set_daemon_flag.assert_called_once_with("paused", "0")
            assert "unpause flag set" in result.stdout
