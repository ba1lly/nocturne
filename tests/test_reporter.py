from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from nocturne.models import RunReport, Task, ParkedTask
from nocturne.config import Config, ProviderConfig, ModelsConfig
from nocturne.reporter import (
    write_report,
    summarize,
    discord_message,
    _human_duration,
    _deterministic_summary,
    _format_task_report,
    _format_run_report,
    post_task_report,
    post_run_report,
)


@pytest.fixture
def sample_task() -> Task:
    """Create a sample Task for testing."""
    return Task(
        id="task-1",
        status="done",
        created_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 8, 12, 30, tzinfo=timezone.utc),
        repo_slug="owner/repo",
        checkout_path="/tmp/repo",
        issue_number=1,
        title="Fix bug in parser",
        body="This is a bug",
        base="main",
        verify_cmd="pytest",
        require_new_test=True,
        coding_model="alibaba-coding-plan/qwen3.6-plus",
        branch="fix/parser-bug",
        attempts=1,
        pr_url="https://github.com/owner/repo/pull/42",
    )


@pytest.fixture
def sample_parked_task() -> ParkedTask:
    """Create a sample ParkedTask for testing."""
    return ParkedTask(
        id="task-2",
        status="parked",
        created_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 8, 12, 30, tzinfo=timezone.utc),
        repo_slug="owner/repo",
        checkout_path="/tmp/repo",
        issue_number=2,
        title="Implement feature X",
        body="This is a feature",
        base="main",
        verify_cmd="pytest",
        require_new_test=True,
        coding_model="alibaba-coding-plan/qwen3.6-plus",
        branch="feat/x",
        attempts=2,
        pr_url=None,
        question="Should we use async or sync?",
        parked_at=datetime(2026, 6, 8, 12, 30, tzinfo=timezone.utc),
    )


@pytest.fixture
def sample_report(sample_task: Task, sample_parked_task: ParkedTask) -> RunReport:
    """Create a sample RunReport for testing."""
    return RunReport(
        started_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 6, 8, 12, 5, tzinfo=timezone.utc),
        done=[sample_task],
        parked=[sample_parked_task],
        skipped=[(3, "Not enough context")],
        errors=["Timeout on task 4"],
        summary="Test summary",
        token_usage=5000,
    )


@pytest.fixture
def test_config(tmp_worktree: Path) -> Config:
    """Create a test Config."""
    return Config(
        github={"owner": "test-owner"},
        sandbox={"repo_name": "test-repo"},
        providers={
            "alibaba-coding-plan": ProviderConfig(
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                api_key_env="DASHSCOPE_API_KEY",
            ),
        },
        models=ModelsConfig(
            reasoning="alibaba-coding-plan/qwen3.6-plus",
            coding="alibaba-coding-plan/qwen3.6-plus",
            report="alibaba-coding-plan/qwen3.6-plus",
        ),
        opencode={"command": "opencode", "timeout_min": 25, "worktree_root": "/tmp/nocturne"},
        repos=[
            {
                "slug": "owner/repo",
                "checkout_path": str(tmp_worktree),
                "verify_cmd": "pytest",
            }
        ],
        guardrails={},
        discord={"enabled": False, "channel_id": 123456, "mention_user_id": 789012},
        daemon={},
        review={},
        healthcheck={},
        persona={},
    )


class TestHumanDuration:
    def test_seconds_only(self) -> None:
        start = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 8, 12, 0, 30, tzinfo=timezone.utc)
        assert _human_duration(start, end) == "30s"

    def test_minutes_and_seconds(self) -> None:
        start = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 8, 12, 5, 30, tzinfo=timezone.utc)
        assert _human_duration(start, end) == "5m 30s"

    def test_minutes_only(self) -> None:
        start = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 8, 12, 5, 0, tzinfo=timezone.utc)
        assert _human_duration(start, end) == "5m"

    def test_none_end(self) -> None:
        start = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
        assert _human_duration(start, None) == "in progress"


class TestWriteReport:
    def test_write_report_creates_file(self, sample_report: RunReport, tmp_path: Path) -> None:
        """Test that write_report creates a file and returns its path."""
        result_path = write_report(sample_report, tmp_path)
        
        assert result_path.exists()
        assert result_path.parent == tmp_path
        assert result_path.name.endswith(".md")
        # Verify no colons in filename (filesystem-safe)
        assert ":" not in result_path.name

    def test_write_report_contents(self, sample_report: RunReport, tmp_path: Path) -> None:
        """Test that report file contains all expected sections."""
        result_path = write_report(sample_report, tmp_path)
        content = result_path.read_text()
        
        # Check header
        assert "# Nocturne Run Report" in content
        assert "**Started**:" in content
        assert "**Ended**:" in content
        assert "**Duration**:" in content
        
        # Check summary section
        assert "## Summary" in content
        assert "Test summary" in content
        
        # Check done section
        assert "## Done (1)" in content
        assert "Issue #1" in content
        assert "Fix bug in parser" in content
        assert "https://github.com/owner/repo/pull/42" in content
        assert "`fix/parser-bug`" in content
        assert "Attempts: 1" in content
        
        # Check parked section
        assert "## Parked (1)" in content
        assert "Issue #2" in content
        assert "Implement feature X" in content
        assert "Should we use async or sync?" in content
        
        # Check skipped section
        assert "## Skipped (1)" in content
        assert "Issue #3" in content
        assert "Not enough context" in content
        
        # Check errors section
        assert "## Errors (1)" in content
        assert "Timeout on task 4" in content
        
        # Check token usage
        assert "Token usage: 5000 tokens" in content

    def test_write_report_filesystem_safe_filename(self, sample_report: RunReport, tmp_path: Path) -> None:
        """Test that filename uses dashes instead of colons."""
        result_path = write_report(sample_report, tmp_path)
        # Expected format: 2026-06-08T12-00-00.md
        assert result_path.name == "2026-06-08T12-00-00.md"
        assert ":" not in result_path.name

    def test_write_report_creates_directory(self, sample_report: RunReport, tmp_path: Path) -> None:
        """Test that write_report creates reports_dir if it doesn't exist."""
        reports_dir = tmp_path / "reports" / "nested"
        assert not reports_dir.exists()
        
        write_report(sample_report, reports_dir)
        
        assert reports_dir.exists()

    def test_write_report_empty_lists(self, tmp_path: Path) -> None:
        """Test report with empty done/parked/skipped/errors lists."""
        report = RunReport(
            started_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 6, 8, 12, 5, tzinfo=timezone.utc),
            done=[],
            parked=[],
            skipped=[],
            errors=[],
            summary="Empty run",
            token_usage=0,
        )
        
        result_path = write_report(report, tmp_path)
        content = result_path.read_text()
        
        assert "## Done (0)" in content
        assert "## Parked (0)" in content
        assert "## Skipped (0)" in content
        assert "## Errors (0)" in content


class TestDeterministicSummary:
    def test_deterministic_summary(self, sample_report: RunReport) -> None:
        """Test deterministic summary format."""
        result = _deterministic_summary(sample_report)
        assert result == "1 done, 1 parked, 1 skipped, 1 errors."

    def test_deterministic_summary_empty(self) -> None:
        """Test deterministic summary for empty report."""
        report = RunReport(
            started_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 6, 8, 12, 5, tzinfo=timezone.utc),
            done=[],
            parked=[],
            skipped=[],
            errors=[],
            summary="",
            token_usage=0,
        )
        result = _deterministic_summary(report)
        assert result == "0 done, 0 parked, 0 skipped, 0 errors."


class TestSummarize:
    def test_summarize_empty_run(self, test_config: Config) -> None:
        """Test that empty run returns 'Empty run.'"""
        report = RunReport(
            started_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 6, 8, 12, 5, tzinfo=timezone.utc),
            done=[],
            parked=[],
            skipped=[],
            errors=[],
            summary="",
            token_usage=0,
        )
        
        result = summarize(report, test_config)
        assert result == "Empty run."

    def test_summarize_llm_success(self, sample_report: RunReport, test_config: Config, mock_openai) -> None:
        """Test successful LLM summarization."""
        mock_openai.responses.append("All tasks completed successfully.")
        
        result = summarize(sample_report, test_config)
        
        assert result == "All tasks completed successfully."

    def test_summarize_llm_failure_fallback(self, sample_report: RunReport, test_config: Config) -> None:
        """Test that LLM failure falls back to deterministic summary."""
        with patch("openai.OpenAI") as mock_openai_class:
            mock_openai_class.side_effect = Exception("API error")
            
            result = summarize(sample_report, test_config)
            
            # Should return deterministic summary, not raise
            assert "done" in result
            assert "parked" in result
            assert "skipped" in result
            assert "errors" in result

    def test_summarize_missing_api_key(self, sample_report: RunReport, test_config: Config) -> None:
        """Test that missing API key falls back gracefully."""
        with patch("nocturne.config.get_api_key") as mock_get_key:
            mock_get_key.side_effect = Exception("Missing API key")
            
            result = summarize(sample_report, test_config)
            
            # Should return deterministic summary
            assert "done" in result


class TestDiscordMessage:
    def test_discord_message_clean_run(self, sample_task: Task) -> None:
        """Test Discord message for clean run (no errors/parked)."""
        report = RunReport(
            started_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 6, 8, 12, 5, tzinfo=timezone.utc),
            done=[sample_task],
            parked=[],
            skipped=[],
            errors=[],
            summary="",
            token_usage=0,
        )
        
        result = discord_message(report)
        
        assert result.startswith("🟢")
        assert "1 done" in result
        assert "0 parked" in result
        assert "0 errors" in result

    def test_discord_message_with_parked(self, sample_task: Task, sample_parked_task: ParkedTask) -> None:
        """Test Discord message with parked tasks."""
        report = RunReport(
            started_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 6, 8, 12, 5, tzinfo=timezone.utc),
            done=[sample_task],
            parked=[sample_parked_task],
            skipped=[],
            errors=[],
            summary="",
            token_usage=0,
        )
        
        result = discord_message(report)
        
        assert result.startswith("🟡")
        assert "1 parked" in result

    def test_discord_message_with_errors(self, sample_task: Task) -> None:
        """Test Discord message with errors."""
        report = RunReport(
            started_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 6, 8, 12, 5, tzinfo=timezone.utc),
            done=[sample_task],
            parked=[],
            skipped=[],
            errors=["Error 1", "Error 2"],
            summary="",
            token_usage=0,
        )
        
        result = discord_message(report)
        
        assert result.startswith("🔴")
        assert "2 errors" in result

    def test_discord_message_under_280_chars(self, sample_task: Task) -> None:
        """Test that Discord message is truncated to 280 chars."""
        # Create many done tasks to make a long message
        tasks = [
            Task(
                id=f"task-{i}",
                status="done",
                created_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
                updated_at=datetime(2026, 6, 8, 12, 30, tzinfo=timezone.utc),
                repo_slug="owner/repo",
                checkout_path="/tmp/repo",
                issue_number=i,
                title=f"Task {i}",
                body="",
                base="main",
                verify_cmd="pytest",
                require_new_test=True,
                coding_model="alibaba-coding-plan/qwen3.6-plus",
                branch=f"fix/{i}",
                attempts=1,
                pr_url=f"https://github.com/owner/repo/pull/{i}",
            )
            for i in range(1, 20)
        ]
        
        report = RunReport(
            started_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 6, 8, 12, 5, tzinfo=timezone.utc),
            done=tasks,
            parked=[],
            skipped=[],
            errors=[],
            summary="",
            token_usage=0,
        )
        
        result = discord_message(report)
        
        assert len(result) <= 280
        if len(result) == 280:
            assert result.endswith("...")

    def test_discord_message_with_pr_url(self, sample_task: Task) -> None:
        """Test that Discord message includes first PR URL."""
        report = RunReport(
            started_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 6, 8, 12, 5, tzinfo=timezone.utc),
            done=[sample_task],
            parked=[],
            skipped=[],
            errors=[],
            summary="",
            token_usage=0,
        )
        
        result = discord_message(report)
        
        assert "PR: https://github.com/owner/repo/pull/42" in result

    def test_discord_message_no_pr_url(self, sample_task: Task) -> None:
        """Test Discord message when first task has no PR URL."""
        task_no_pr = Task(
            id="task-1",
            status="done",
            created_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 6, 8, 12, 30, tzinfo=timezone.utc),
            repo_slug="owner/repo",
            checkout_path="/tmp/repo",
            issue_number=1,
            title="Fix bug",
            body="",
            base="main",
            verify_cmd="pytest",
            require_new_test=True,
            coding_model="alibaba-coding-plan/qwen3.6-plus",
            branch="fix/bug",
            attempts=1,
            pr_url=None,
        )
        
        report = RunReport(
            started_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 6, 8, 12, 5, tzinfo=timezone.utc),
            done=[task_no_pr],
            parked=[],
            skipped=[],
            errors=[],
            summary="",
            token_usage=0,
        )
        
        result = discord_message(report)
        
        assert "PR:" not in result


class TestFormatTaskReport:
    def test_format_task_report_done_with_pr(self) -> None:
        """Done task with PR url → message starts with 🟢, contains PR url."""
        t = Task(
            id="x/y#42",
            repo_slug="x/y",
            checkout_path="/tmp/x",
            issue_number=42,
            title="Fix divide() bug",
            body="b",
            base="main",
            verify_cmd="pytest",
            require_new_test=False,
            coding_model="x/y",
            branch="b",
            status="done",
            attempts=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            pr_url="https://github.com/x/y/pull/1",
        )
        msg = _format_task_report(t)
        assert msg.startswith("🟢")
        assert "#42" in msg
        assert "pull/1" in msg

    def test_format_task_report_parked(self) -> None:
        """Parked task → 🟡 emoji."""
        t = Task(
            id="x/y#42",
            repo_slug="x/y",
            checkout_path="/tmp/x",
            issue_number=42,
            title="Fix divide() bug",
            body="b",
            base="main",
            verify_cmd="pytest",
            require_new_test=False,
            coding_model="x/y",
            branch="b",
            status="parked",
            attempts=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            pr_url=None,
        )
        msg = _format_task_report(t)
        assert msg.startswith("🟡")

    def test_format_task_report_failed(self) -> None:
        """Failed task → 🔴 emoji."""
        t = Task(
            id="x/y#42",
            repo_slug="x/y",
            checkout_path="/tmp/x",
            issue_number=42,
            title="Fix divide() bug",
            body="b",
            base="main",
            verify_cmd="pytest",
            require_new_test=False,
            coding_model="x/y",
            branch="b",
            status="failed",
            attempts=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            pr_url=None,
        )
        msg = _format_task_report(t)
        assert msg.startswith("🔴")

    def test_format_task_report_truncates_at_280(self) -> None:
        """Very long title → message truncated to 280 chars with ... suffix."""
        long_title = "X" * 500
        t = Task(
            id="x/y#42",
            repo_slug="x/y",
            checkout_path="/tmp/x",
            issue_number=42,
            title=long_title,
            body="b",
            base="main",
            verify_cmd="pytest",
            require_new_test=False,
            coding_model="x/y",
            branch="b",
            status="done",
            attempts=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            pr_url="https://x/y",
        )
        msg = _format_task_report(t)
        assert len(msg) <= 280

    def test_280_cap(self) -> None:
        """Alias for the cap test under the QA scenario name."""
        long_title = "Y" * 1000
        t = Task(
            id="x/y#42",
            repo_slug="x/y",
            checkout_path="/tmp/x",
            issue_number=42,
            title=long_title,
            body="b",
            base="main",
            verify_cmd="pytest",
            require_new_test=False,
            coding_model="x/y",
            branch="b",
            status="done",
            attempts=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            pr_url=None,
        )
        msg = _format_task_report(t)
        assert len(msg) <= 280


class TestFormatRunReport:
    def test_format_run_report_includes_counts(self) -> None:
        """Run report message includes all four count categories."""
        t1 = Task(
            id="x/y#1",
            repo_slug="x/y",
            checkout_path="/tmp/x",
            issue_number=1,
            title="Task 1",
            body="b",
            base="main",
            verify_cmd="pytest",
            require_new_test=False,
            coding_model="x/y",
            branch="b",
            status="done",
            attempts=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            pr_url=None,
        )
        t2 = Task(
            id="x/y#2",
            repo_slug="x/y",
            checkout_path="/tmp/x",
            issue_number=2,
            title="Task 2",
            body="b",
            base="main",
            verify_cmd="pytest",
            require_new_test=False,
            coding_model="x/y",
            branch="b",
            status="done",
            attempts=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            pr_url=None,
        )
        report = RunReport(
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            done=[t1, t2],
            parked=[],
            skipped=[(3, "vague")],
            errors=["err1"],
            summary="",
            token_usage=0,
        )
        msg = _format_run_report(report)
        assert "2 done" in msg
        assert "1 skipped" in msg
        assert "1 errors" in msg


class TestPostTaskReport:
    @pytest.mark.asyncio
    async def test_post_task_report_no_mention(self) -> None:
        """Task completion → bot.send_status_msg called with NO <@ mention pattern."""
        bot = MagicMock()
        bot.send_status_msg = AsyncMock(return_value=12345)
        t = Task(
            id="x/y#42",
            repo_slug="x/y",
            checkout_path="/tmp/x",
            issue_number=42,
            title="Fix divide() bug",
            body="b",
            base="main",
            verify_cmd="pytest",
            require_new_test=False,
            coding_model="x/y",
            branch="b",
            status="done",
            attempts=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            pr_url="https://x/y",
        )
        msg_id = await post_task_report(t, bot)
        assert msg_id == 12345
        args, kwargs = bot.send_status_msg.call_args
        text = args[0]
        assert "<@" not in text, f"unexpected mention pattern in: {text}"

    @pytest.mark.asyncio
    async def test_post_task_report_no_bot_returns_none(self) -> None:
        """No bot → returns None silently."""
        t = Task(
            id="x/y#42",
            repo_slug="x/y",
            checkout_path="/tmp/x",
            issue_number=42,
            title="Fix divide() bug",
            body="b",
            base="main",
            verify_cmd="pytest",
            require_new_test=False,
            coding_model="x/y",
            branch="b",
            status="done",
            attempts=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            pr_url=None,
        )
        assert await post_task_report(t, None) is None

    @pytest.mark.asyncio
    async def test_post_task_report_swallows_bot_failure(self) -> None:
        """Bot raises → post_task_report logs warning, returns None (non-blocking)."""
        bot = MagicMock()
        bot.send_status_msg = AsyncMock(side_effect=RuntimeError("boom"))
        t = Task(
            id="x/y#42",
            repo_slug="x/y",
            checkout_path="/tmp/x",
            issue_number=42,
            title="Fix divide() bug",
            body="b",
            base="main",
            verify_cmd="pytest",
            require_new_test=False,
            coding_model="x/y",
            branch="b",
            status="done",
            attempts=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            pr_url=None,
        )
        result = await post_task_report(t, bot)
        assert result is None


class TestPostRunReport:
    @pytest.mark.asyncio
    async def test_post_run_report_calls_bot(self) -> None:
        """post_run_report calls bot.send_status_msg and returns message ID."""
        bot = MagicMock()
        bot.send_status_msg = AsyncMock(return_value=99999)
        report = RunReport(
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            done=[],
            parked=[],
            skipped=[],
            errors=[],
            summary="",
            token_usage=0,
        )
        msg_id = await post_run_report(report, bot)
        assert msg_id == 99999
