"""Phase 2: provision GitLab + Runner + OpenBao + Traefik into a Phase-1 cluster.

This subpackage is invoked by `BootstrapApp.run()` when `--phase 2` is
passed. Unlike Phase 1 (which only prepares and prints), Phase 2 ACTUALLY
runs `helm install` and `kubectl apply` — the spec rule that protected
Phase 1 from `tofu apply` does not extend to helm/kubectl, and iteration
needs the actual install to run.

Public surface:

    Pipeline orchestration:
        Phase2Pipeline      7-step install orchestrator (called from app.py)
        Phase2Installers    dataclass bundling every installer

    Per-component installers:
        WildcardCertInstaller   republishes Phase 1's CA-signed wildcard
                                cert as kubernetes.io/tls Secrets
        TraefikInstaller        Gateway API reverse proxy
        OpenBaoInstaller        KV secret store + init/unseal
        GitlabInstaller         the big chart (with post-install secret capture)
        GitLabRunnerInstaller   registers against GitLab

    Supporting classes:
        OpenBaoClient      `kubectl exec ... bao ...` wrapper
        GatewayApplier     kubectl apply for Gateway + HTTPRoute YAMLs

The iteration loop (run, observe, fix, repeat) is documented in
`.agents/skills/provision-gitlab/SKILL.md`.
"""

from .catalog import Phase2Installers
from .cert import CertSecret, TLS_NAMESPACES, WildcardCertInstaller
from .gateway import GatewayApplier, MANIFESTS as GATEWAY_MANIFESTS
from .gitlab import GitlabCredentials, GitlabInstaller
from .openbao import OpenBaoInitOutput, OpenBaoInstaller
from .pipeline import Phase2Pipeline
from .runner import GitLabRunnerInstaller
from .secrets import OpenBaoClient
from .traefik import TraefikInstaller

__all__ = [
    "Phase2Installers",
    "Phase2Pipeline",
    "CertSecret",
    "TLS_NAMESPACES",
    "WildcardCertInstaller",
    "GatewayApplier",
    "GATEWAY_MANIFESTS",
    "GitlabCredentials",
    "GitlabInstaller",
    "OpenBaoInitOutput",
    "OpenBaoInstaller",
    "GitLabRunnerInstaller",
    "OpenBaoClient",
    "TraefikInstaller",
]