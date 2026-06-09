from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from subprocess import CompletedProcess
from typing import Any, cast

from nocturne.models import OpenCodeResult


def make_subprocess_result(
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    args: list[str] | None = None,
) -> CompletedProcess[str]:
    return CompletedProcess(args=args or [], returncode=exit_code, stdout=stdout, stderr=stderr)


class FakeOpenCodeResult:
    @staticmethod
    def success(text: str) -> OpenCodeResult:
        return OpenCodeResult(
            exit_code=0,
            events=[{"type": "assistant", "text": text}],
            sentinel_seen=False,
            need_input_question=None,
            pid=12345,
            error_events=[],
        )

    @staticmethod
    def with_sentinel(question: str) -> OpenCodeResult:
        return OpenCodeResult(
            exit_code=0,
            events=[
                {"type": "assistant", "text": "working"},
                {"type": "assistant", "text": f"##NOCTURNE_NEED_INPUT##\n{question}"},
            ],
            sentinel_seen=True,
            need_input_question=question,
            pid=12345,
            error_events=[],
        )

    @staticmethod
    def with_error_event(message: str) -> OpenCodeResult:
        error = {"type": "error", "message": message}
        return OpenCodeResult(
            exit_code=1,
            events=[error],
            sentinel_seen=False,
            need_input_question=None,
            pid=12345,
            error_events=[error],
        )

    @staticmethod
    def timeout() -> OpenCodeResult:
        return OpenCodeResult(
            exit_code=-1,
            events=[],
            sentinel_seen=False,
            need_input_question=None,
            pid=None,
            error_events=[{"type": "timeout"}],
        )


class FakeGhResult:
    @staticmethod
    def success(stdout: str = "") -> CompletedProcess[str]:
        return make_subprocess_result(0, stdout=stdout, stderr="")

    @staticmethod
    def rate_limited() -> CompletedProcess[str]:
        return make_subprocess_result(
            1,
            stderr="API rate limit exceeded\nHTTP 403\n",
        )

    @staticmethod
    def auth_failed() -> CompletedProcess[str]:
        return make_subprocess_result(1, stderr="HTTP 401\n")

    @staticmethod
    def not_found() -> CompletedProcess[str]:
        return make_subprocess_result(1, stderr="HTTP 404\n")


@dataclass
class _FakeMessage:
    content: str | None


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeChatCompletionResponse:
    choices: list[_FakeChoice]


class FakeOpenAI:
    def __init__(self) -> None:
        self.responses: list[object] = []
        self.chat = self._Chat(self)

    class _Completions:
        def __init__(self, outer: "FakeOpenAI") -> None:
            self._outer = outer

        def create(self, *args: object, **kwargs: object) -> _FakeChatCompletionResponse:
            if not self._outer.responses:
                raise AssertionError("no fake openai responses queued")
            response = self._outer.responses.pop(0)
            if isinstance(response, str):
                return _FakeChatCompletionResponse(choices=[_FakeChoice(message=_FakeMessage(content=response))])
            return cast(_FakeChatCompletionResponse, response)

    class _Chat:
        def __init__(self, outer: "FakeOpenAI") -> None:
            self.completions = FakeOpenAI._Completions(outer)


class RecordingSubprocess:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.queued_results: deque[CompletedProcess[str]] = deque()

    def __call__(self, args: Any, **kwargs: Any) -> CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if self.queued_results:
            return self.queued_results.popleft()
        return make_subprocess_result(0, args=list(args) if isinstance(args, (list, tuple)) else [args])

    def queue_result(self, result: CompletedProcess[str]) -> None:
        self.queued_results.append(result)
