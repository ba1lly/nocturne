from __future__ import annotations

import re
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from pydantic_core import core_schema

TaskStatus = Literal["selected", "running", "done", "parked", "skipped", "failed", "aborted"]


class TriageOutcome(str):
    _allowed_values = ("DOABLE", "SKIP", "NEED_INPUT")
    _adapter = TypeAdapter(Literal["DOABLE", "SKIP", "NEED_INPUT"])

    def __new__(cls, value: str) -> "TriageOutcome":
        validated = cls._adapter.validate_python(value)
        return str.__new__(cls, validated)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(cls)


class BaseTaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime


class Task(BaseTaskModel):
    _repo_slug_re: ClassVar[re.Pattern[str]] = re.compile(
        r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
    )

    repo_slug: str
    checkout_path: str
    issue_number: int
    title: str
    body: str
    base: str
    verify_cmd: str
    require_new_test: bool
    coding_model: str
    branch: str
    attempts: int
    pr_url: str | None = None
    question: str | None = None
    answer: str | None = None
    opencode_pid: int | None = None

    @field_validator("repo_slug")
    @classmethod
    def validate_repo_slug(cls, value: str) -> str:
        if not cls._repo_slug_re.fullmatch(value):
            raise ValueError("repo_slug must match owner/repo format")
        return value


class ParkedTask(BaseTaskModel):
    repo_slug: str
    checkout_path: str
    issue_number: int
    title: str
    body: str
    base: str
    verify_cmd: str
    require_new_test: bool
    coding_model: str
    branch: str
    attempts: int
    pr_url: str | None = None
    question: str = Field(min_length=1)
    answer: str | None = None
    opencode_pid: int | None = None
    parked_at: datetime

    @field_validator("repo_slug")
    @classmethod
    def validate_repo_slug(cls, value: str) -> str:
        if not Task._repo_slug_re.fullmatch(value):
            raise ValueError("repo_slug must match owner/repo format")
        return value


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    doable: bool
    outcome: TriageOutcome
    priority: int = Field(ge=0, le=100)
    reason: str


class VerifyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    new_test_added: bool
    reason: str | None = None


class OpenCodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exit_code: int
    events: list[dict[str, Any]]
    sentinel_seen: bool
    need_input_question: str | None = None
    pid: int | None = None
    error_events: list[dict[str, Any]]


class RunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started_at: datetime
    ended_at: datetime
    done: list[Task]
    parked: list[ParkedTask]
    skipped: list[tuple[int, str]]
    aborted: list[Task] = Field(default_factory=list)
    errors: list[str]
    summary: str
    token_usage: int
