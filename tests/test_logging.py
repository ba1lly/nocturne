from __future__ import annotations

import importlib
import io
import logging
from pathlib import Path
from typing import Callable, Protocol, cast

import pytest


class _TimedLike(Protocol):
    elapsed_ms: float

    def __enter__(self) -> _TimedLike: ...

    def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, tb: object | None) -> bool: ...


_logging = importlib.import_module("nocturne._logging")
SensitiveFilter = cast(type[logging.Filter], getattr(_logging, "SensitiveFilter"))
discord_formatter = cast(Callable[[logging.LogRecord], str], getattr(_logging, "discord_formatter"))
get_logger = cast(Callable[[str], logging.Logger], getattr(_logging, "get_logger"))
setup_logging = cast(Callable[[Path], None], getattr(_logging, "setup_logging"))
timed = cast(Callable[[str], _TimedLike], getattr(_logging, "timed"))


def _record(message: str, *, level: int = logging.INFO, args: tuple[object, ...] = ()) -> logging.LogRecord:
    return logging.LogRecord("nocturne.test", level, __file__, 1, message, args, None)


def test_sensitive_filter_redacts_bearer_in_msg() -> None:
    record = _record("token Bearer abc123def456 leaked")

    _ = SensitiveFilter().filter(record)

    assert "***" in record.msg
    assert "abc123def456" not in record.msg


def test_sensitive_filter_redacts_sk_token_in_msg() -> None:
    record = _record("token sk-proj-xyz12345abcdef leaked")

    _ = SensitiveFilter().filter(record)

    assert "***" in record.msg
    assert "sk-proj-xyz12345abcdef" not in record.msg


def test_sensitive_filter_redacts_gho_token_in_msg() -> None:
    record = _record("token gho_abcdef1234567890 leaked")

    _ = SensitiveFilter().filter(record)

    assert "***" in record.msg
    assert "gho_abcdef1234567890" not in record.msg


def test_sensitive_filter_leaves_safe_strings_unchanged() -> None:
    record = _record("plain text only")

    _ = SensitiveFilter().filter(record)

    assert record.msg == "plain text only"


def test_sensitive_filter_redacts_args_tuple() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SensitiveFilter())
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger("nocturne.test.args")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("token=%s", "Bearer xyz123")
    handler.flush()

    output = stream.getvalue()
    assert "***" in output
    assert "Bearer xyz123" not in output


def test_discord_formatter_info_prefix() -> None:
    record = _record("hello world", level=logging.INFO)

    formatted = discord_formatter(record)

    assert formatted.startswith("🟢 [INFO]")


def test_discord_formatter_error_prefix() -> None:
    record = _record("boom", level=logging.ERROR)

    formatted = discord_formatter(record)

    assert formatted.startswith("🔴 [ERROR]")


def test_discord_280_cap() -> None:
    record = _record("x" * 400, level=logging.WARNING)

    formatted = discord_formatter(record)

    assert len(formatted) == 280
    assert formatted.endswith("...")


def test_setup_logging_creates_file_with_redaction(tmp_path: Path) -> None:
    setup_logging(tmp_path)
    logger = get_logger("nocturne.test.file")

    logger.info("token Bearer abc123def456 leaked")
    logging.shutdown()

    content = (tmp_path / "nocturne.log").read_text(encoding="utf-8")
    assert "***" in content
    assert "abc123def456" not in content


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    setup_logging(tmp_path)
    setup_logging(tmp_path)
    logger = get_logger("nocturne.test.idempotent")

    logger.info("once only")
    logging.shutdown()

    content = (tmp_path / "nocturne.log").read_text(encoding="utf-8")
    assert content.count("once only") == 1


def test_timed_records_elapsed_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter([1.0, 1.5])
    monkeypatch.setattr("time.perf_counter", lambda: next(values))

    with timed("op") as timer:
        pass

    assert timer.elapsed_ms == 500.0


def test_timed_does_not_suppress_exceptions() -> None:
    with pytest.raises(ValueError):
        with timed("x"):
            raise ValueError("boom")
