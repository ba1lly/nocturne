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
    extract_token_usage,
    has_error_events,
    parse_ndjson_line,
    parse_ndjson_stream,
    render_prompt,
    run,
)


class _FakeStream:
    """Minimal readable text stream: yields its data once, then EOF ('')."""

    def __init__(self, data: str) -> None:
        self._data = data
        self._done = False

    def read(self, _size: int = -1) -> str:
        if self._done:
            return ""
        self._done = True
        return self._data


class FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0, pid: int = 42) -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.returncode = returncode
        self.pid = pid
        self.killed = False
        self.wait_calls: list[float | int | None] = []

    def wait(self, timeout: float | int | None = None) -> int:
        self.wait_calls.append(timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class TimeoutProc(FakeProc):
    def wait(self, timeout: float | int | None = None) -> int:
        self.wait_calls.append(timeout)
        if len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired(cmd=["opencode"], timeout=timeout)
        return self.returncode


@pytest.fixture
def cfg(tmp_worktree: Path) -> Config:
    return Config(
        github=GitHubConfig(owner="owner"),
        sandbox=SandboxConfig(repo_name="nocturne-playground", checkout_path=str(tmp_worktree)),
        providers={"alibaba-coding-plan": ProviderConfig(base_url="https://example.test", api_key_env="DASHSCOPE_API_KEY")},
        models=ModelsConfig(
            reasoning="alibaba-coding-plan/qwen3-reasoning-plus",
            coding="alibaba-coding-plan/qwen3-coder-plus",
            report="alibaba-coding-plan/qwen3-report-plus",
        ),
        opencode=OpenCodeConfig(command="opencode", timeout_min=1, worktree_root=str(tmp_worktree)),
        repos=[RepoConfig(slug="owner/nocturne-playground", checkout_path=str(tmp_worktree), verify_cmd="pytest -q")],
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
        repo_slug="owner/nocturne-playground",
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
        def wait(self, timeout: float | int | None = None) -> int:
            order.append("wait")
            return super().wait(timeout)

    proc = OrderingProc(stdout='{"type":"text","text":"done"}\n', pid=42)
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg, on_pid_started=lambda pid: order.append(f"pid:{pid}"))

    assert order == ["pid:42", "wait"]
    assert result.pid == 42


def test_drain_capped_retains_up_to_cap_and_flags_truncation() -> None:
    from nocturne.opencode_driver import _drain_capped

    sink: dict[str, object] = {"parts": [], "truncated": False}
    _drain_capped(_FakeStream("a" * 100), 40, sink)

    assert sink["truncated"] is True
    assert len("".join(sink["parts"])) == 40  # never holds more than the cap


def test_drain_capped_under_cap_keeps_everything() -> None:
    from nocturne.opencode_driver import _drain_capped

    sink: dict[str, object] = {"parts": [], "truncated": False}
    _drain_capped(_FakeStream("hello"), 1000, sink)

    assert sink["truncated"] is False
    assert "".join(sink["parts"]) == "hello"


def test_run_flags_output_overflow_as_error(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    """A runaway agent that overflows the output cap must fail the attempt, not
    be treated as a successful run on truncated NDJSON."""
    monkeypatch.setattr("nocturne.opencode_driver._MAX_OUTPUT_CHARS", 20)
    big = json.dumps({"type": "text", "text": "x" * 200}) + "\n"
    proc = FakeProc(stdout=big, returncode=0)
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg)

    assert {"type": "output_truncated"} in result.error_events


def test_timeout_kills_process(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    proc = TimeoutProc(pid=99)
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg)

    assert proc.killed is True
    assert proc.wait_calls == [60, None]
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


def test_run_scrubs_github_credentials_from_child_env(
    monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path
) -> None:
    """opencode runs over attacker-influenceable issue text, so the subprocess
    must NOT inherit GitHub write tokens, and gh must be redirected at an
    isolated config dir so it cannot read the operator's stored auth."""
    monkeypatch.setenv("GH_TOKEN", "ghp_supersecret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh_other_secret")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "ent_secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/ssh-agent.sock")
    monkeypatch.setenv("SSH_AGENT_PID", "4321")
    proc = FakeProc(stdout=json.dumps({"type": "text", "text": "done"}) + "\n", returncode=0)
    captured = patch_popen(monkeypatch, proc)

    run(task, tmp_path, cfg)

    child_env = captured["kwargs"]["env"]
    assert "GH_TOKEN" not in child_env
    assert "GITHUB_TOKEN" not in child_env
    assert "GH_ENTERPRISE_TOKEN" not in child_env
    # ssh-agent forwarding is denied so the agent can't push as the operator.
    assert "SSH_AUTH_SOCK" not in child_env
    assert "SSH_AGENT_PID" not in child_env
    # gh pointed at a throwaway empty config dir, not the operator's real one.
    assert child_env["GH_CONFIG_DIR"]
    assert "nocturne-gh-isolated" in child_env["GH_CONFIG_DIR"]
    # SSH git auth neutralised (no agent, no on-disk key, no prompts).
    assert "IdentityAgent=none" in child_env["GIT_SSH_COMMAND"]
    assert "BatchMode=yes" in child_env["GIT_SSH_COMMAND"]
    assert child_env["GIT_TERMINAL_PROMPT"] == "0"
    # The coding provider key is still injected so opencode can reach its model.
    assert child_env["OPENCODE_PROVIDER_API_KEY"]


def test_run_isolated_gh_config_dir_is_cleaned_up(
    monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path
) -> None:
    proc = FakeProc(stdout=json.dumps({"type": "text", "text": "done"}) + "\n", returncode=0)
    captured = patch_popen(monkeypatch, proc)

    run(task, tmp_path, cfg)

    # The temp gh config dir must not leak onto disk after the run returns.
    assert not Path(captured["kwargs"]["env"]["GH_CONFIG_DIR"]).exists()


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


# -- token usage extraction --


def test_extract_token_usage_empty_stream_is_zero() -> None:
    assert extract_token_usage([]) == 0
    assert extract_token_usage([{"type": "text", "text": "hi"}]) == 0


def test_extract_token_usage_opencode_native_shape() -> None:
    events = [
        {"type": "step-finish", "tokens": {"input": 100, "output": 50, "reasoning": 10,
                                            "cache": {"read": 5, "write": 2}}},
    ]
    assert extract_token_usage(events) == 167


def test_extract_token_usage_openai_total_not_double_counted() -> None:
    # When an explicit total is present we trust it and ignore the components.
    events = [{"usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}}]
    assert extract_token_usage(events) == 100


def test_extract_token_usage_openai_components_summed_without_total() -> None:
    events = [{"usage": {"prompt_tokens": 80, "completion_tokens": 20}}]
    assert extract_token_usage(events) == 100


def test_extract_token_usage_sums_across_steps() -> None:
    events = [
        {"tokens": {"input": 10, "output": 5}},
        {"type": "text", "text": "noise"},
        {"tokens": {"input": 20, "output": 5}},
    ]
    assert extract_token_usage(events) == 40


def test_extract_token_usage_reads_nested_message_container() -> None:
    events = [{"type": "message", "message": {"tokens": {"input": 30, "output": 10}}}]
    assert extract_token_usage(events) == 40


def test_extract_token_usage_single_mapping_per_event_no_double_count() -> None:
    # Same usage mirrored at top level and under part → counted once.
    payload = {"input": 10, "output": 10}
    events = [{"tokens": payload, "part": {"tokens": payload}}]
    assert extract_token_usage(events) == 20


def test_run_populates_token_usage(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    stdout = (
        json.dumps({"type": "text", "text": "done"}) + "\n"
        + json.dumps({"type": "step-finish", "tokens": {"input": 200, "output": 40}}) + "\n"
    )
    proc = FakeProc(stdout=stdout, returncode=0)
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg)

    assert result.token_usage == 240


def test_run_timeout_token_usage_is_zero(monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path) -> None:
    proc = TimeoutProc(pid=7)
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg)

    assert result.token_usage == 0


# Verbatim stdout captured from a real `opencode run --format json` session
# (opencode 1.15.13). Anchors the parser to the actual event shape: tokens live
# under part.tokens in a step_finish event as {total,input,output,reasoning,
# cache:{write,read}}, and the plugin's OSC ]777;notify escape sequences must be
# tolerated as parse-errors without losing the token-bearing event.
_REAL_OPENCODE_STDOUT = (
    '\x1b]777;notify;warp://cli-agent;{"v":1,"event":"session_start"}'
    '{"type":"step_start","sessionID":"ses_1","part":{"id":"prt_1","type":"step-start"}}\n'
    '{"type":"text","sessionID":"ses_1","part":{"id":"prt_2","type":"text","text":"hi"}}\n'
    '{"type":"step_finish","sessionID":"ses_1","part":{"id":"prt_3","type":"step-finish",'
    '"tokens":{"total":37908,"input":6,"output":6,"reasoning":0,'
    '"cache":{"write":37896,"read":0}},"cost":0.23703}}\n'
    '\x1b]777;notify;warp://cli-agent;{"v":1,"event":"stop","response":"hi"}'
)


def test_extract_token_usage_matches_real_opencode_session() -> None:
    events, _errs = parse_ndjson_stream(_REAL_OPENCODE_STDOUT)
    # The text + step_finish lines parse; the notify-polluted lines are skipped.
    assert [e.get("type") for e in events] == ["text", "step_finish"]
    assert extract_token_usage(events) == 37908


def test_run_extracts_tokens_from_real_opencode_stream(
    monkeypatch: pytest.MonkeyPatch, task: Task, cfg: Config, tmp_path: Path
) -> None:
    proc = FakeProc(stdout=_REAL_OPENCODE_STDOUT, returncode=0)
    patch_popen(monkeypatch, proc)

    result = run(task, tmp_path, cfg)

    assert result.token_usage == 37908
