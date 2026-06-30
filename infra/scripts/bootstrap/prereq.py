"""Host prereq tool layer.

Each tool is a small class that knows:
  - which binary candidates to look for
  - which `--version`-like flag prints its version (some CLIs differ)
  - the apt/pacman/dnf/brew package name to install it (resolved from VERSIONS.json)

The `PrereqRegistry` aggregates them and the orchestrator just calls
`registry.ensure_all()` / `registry.report()`. To add a new tool, drop
another subclass into this file and register it.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from .installer import Installer
from .shell import CommandRunner
from .versions import VERSIONS, tool_pin


# Per-tool probes: some CLIs (helm v4, openssl) don't accept --version.
_PROBES: dict[str, list[str]] = {
    "docker": ["docker", "--version"],
    "kubectl": ["kubectl", "version", "--client=true", "--output=yaml"],
    "kind": ["kind", "--version"],
    "helm": ["helm", "version"],
    "opentofu": ["tofu", "--version"],
    "openssl": ["openssl", "version"],
}


@dataclass(frozen=True)
class PrereqReport:
    name: str
    ok: bool
    binary: str | None
    version: str | None


class PrereqTool(ABC):
    """Abstract base for every prereq. Subclasses are tiny and replaceable."""

    name: ClassVar[str] = ""
    candidates: ClassVar[list[str]] = []
    pin_key: ClassVar[str] = ""  # matches `tools.<key>` in VERSIONS.json

    def __init__(self, runner: CommandRunner) -> None:
        self._r = runner

    # ---- identity --------------------------------------------------------

    @property
    def package_for_current_family(self) -> str:
        """Resolve the package name for the *current* host from VERSIONS.json."""
        family = _current_family_unused_marker()  # see helper below; cleaner impl in subclasses if needed
        # We resolve lazily so that tests don't pin a family.
        from .os_detect import detect_os
        family, _ = detect_os()
        return self._package_for(family)

    def _package_for(self, family: str) -> str:
        pin = tool_pin(self.pin_key or self.name)
        return pin["package_by_family"].get(family, self.name)

    # ---- checks ----------------------------------------------------------

    def binary_path(self) -> str | None:
        for c in self.candidates:
            if shutil.which(c):
                return c
        return None

    def check(self) -> PrereqReport:
        binp = self.binary_path()
        if not binp:
            return PrereqReport(self.name, False, None, None)
        probe = _PROBES.get(self.name, [binp, "--version"])
        cp = self._r.run(probe, check=False)
        text = ((cp.stdout or cp.stderr) or "").strip().splitlines()
        version = text[0] if text else "(no output)"
        return PrereqReport(self.name, True, binp, version)

    # ---- install ---------------------------------------------------------

    def install(self, installer: Installer) -> None:
        pkg = self._package_for(installer.family)
        installer.install(self.name, pkg)


def _current_family_unused_marker() -> str:
    """Marker — real lookup happens lazily inside package_for_current_family."""
    return ""


# ---- concrete tools --------------------------------------------------------


class DockerTool(PrereqTool):
    name = "docker"
    candidates = ["docker"]
    pin_key = "docker"

    def daemon_reachable(self) -> bool:
        cp = self._r.run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False)
        return cp.ok and bool(cp.stdout.strip())


class KubectlTool(PrereqTool):
    name = "kubectl"
    candidates = ["kubectl"]
    pin_key = "kubectl"


class KindTool(PrereqTool):
    name = "kind"
    candidates = ["kind"]
    pin_key = "kind"


class HelmTool(PrereqTool):
    name = "helm"
    candidates = ["helm"]
    pin_key = "helm"


class TofuTool(PrereqTool):
    name = "opentofu"
    candidates = ["tofu", "opentofu"]
    pin_key = "opentofu"


class OpensslTool(PrereqTool):
    name = "openssl"
    candidates = ["openssl"]
    pin_key = "openssl"


# ---- registry --------------------------------------------------------------


@dataclass
class PrereqRegistry:
    """Composition root for prereqs. Each tool class registered exactly once."""

    runner: CommandRunner
    tools: list[PrereqTool] = field(default_factory=list)

    @classmethod
    def default(cls, runner: CommandRunner) -> "PrereqRegistry":
        return cls(
            runner=runner,
            tools=[
                DockerTool(runner),
                KubectlTool(runner),
                KindTool(runner),
                HelmTool(runner),
                TofuTool(runner),
                OpensslTool(runner),
            ],
        )

    def docker(self) -> DockerTool:
        for t in self.tools:
            if isinstance(t, DockerTool):
                return t
        raise RuntimeError("DockerTool not registered")

    def report(self) -> list[PrereqReport]:
        return [t.check() for t in self.tools]

    def ensure_all(self, installer: Installer) -> list[PrereqReport]:
        for t in self.tools:
            r = t.check()
            if not r.ok:
                t.install(installer)
        return self.report()

    def all_ok(self, reports: list[PrereqReport]) -> bool:
        return all(r.ok for r in reports)

    def daemon_ok(self) -> bool:
        return self.docker().daemon_reachable()