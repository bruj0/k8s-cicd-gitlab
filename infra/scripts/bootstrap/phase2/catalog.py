"""Phase 2 installer bundle (composition helper, no logic)."""

from __future__ import annotations

from dataclasses import dataclass

from ..app_installer import HelmAppInstaller
from .gateway import GatewayCRDsInstaller
from .gitlab import GitlabInstaller
from .openbao import OpenBaoInstaller
from .runner import GitLabRunnerInstaller


@dataclass(frozen=True)
class Phase2Installers:
    """All Phase 2 installers wired together. Composition-root data class.

    The reverse-proxy role is no longer a standalone installer: Phase 2
    delegates it to the GitLab chart's `gateway-helm` sub-chart (Envoy).
    The TLS path is also owned by the chart — its pre-install Job
    (`templates/shared-secrets/self-signed-cert-job.yml`) mints a
    wildcard cert for `*.global.hosts.domain` when
    `configureCertmanager: false`. We only install:

      - the upstream Gateway API CRDs (chart doesn't ship them)
      - OpenBao (KV secret store + init/unseal)
      - GitLab (the big chart + Envoy sub-chart + chart-managed
        Gateway + HTTPRoutes + chart-managed self-signed cert)
      - GitLab Runner (registers against GitLab)
    """

    crds: GatewayCRDsInstaller
    openbao: OpenBaoInstaller
    gitlab: GitlabInstaller
    runner: GitLabRunnerInstaller

    def all(self) -> tuple[HelmAppInstaller, ...]:
        """Tuple form for generic iteration (smoke tests, status reports)."""
        return (self.openbao, self.gitlab, self.runner)