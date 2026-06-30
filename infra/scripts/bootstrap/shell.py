"""Command-runner abstraction.

Wraps subprocess so the rest of the package can be tested without
actually exec'ing. Every other class talks to a `CommandRunner`,
not to `subprocess.run` directly.

Important: we never invoke a shell. Every command is a list[str] passed
directly to subprocess.run. This is the spec rule "no shell scripts"
made structural — `cmd | cmd` is impossible at this layer.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import IO, Optional, Protocol, Union

from .logger import ConsoleLogger, Logger

StdinSource = Union[str, bytes, IO[bytes], None]


class CommandRunner(Protocol):
    def run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        env: dict | None = None,
        stdin: StdinSource = None,
        cwd: Optional[str] = None,
    ) -> "CommandResult": ...


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

    def run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        env: dict | None = None,
        stdin: StdinSource = None,
        cwd: Optional[str] = None,
    ) -> CommandResult:
        self._log.info(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
        # Resolve stdin source: None / str / bytes / IO all become the
        # `input=` argument. Passing text/bytes means we don't depend on
        # the subprocess seeing a real pipe.
        input_arg: Optional[bytes]
        if stdin is None:
            input_arg = None
        elif isinstance(stdin, (bytes, bytearray)):
            input_arg = bytes(stdin)
        elif isinstance(stdin, str):
            input_arg = stdin.encode("utf-8")
        else:
            # File-like (BinaryIO/BufferedReader) — drain it.
            cp = subprocess.run(
                cmd,
                input=stdin.read(),
                capture_output=True,
                env={**os.environ, **(env or {})},
                check=False,
                cwd=cwd,
            )
            return self._finalise(cmd, check, cp)

        cp = subprocess.run(
            cmd,
            input=input_arg,
            capture_output=True,
            env={**os.environ, **(env or {})},
            check=False,
            cwd=cwd,
        )
        return self._finalise(cmd, check, cp)

    def _finalise(self, cmd: list[str], check: bool, cp: "subprocess.CompletedProcess[str]") -> CommandResult:
        # Force-decode stdout/stderr to str so all consumers (JSON.parse,
        # strip/lower/split, etc.) can treat it the same way as the
        # DryRunRunner's empty-string contract.
        def _decode(b: object) -> str:
            if isinstance(b, (bytes, bytearray)):
                return b.decode("utf-8", errors="replace")
            return str(b or "")
        result = CommandResult(cp.returncode, _decode(cp.stdout), _decode(cp.stderr))
        if check and not result.ok:
            raise CommandFailed(cmd, result)
        return result


class CommandFailed(RuntimeError):
    def __init__(self, cmd: list[str], result: CommandResult) -> None:
        super().__init__(
            f"command failed (rc={result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}\n"
            f"stdout: {result.stdout.strip()[:400]}"
        )
        self.cmd = cmd
        self.result = result


class DryRunRunner:
    """Logs each command but does not execute. Used when --dry-run is set."""

    def __init__(self, log: Logger) -> None:
        self._log = log

    def run(
        self,
        cmd: list[str],
        *,
        check: bool = True,
        env: dict | None = None,
        stdin: StdinSource = None,
        cwd: Optional[str] = None,
    ) -> CommandResult:
        self._log.info(f"[dry-run] $ {' '.join(shlex.quote(c) for c in cmd)}")
        return CommandResult(0, "", "")