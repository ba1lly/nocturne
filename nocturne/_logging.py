from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path
from typing import cast, override

SECRET_REGEX = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9._\-]+|sk-[A-Za-z0-9_\-]+|gho_[A-Za-z0-9]+|ghp_[A-Za-z0-9]+|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|xox[bap]-[A-Za-z0-9\-]{30,})"
)

EMOJI_BY_LEVEL = {
    "DEBUG": "🔍",
    "INFO": "🟢",
    "WARNING": "🟡",
    "ERROR": "🔴",
    "CRITICAL": "🚨",
}

_loggers_configured = False
_configured_handlers: list[logging.Handler] = []


def _scrub_text(value: object) -> str:
    return SECRET_REGEX.sub("***", str(value))


def _scrub_arg(value: object) -> object:
    if isinstance(value, str):
        return _scrub_text(value)
    return value


class SensitiveFilter(logging.Filter):
    @override
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _scrub_text(record.msg)

        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_scrub_arg(item) for item in args)
        elif isinstance(args, list):
            items = cast(list[object], args)
            record.args = tuple(_scrub_arg(item) for item in items)

        return True


def discord_formatter(record: logging.LogRecord) -> str:
    emoji = EMOJI_BY_LEVEL.get(record.levelname, "🟢")
    message = f"{emoji} [{record.levelname}] {record.getMessage()}"
    if len(message) > 280:
        return f"{message[:277]}..."
    return message


def _resolve_level(level: str) -> int:
    return getattr(logging, level.upper(), logging.INFO)


def setup_logging(state_dir: Path, level: str = "INFO") -> None:
    global _loggers_configured, _configured_handlers

    state_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    for handler in _configured_handlers:
        if handler in root.handlers:
            root.removeHandler(handler)
        handler.close()
    _configured_handlers = []

    try:
        from rich.logging import RichHandler
    except Exception:
        console_handler: logging.Handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    else:
        console_handler = RichHandler(rich_tracebacks=True, show_time=False, show_level=True, show_path=False)

    console_handler.setLevel(_resolve_level(level))
    console_handler.addFilter(SensitiveFilter())

    file_handler = logging.handlers.RotatingFileHandler(
        state_dir / "nocturne.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    file_handler.addFilter(SensitiveFilter())

    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root.setLevel(_resolve_level(level))

    _configured_handlers = [console_handler, file_handler]
    _loggers_configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class timed:
    def __init__(self, name: str):
        self.name: str = name
        self.elapsed_ms: float = 0.0
        self._t0: float = 0.0

    def __enter__(self) -> "timed":
        self._t0 = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: object | None,
    ) -> None:
        self.elapsed_ms = (time.perf_counter() - self._t0) * 1000
