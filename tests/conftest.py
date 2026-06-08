from __future__ import annotations

import importlib
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from nocturne.store import Store

from tests.fakes import FakeOpenAI, RecordingSubprocess


os.environ.setdefault("DASHSCOPE_API_KEY", "test-dashscope-key")


@pytest.fixture
def inmem_store() -> Iterator[Store]:
    store = Store(":memory:")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def tmp_worktree(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "checkout", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_path), "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "--allow-empty", "-m", "init"],
        check=True,
    )
    return repo_path


@pytest.fixture
def mock_subprocess(monkeypatch: pytest.MonkeyPatch) -> RecordingSubprocess:
    recorder = RecordingSubprocess()
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


@pytest.fixture
def mock_openai(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    openai = importlib.import_module("openai")
    fake = FakeOpenAI()
    monkeypatch.setattr(openai, "OpenAI", lambda *args, **kwargs: fake)
    return fake


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch):
    current = {"dt": datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)}

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            dt = current["dt"]
            if tz is None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt.astimezone(tz)

        @classmethod
        def utcnow(cls):
            return current["dt"].astimezone(timezone.utc).replace(tzinfo=None)

    def _patch(module_name: str) -> None:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "datetime", FakeDateTime)

    for module_name in ("nocturne.store", "nocturne.guardrails"):
        try:
            _patch(module_name)
        except Exception:
            pass

    class ClockController:
        def advance(self, seconds: int) -> None:
            current["dt"] = current["dt"] + timedelta(seconds=seconds)

        def set(self, dt: datetime) -> None:
            if dt.tzinfo is None:
                current["dt"] = dt.replace(tzinfo=timezone.utc)
            else:
                current["dt"] = dt.astimezone(timezone.utc)

    return ClockController()
