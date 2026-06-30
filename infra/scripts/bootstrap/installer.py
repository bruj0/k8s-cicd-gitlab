"""OS-specific installer strategies (Open/Closed + Strategy).

Adding a new distro means adding a new subclass of `Installer` and
registering it in `installer_for()` — the orchestrator never changes.
"""

from __future__ import annotations

from typing import Protocol

from .os_detect import OSFamily
from .shell import CommandRunner


class Installer(Protocol):
    family: OSFamily

    def install(self, tool: str, package: str) -> None: ...


class SudoMixin:
    """Wrap sudo for privileged operations. No-op when already root."""

    @staticmethod
    def _sudo(runner: CommandRunner, argv: list[str]) -> None:
        import os
        if os.geteuid() == 0:
            runner.run(argv)
        else:
            runner.run(["sudo", *argv])


class ArchInstaller:
    family: OSFamily = "arch"

    def __init__(self, runner: CommandRunner) -> None:
        self._r = runner

    def install(self, tool: str, package: str) -> None:
        self._r.run(["sudo", "pacman", "-Syu", "--noconfirm", "--needed", package])


class DebianInstaller:
    family: OSFamily = "debian"

    def __init__(self, runner: CommandRunner) -> None:
        self._r = runner

    def install(self, tool: str, package: str) -> None:
        # For a couple of tools, the apt package name doesn't match the tool name
        # exactly (docker.io vs docker). Caller passes the resolved package.
        self._r.run(["sudo", "apt-get", "update"])
        self._r.run(["sudo", "apt-get", "install", "-y", package])


class RhelInstaller:
    family: OSFamily = "rhel"

    def __init__(self, runner: CommandRunner) -> None:
        self._r = runner

    def install(self, tool: str, package: str) -> None:
        self._r.run(["sudo", "dnf", "-y", "install", package])


class DarwinInstaller:
    family: OSFamily = "darwin"

    def __init__(self, runner: CommandRunner) -> None:
        self._r = runner

    def install(self, tool: str, package: str) -> None:
        # `package` may be a flag-prefixed string like '--cask docker'
        argv = ["brew", "install", *package.split()]
        self._r.run(argv)


def installer_for(family: OSFamily, runner: CommandRunner) -> Installer:
    mapping = {
        "arch": ArchInstaller,
        "debian": DebianInstaller,
        "rhel": RhelInstaller,
        "darwin": DarwinInstaller,
    }
    if family not in mapping:
        raise SystemExit(f"Unsupported OS family: {family!r}. Install tools manually.")
    return mapping[family](runner)