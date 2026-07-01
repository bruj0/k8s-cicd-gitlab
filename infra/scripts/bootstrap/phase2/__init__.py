"""Phase 2: provision GitLab + Runner + OpenBao into a Phase-1 cluster.

This subpackage is invoked by `BootstrapApp.run()` when `--phase 2` is
passed. Unlike Phase 1 (which only prepares and prints), Phase 2 ACTUALLY
runs `helm install` and `kubectl apply` — the spec rule that protected
Phase 1 from `tofu apply` does not extend to helm/kubectl.

Public surface:

    Pipeline orchestration:
        Phase2Pipeline      orchestrator (called from app.py)
        Phase2Installers    dataclass bundling every installer

    Per-component installers:
        GatewayCRDsInstaller    upstream + Envoy Gateway API CRDs
        OpenBaoInstaller        KV secret store + init/unseal
        GitlabInstaller         the big chart + Envoy sub-chart
                                (with post-install secret capture)
        GitLabRunnerInstaller   registers against GitLab

    Supporting classes:
        OpenBaoClient      hvac-backed client with auto port-forward
                            (replaces the old `kubectl exec ... bao ...` wrapper)

The iteration loop is documented in
`.agents/skills/provision-gitlab/SKILL.md`.
"""

from .catalog import Phase2Installers
from .gateway import GATEWAY_API_STANDARD_URL, GatewayCRDsInstaller
from .gitlab import GitlabCredentials, GitlabInstaller
from .local_path_provisioner import LocalPathProvisionerInstaller
from .openbao import OpenBaoInitOutput, OpenBaoInstaller
from .persistent_secrets import (
    PERSISTED_GITLAB_SECRETS, PersistentSecretsInstaller,
)
from .pipeline import Phase2Pipeline
from .runner import GitLabRunnerInstaller
from .secrets import OpenBaoClient
from .stable_storage import STABLE_VOLUMES, StableStorageInstaller
from .wildcard_certs import (
    WILDCARD_LISTENER_SECRETS, WildcardCertPaths, WildcardCertsInstaller,
)

__all__ = [
    "Phase2Installers",
    "Phase2Pipeline",
    "GatewayCRDsInstaller",
    "GATEWAY_API_STANDARD_URL",
    "LocalPathProvisionerInstaller",
    "STABLE_VOLUMES",
    "StableStorageInstaller",
    "WILDCARD_LISTENER_SECRETS",
    "WildcardCertPaths",
    "WildcardCertsInstaller",
    "PERSISTED_GITLAB_SECRETS",
    "PersistentSecretsInstaller",
    "GitlabCredentials",
    "GitlabInstaller",
    "OpenBaoInitOutput",
    "OpenBaoInstaller",
    "GitLabRunnerInstaller",
    "OpenBaoClient",
]