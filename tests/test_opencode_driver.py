from __future__ import annotations

# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportAny=false, reportExplicitAny=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportArgumentType=false, reportUnusedCallResult=false
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

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
from nocturne.guardrails import GuardrailViolation
from nocturne.models import Task
from nocturne.opencode_driver import (
    SENTINEL,
    _build_opencode_args,
    detect_sentinel,
    has_error_events,
    parse_ndjson_line,
    parse_ndjson_stream,
    render_prompt,
    run,
)


class FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0, pid: int = 42) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = pid
        self.killed = False
        self.communicate_calls: list[float | int | None] = []

    def communicate(self, timeout: float | int | None = None) -> tuple[str, str]:
        self.communicate_calls.append(timeout)
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True


class TimeoutProc(FakeProc):
    def communicate(self, timeout: float | int | None = None) -> tuple[str, str]:
        self.communicate_calls.append(timeout)
        if len(self.communicate_calls) == 1:
            raise subprocess.TimeoutExpired(cmd=["opencode"], timeout=timeout)
        return "drained stdout", "drained stderr"


@pytest.fixture
def cfg(tmp_worktree: Path) -> Config:
    return Config(
        github=GitHubConfig(owner="ba1lly"),
        sandbox=SandboxConfig(repo_name="nocturne-playground", checkout_path=str(tmp_worktree)),
        providers={"alibaba-coding-plan": ProviderConfig(base_url="https://example.test", api_key_env="DASHSCOPE_API_KEY")},
        models=ModelsConfig(
            reasoning="alibaba-coding-plan/qwen3-reasoning-plus",
            coding="alibaba-coding-plan/qwen3-coder-plus",
            report="alibaba-coding-plan/qwen3-report-plus",
        ),
        opencode=OpenCodeConfig(command="opencode", timeout_min=1, worktree_root=str(tmp_worktree)),
        repos=[RepoConfig(slug="ba1lly/nocturne-playground", checkout_path=str(tmp_worktree), verify_cmd="pytest -q")],
        guardrails=GuardrailsConfig(),
        discord=DiscordConfig(channel_id=1, mention_user_id=1),
        daemon=DaemonConfig(),
        review=ReviewConfig(),
        healthcheck=HealthcheckConfig(),
        persona=PersonaConfig(enabled=False),
    )


@pytest.fixture
def task(tmp_worktree: Path) -> Task:
    now = datetime.now(UTC)
    return Task(
        id="issue#1",
        repo_slug="ba1lly/nocturne-playground",
        checkout_path=str(tmp_worktree),
        issue_number=1,
        title="Implement driver test title",
        body="Issue body",
        base="main",
        verify_cmd="pytest -q",
        require_new_test=False,
        coding_model="",
        branch="nocturne/issue-1",
        status="running",
        attempts=1,
        created_at=now,
        updated_at=now,
    )


def patch_popen(monkeypatch: pytest.MonkeyPatch, proc: FakeProc) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_popen(*args: Any, **kwargs: Any) -> FakeProc:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return captured


def test_render_prompt_returns_rendered_string_with_issue_title(task: Task, cfg: Config) -> None:
    content = render_prompt(task, cfg)

    assert isinstance(content, str)
    assert "Implement driver test title" in content


def test_render_prompt_does_not_write_anything_to_worktree(task: Task, cfg: Config, tmp_path: Path) -> None:
    before = set(tmp_path.rglob("*"))
    _ = render_prompt(task, cfg)
    after = set(tmp_path.rglob("*"))
    assert before == after, "render_prompt must not touch the worktree"


@pytest.mark.parametrize("line", ["", "   ", "not json", "{bad"])
def test_parse_ndjson_line_returns_none_for_empty_garbage_or_invalid(line: str) -> None:
    assert parse_ndjson_line(line) is None


def test_parse_ndjson_line_returns_dict_for_valid_json() -> None:
    assert parse_ndjson_line('{"type":"text","text":"hi"}') == {"type": "text", "text": "hi"}


def test_parse_ndjson_stream_separates_events_and_parse_errors() -> None:
    text = '{"type":"text","text":"ok"}\n garbage \n\n{"type":"error","message":"x"}\n   '

    events, parse_errors = parse_ndjson_stream(text)

    assert events == [{"type": "text", "text": "ok"}, {"type": "error", "message": "x"}]
    assert parse_errors == [" garbage "]


def test_detect_sentinel_returns_question_from_last_text_event() -> None:
    assert detect_sentinel([{"type": "text", "text": f"{SENTINEL}\nWhy?"}]) == "Why?"


def test_sentinel_only_in_last_text() -> None:
    events = [
        {"type": "text", "text": f"Echoed issue body: {SENTINEL}\nShould not trigger"},
        {"type": "text", "text": "Final answer without sentinel"},
    ]

    assert detect_sentinel(events) is None


def test_detect_sentinel_ignores_user_system_and_error_events_even_when_last() -> None:
    events = [
        {"type": "user", "text": f"{SENTINEL}\nUser question"},
        {"type": "system", "text": f"{SENTINEL}\nSystem question"},
        {"type": "error", "text": f"{SENTINEL}\nError question"},
    ]

    assert detect_sentinel(events) is None


def test_detect_sentinel_reads_part_text_shape() -> None:
    assert detect_sentinel([{"type": "text", "part": {"text": f"{SENTINEL}\nQ"}}]) == "Q"


def test_has_error_events_returns_errors_when_present() -> None:
    error = {"type": "error", "message": "x"}
    assert has_error_events([{"type": "text", "text": "ok"}, error]) == [error]


def test_has_error_events_returns_empty_list_when_absent() -> None:
    assert has_error_events([{"type": "text", "text": "ok"}]) == []


def test_build_opencode_args_uses_config_model_when_task_model_empty(task: Task, cfg: Config, tmp_path: Path) -> None:
    cfg.models.coding = "alibaba-coding-plan/qwen3-coder-plus"
    args = _build_opencode_args(task, tmp_path, "test prompt content", cfg)

    assert args[args.index("--model") + 1] == cfg.models.coding
    assert args[-1] == "test prompt content"
    assert "-f" not in args


def test_build_opencode_args_uses_task_model_when_set(task: Task, cfg: Config, tmp_path: Path) -> None:
    task.coding_model = "alibaba-coding-plan/custom-coder"
    args = _build_opencode_args(task, tmp_path, "test", cfg)

    assert args[args.index("--model") + 1] == "alibaba-coding-plan/custom-coder"


def test_build_opencode_args_omits_model_when_neither_set(task: Task, cfg: Config, tmp_path: Path) -> None:
    cfg.models.coding = None
    task.coding_model = ""
    args = _build_opencode_args(task, tmp_path, "test prompt", cfg)

    assert "--model" not in args
    assert args[-1] == "test prompt"


def test_dangerous_flag_blocked(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "nocturne.opencode_driver._build_opencode_args",
        lambda *_args, **_kwargs: ["opencode", "run", "--dangerously-skip-permissions"],
    )

    with pytest.raises(GuardrailViolation):
        run(task, tmp_path, cfg)


def test_run_captures_pid_before_communicate(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    order: list[str] = []

    class OrderingProc(FakeProc):
        def communicate(self, timeout: float | int | None = None) -> tuple[str, str]:
            order.append("communicate")
            return super().communicate(timeout)

    proc = OrderingProc(stdout='{"type":"text","text":"done"}\n', pid=42)
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg, on_pid_started=lambda pid: order.append(f"pid:{pid}"))

    assert order == ["pid:42", "communicate"]
    assert result.pid == 42


def test_timeout_kills_process(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    proc = TimeoutProc(pid=99)
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg)

    assert proc.killed is True
    assert proc.communicate_calls == [60, None]
    assert result.exit_code == -1
    assert result.pid == 99
    assert result.error_events == [{"type": "timeout"}]


def test_run_success_parses_stdout_without_sentinel(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    proc = FakeProc(stdout=json.dumps({"type": "text", "text": "done"}) + "\n", returncode=0)
    captured = patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg)

    assert captured["args"][0][0] == "opencode"
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["env"]["OPENCODE_PROVIDER_API_KEY"]
    assert result.exit_code == 0
    assert result.events == [{"type": "text", "text": "done"}]
    assert result.sentinel_seen is False
    assert result.error_events == []


def test_run_sentinel_path_populates_question(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    proc = FakeProc(stdout=json.dumps({"type": "text", "text": f"{SENTINEL}\nNeed details?"}) + "\n")
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg)

    assert result.sentinel_seen is True
    assert result.need_input_question == "Need details?"


def test_run_error_event_path_collects_error_events(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    proc = FakeProc(stdout=json.dumps({"type": "error", "message": "x"}) + "\n", returncode=0)
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg)

    assert result.exit_code == 0
    assert result.error_events == [{"type": "error", "message": "x"}]


def test_run_tolerates_parse_errors(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    proc = FakeProc(stdout='{"type":"text","text":"ok"}\ngarbage\n{"type":"text","text":"done"}\n')
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg)

    assert result.events == [{"type": "text", "text": "ok"}, {"type": "text", "text": "done"}]
    assert result.sentinel_seen is False
