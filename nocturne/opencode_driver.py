from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import IO, Callable, cast

from nocturne._sandbox import scrubbed_env
from nocturne.config import Config, provider_of
from nocturne.guardrails import enforce_no_dangerous_opencode_flags
from nocturne.models import OpenCodeResult, Task
from nocturne.prompts.render import render_task_prompt


class OpenCodeError(Exception):
    pass


class OpenCodeTimeout(OpenCodeError):
    pass


SENTINEL = "##NOCTURNE_NEED_INPUT##"


def render_prompt(task: Task, cfg: Config, prior_failure: str | None = None) -> str:
    if (task.answer or "").strip():
        from nocturne.askflow import render_resume_prompt
        return render_resume_prompt(task, cfg, prior_failure)
    return render_task_prompt(task, cfg, prior_failure)


def parse_ndjson_line(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = cast(object, json.loads(stripped))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return cast(dict[str, object], parsed)


def parse_ndjson_stream(text: str) -> tuple[list[dict[str, object]], list[str]]:
    events: list[dict[str, object]] = []
    parse_errors: list[str] = []
    for line in text.split("\n"):
        event = parse_ndjson_line(line)
        if event is not None:
            events.append(event)
        elif line.strip():
            parse_errors.append(line)
    return events, parse_errors


def detect_sentinel(events: list[dict[str, object]]) -> str | None:
    last_text: str | None = None
    for event in reversed(events):
        if event.get("type") != "text":
            continue
        text = event.get("text")
        if not isinstance(text, str):
            part = event.get("part")
            part_text = cast(dict[str, object], part).get("text") if isinstance(part, dict) else None
            text = part_text if isinstance(part_text, str) else None
        last_text = text if isinstance(text, str) else ""
        break

    if last_text is None:
        return None

    match = re.search(r"##NOCTURNE_NEED_INPUT##\s*\n(.+)", last_text, re.DOTALL)
    if match is None:
        return None
    return match.group(1).strip()


def has_error_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [event for event in events if event.get("type") == "error"]


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _sum_token_mapping(tokens: dict[str, object]) -> int:
    """Sum a single usage/token mapping to a total token count.

    Handles the two shapes seen in practice:
      - opencode native:  {input, output, reasoning, cache: {read, write}}
      - OpenAI-style:     {prompt_tokens, completion_tokens, total_tokens}
    When an explicit total is present we trust it (and do NOT also add the
    components, which would double-count). Otherwise we sum the components.
    """
    for total_key in ("total_tokens", "total"):
        if total_key in tokens:
            return _coerce_int(tokens[total_key])
    total = 0
    for key in (
        "input",
        "output",
        "reasoning",
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
    ):
        total += _coerce_int(tokens.get(key))
    cache = tokens.get("cache")
    if isinstance(cache, dict):
        total += _coerce_int(cache.get("read"))
        total += _coerce_int(cache.get("write"))
    return total


def _event_token_usage(event: dict[str, object]) -> int:
    """Extract token usage from one event, counting at most one mapping.

    opencode attaches usage either at the top level or nested under the
    message/part container depending on event type. We count the first
    non-zero mapping found so a single event is never double-counted.
    """
    candidates: list[dict[str, object]] = []
    for key in ("tokens", "usage"):
        value = event.get(key)
        if isinstance(value, dict):
            candidates.append(cast(dict[str, object], value))
    for container_key in ("part", "message", "info", "metadata"):
        container = event.get(container_key)
        if isinstance(container, dict):
            for key in ("tokens", "usage"):
                value = cast(dict[str, object], container).get(key)
                if isinstance(value, dict):
                    candidates.append(cast(dict[str, object], value))
    for candidate in candidates:
        total = _sum_token_mapping(candidate)
        if total:
            return total
    return 0


def extract_token_usage(events: list[dict[str, object]]) -> int:
    """Total tokens consumed across an opencode session's event stream.

    Returns 0 when the stream carries no usage data (older opencode builds,
    timeouts, or non-LLM runs) so callers can treat it as a best-effort
    measurement rather than a guarantee.
    """
    return sum(_event_token_usage(event) for event in events)


def _build_opencode_args(task: Task, cwd: Path, prompt_content: str, cfg: Config) -> list[str]:
    args: list[str] = [
        cfg.opencode.command,
        "run",
        "--dir",
        str(cwd),
        "--format",
        "json",
    ]
    model_string = task.coding_model if task.coding_model else cfg.models.coding
    if model_string:
        args.extend(["--model", model_string])
    args.extend(["--", prompt_content])
    return args


def _build_child_env(task: Task, cfg: Config, gh_config_dir: str) -> dict[str, str]:
    """Environment for the opencode subprocess: parent env minus git remote
    credentials, with gh redirected at a throwaway empty config dir and SSH
    auth disabled.

    opencode runs over attacker-influenceable issue text inside the worktree;
    every git push / PR creation is performed by the PARENT process AFTER
    opencode exits, so the agent never needs git remote write access. Only the
    coding provider's API key is added back so opencode can reach its model.
    """
    extra: dict[str, str] = {}
    model_string = task.coding_model if task.coding_model else cfg.models.coding
    if model_string:
        provider_name = provider_of(model_string)
        provider_cfg = cfg.providers.get(provider_name)
        if provider_cfg is not None:
            api_key = os.environ.get(provider_cfg.api_key_env, "")
            if api_key:
                extra["OPENCODE_PROVIDER_API_KEY"] = api_key
    return scrubbed_env(gh_config_dir=gh_config_dir, extra=extra)


# Per-stream cap on opencode output held in memory. A real run emits kilobytes
# of NDJSON; this bounds a runaway/malicious agent that loops emitting output so
# it can never OOM the daemon. Output beyond the cap is drained but discarded,
# and the run is failed (truncated NDJSON must not be acted on as success).
_MAX_OUTPUT_CHARS = 64 * 1024 * 1024  # 64 MiB


def _drain_capped(stream: IO[str], cap: int, sink: dict[str, object]) -> None:
    """Read a text stream to EOF, retaining at most ``cap`` chars in
    ``sink['parts']`` and setting ``sink['truncated']`` if it overflowed.

    Keeps reading past the cap (discarding) so the child never blocks on a full
    pipe; only the retained bytes consume memory.
    """
    parts = cast(list[str], sink["parts"])
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if total < cap:
                parts.append(chunk[: cap - total])
            if total + len(chunk) > cap:
                sink["truncated"] = True
            total += len(chunk)
    except (OSError, ValueError):
        pass


def run(
    task: Task,
    cwd: Path,
    cfg: Config,
    prior_failure: str | None = None,
    on_pid_started: Callable[[int], None] | None = None,
    prompt_override: str | None = None,
) -> OpenCodeResult:
    prompt_content = prompt_override if prompt_override is not None else render_prompt(task, cfg, prior_failure)
    args = _build_opencode_args(task, cwd, prompt_content, cfg)
    enforce_no_dangerous_opencode_flags(args)

    # Isolated, empty gh config dir for the child so opencode cannot reach the
    # operator's stored GitHub auth. Lives only for this run; cleaned up below.
    gh_config_dir = tempfile.mkdtemp(prefix="nocturne-gh-isolated-")
    try:
        env = _build_child_env(task, cfg, gh_config_dir)

        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(cwd),
        )
        if on_pid_started is not None:
            on_pid_started(proc.pid)

        assert proc.stdout is not None and proc.stderr is not None  # PIPE set above
        # Drain both streams in threads with a per-stream memory cap so a
        # runaway agent cannot OOM the daemon by emitting unbounded output.
        out_sink: dict[str, object] = {"parts": [], "truncated": False}
        err_sink: dict[str, object] = {"parts": [], "truncated": False}
        t_out = threading.Thread(target=_drain_capped, args=(proc.stdout, _MAX_OUTPUT_CHARS, out_sink), daemon=True)
        t_err = threading.Thread(target=_drain_capped, args=(proc.stderr, _MAX_OUTPUT_CHARS, err_sink), daemon=True)
        t_out.start()
        t_err.start()

        try:
            proc.wait(timeout=cfg.opencode.timeout_min * 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return OpenCodeResult(
                exit_code=-1,
                events=[],
                sentinel_seen=False,
                need_input_question=None,
                pid=proc.pid,
                error_events=[{"type": "timeout"}],
                token_usage=0,
            )

        t_out.join(timeout=10)
        t_err.join(timeout=10)
        stdout = "".join(cast(list[str], out_sink["parts"]))

        events, _parse_errors = parse_ndjson_stream(stdout)
        error_events = has_error_events(events)
        # Output overflow means the NDJSON is truncated: fail the attempt rather
        # than act on a partial stream as if it succeeded.
        if out_sink["truncated"]:
            error_events = [*error_events, {"type": "output_truncated"}]
        question = detect_sentinel(events)
        return OpenCodeResult(
            exit_code=proc.returncode,
            events=events,
            sentinel_seen=question is not None,
            need_input_question=question,
            pid=proc.pid,
            error_events=error_events,
            token_usage=extract_token_usage(events),
        )
    finally:
        shutil.rmtree(gh_config_dir, ignore_errors=True)
