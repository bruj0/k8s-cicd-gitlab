"""Command-runner abstraction.

Wraps subprocess so the rest of the package can be tested without
actually exec'ing. Every other class talks to a `CommandRunner`,
not to `subprocess.run` directly.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .logger import ConsoleLogger, Logger


class CommandRunner(Protocol):
    def run(self, cmd: list[str], *, check: bool = True, env: dict | None = None) -> "CommandResult": ...


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SubprocessRunner:
    """Production impl: real subprocess, logs each command via the Logger."""

    def __init__(self, log: Logger) -> None:
        self._log = log

    def run(self, cmd: list[str], *, check: bool = True, env: dict | None = None) -> CommandResult:
        self._log.info(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
        cp = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **(env or {})}, check=False)
        result = CommandResult(cp.returncode, cp.stdout, cp.stderr)
        if check and not result.ok:
            raise CommandFailed(cmd, result)
        return result


class CommandFailed(RuntimeError):
    def __init__(self, cmd: list[str], result: CommandResult) -> None:
        super().__init__(f"command failed (rc={result.returncode}): {' '.join(cmd)}\nstderr: {result.stderr.strip()}")
        self.cmd = cmd
        self.result = result


class DryRunRunner:
    """Logs each command but does not execute. Used when --dry-run is set."""

    def __init__(self, log: Logger) -> None:
        self._log = log

    def run(self, cmd: list[str], *, check: bool = True, env: dict | None = None) -> CommandResult:
        self._log.info(f"[dry-run] $ {' '.join(shlex.quote(c) for c in cmd)}")
        return CommandResult(0, "", "")