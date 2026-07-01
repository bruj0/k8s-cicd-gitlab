"""Phase 2 installer bundle (composition helper, no logic)."""

from __future__ import annotations

from dataclasses import dataclass

from ..app_installer import HelmAppInstaller
from .gateway import GatewayCRDsInstaller
from .gitlab import GitlabInstaller
from .local_path_provisioner import LocalPathProvisionerInstaller
from .openbao import OpenBaoInstaller
from .persistent_secrets import PersistentSecretsInstaller
from .runner import GitLabRunnerInstaller
from .stable_storage import StableStorageInstaller
from .wildcard_certs import WildcardCertsInstaller


@dataclass(frozen=True)
class Phase2Installers:
    """All Phase 2 installers wired together. Composition-root data class.

    The reverse-proxy role is no longer a standalone installer: Phase 2
    delegates it to the GitLab chart's `gateway-helm` sub-chart (Envoy).
    The TLS path is also owned by the chart — its pre-install Job
    (`templates/shared-secrets/self-signed-cert-job.yml`) mints a
    wildcard cert for `*.global.hosts.domain` when
    `configureCertmanager: false`. Persistence is owned by the
    local-path provisioner (this catalog's `local_path` field) wired
    to the host-side `infra/data/shared/` tree via the kind cluster's
    one shared hostPath bind. We install:

      - the upstream Gateway API CRDs (chart doesn't ship them)
      - rancher/local-path-provisioner + set `local-path` as default
        SC (so chart PVCs land on infra/data/shared/)
      - OpenBao (KV secret store + init/unseal)
      - the wildcard TLS cert (we mint it ourselves because the
        chart's self-signed Job is gated on a chart-helper that
        returns "true" in our Gateway-API-on config, so the Job
        is skipped and the listener Secrets are never created)
      - chart-managed Secrets (postgresql/redis/minio/rails/gitaly/kas
        passwords) restored from the host-side snapshot so the
        preserved PVs keep matching the chart's expected credentials
      - GitLab (the big chart + Envoy sub-chart + chart-managed
        Gateway + HTTPRoutes + chart-managed self-signed cert)
      - GitLab Runner (registers against GitLab)
    """

    crds: GatewayCRDsInstaller
    local_path: LocalPathProvisionerInstaller
    stable_storage: StableStorageInstaller
    openbao: OpenBaoInstaller
    wildcard_certs: WildcardCertsInstaller
    persistent_secrets: PersistentSecretsInstaller
    gitlab: GitlabInstaller
    runner: GitLabRunnerInstaller

    def all(self) -> tuple[HelmAppInstaller, ...]:
        """Tuple form for generic iteration (smoke tests, status reports)."""
        return (self.openbao, self.gitlab, self.runner)