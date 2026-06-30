"""Phase 2 installer bundle (composition helper, no logic)."""

from __future__ import annotations

from dataclasses import dataclass

from ..app_installer import HeadlampInstaller, HelmAppInstaller
from .cert import WildcardCertInstaller
from .gitlab import GitlabInstaller
from .openbao import OpenBaoInstaller
from .runner import GitLabRunnerInstaller
from .traefik import TraefikInstaller


@dataclass(frozen=True)
class Phase2Installers:
    """All Phase 2 installers wired together. Composition-root data class.

    Mirrors the way Phase 1's `BootstrapApp` holds its helper classes —
    one place that knows every collaborator, no logic of its own.
    """

    cert: WildcardCertInstaller
    traefik: TraefikInstaller
    openbao: OpenBaoInstaller
    gitlab: GitlabInstaller
    runner: GitLabRunnerInstaller

    def all(self) -> tuple[HelmAppInstaller, ...]:
        """Tuple form for generic iteration (smoke tests, status reports)."""
        return (self.traefik, self.openbao, self.gitlab, self.runner)