"""Traefik (reverse proxy + Gateway API CRDs).

Installs the official `traefik/traefik` chart with the Gateway API
provider enabled. After install, the Traefik pod exposes:

    - 80  → http redirect to https
    - 443 → TLS termination, routes HTTPRoutes from `gateway.yaml`
    - 9000 (or wherever the chart's dashboard port lands) → Traefik dashboard

`extra_set` here sets the experimental Gateway provider flag — chart
versions before 35 need it; we set both `providers.kubernetesGateway.enabled`
and the legacy `experimental.kubernetesGateway.enabled` to be safe across
chart revisions.
"""

from __future__ import annotations

from ..app_installer import AppPrepResult, HelmAppInstaller, HelmAppSpec, HelmChartCache, UserStep
from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner


class TraefikInstaller(HelmAppInstaller):
    """Traefik reverse proxy with the Gateway API provider enabled."""

    NAMESPACE = "traefik"
    RELEASE = "traefik"
    REPO_KEY = "traefik"

    def __init__(self, runner: CommandRunner, paths: Paths, cache: HelmChartCache, log: Logger) -> None:
        super().__init__(
            runner, paths, cache, log,
            HelmAppSpec(
                repo_key=self.REPO_KEY,
                release=self.RELEASE,
                namespace=self.NAMESPACE,
                wait=False,  # Traefik has no Deployment by default, --wait would hang.
                # All chart flags live in the YAML file (strict-schema
                # friendly). See phase2/references/helm-values-traefik.yaml.
                values_files=(
                    str(paths.phase2_refs_dir / "helm-values-traefik.yaml"),
                ),
            ),
        )

    def user_handoff_steps(self) -> list[UserStep]:
        return [
            UserStep(
                title="Traefik dashboard (port-forward locally — NOT exposed by default):",
                lines=(
                    f"KUBECONFIG={self._paths.tofu_dir}/kubeconfig",
                    "kubectl port-forward --namespace traefik deploy/traefik 9000:9000",
                    "open http://localhost:9000/dashboard/",
                ),
            ),
        ]