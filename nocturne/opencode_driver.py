from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, cast

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


def run(
    task: Task,
    cwd: Path,
    cfg: Config,
    prior_failure: str | None = None,
    on_pid_started: Callable[[int], None] | None = None,
) -> OpenCodeResult:
    prompt_content = render_prompt(task, cfg, prior_failure)
    args = _build_opencode_args(task, cwd, prompt_content, cfg)
    enforce_no_dangerous_opencode_flags(args)

    env = {**os.environ}
    model_string = task.coding_model if task.coding_model else cfg.models.coding
    if model_string:
        provider_name = provider_of(model_string)
        provider_cfg = cfg.providers.get(provider_name)
        if provider_cfg is not None:
            api_key = os.environ.get(provider_cfg.api_key_env, "")
            if api_key:
                env["OPENCODE_PROVIDER_API_KEY"] = api_key

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

    try:
        stdout, _stderr = proc.communicate(timeout=cfg.opencode.timeout_min * 60)
    except subprocess.TimeoutExpired:
        proc.kill()
        _ = proc.communicate()
        return OpenCodeResult(
            exit_code=-1,
            events=[],
            sentinel_seen=False,
            need_input_question=None,
            pid=proc.pid,
            error_events=[{"type": "timeout"}],
        )

    events, _parse_errors = parse_ndjson_stream(stdout)
    error_events = has_error_events(events)
    question = detect_sentinel(events)
    return OpenCodeResult(
        exit_code=proc.returncode,
        events=events,
        sentinel_seen=question is not None,
        need_input_question=question,
        pid=proc.pid,
        error_events=error_events,
    )
