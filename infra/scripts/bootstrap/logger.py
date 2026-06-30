"""Logging abstraction (Interface Segregation).

The pipeline only needs four severity levels. A small protocol keeps the
other classes decoupled from stdout/colors, so we can swap in a file
logger, a JSON logger, or a no-op logger for tests without touching
anything else.
"""

from __future__ import annotations

import sys
from typing import Protocol


class Logger(Protocol):
    def info(self, msg: str) -> None: ...
    def ok(self, msg: str) -> None: ...
    def warn(self, msg: str) -> None: ...
    def err(self, msg: str) -> None: ...


class ConsoleLogger:
    """ANSI-coloured stdout logger. Default impl."""

    _COLOURS = {"info": "\033[1;34m", "ok": "\033[1;32m", "warn": "\033[1;33m", "err": "\033[1;31m"}
    _RESET = "\033[0m"

    def __init__(self, *, color: bool = True, stream=None) -> None:
        self._color = color and sys.stdout.isatty()
        self._stream = stream or sys.stdout

    def _write(self, level: str, msg: str) -> None:
        prefix = self._COLOURS[level] if self._color else ""
        suffix = self._RESET if self._color else ""
        self._stream.write(f"{prefix}{msg}{suffix}\n")
        self._stream.flush()

    def info(self, msg: str) -> None:
        self._write("info", msg)

    def ok(self, msg: str) -> None:
        self._write("ok", msg)

    def warn(self, msg: str) -> None:
        self._write("warn", msg)

    def err(self, msg: str) -> None:
        self._write("err", msg)


class NullLogger:
    """Test/dev fallback: drop every message."""

    def info(self, msg: str) -> None: ...
    def ok(self, msg: str) -> None: ...
    def warn(self, msg: str) -> None: ...
    def err(self, msg: str) -> None: ...