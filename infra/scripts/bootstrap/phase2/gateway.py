"""Gateway API manifest applier.

Phase 2 uses the Gateway API (per spec rule: "Traefik as reverse proxy
with Gateway API support"). Traefik installs its own CRDs (IngressRoute,
Middleware, etc.), but the Gateway API SIG CRDs (`Gateway`,
`GatewayClass`, `HTTPRoute`) are NOT shipped by the Traefik chart — they
come from gateway-api.sigs.k8s.io.

This class is the single point of contact for kubectl-apply on Phase 2
gateway manifests. Each manifest lives at
`infra/scripts/bootstrap/phase2/references/` and is referenced by name
in the apply order declared below.
"""

from __future__ import annotations

from pathlib import Path

from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner


# Apply order matters: GatewayClass must exist before Gateway, Gateway
# before HTTPRoute. Each entry is a file name relative to the references
# directory.
MANIFESTS: tuple[str, ...] = (
    "gateway.yaml",           # GatewayClass + Gateway for *.local.bruj0.net
    "httproute-gitlab.yaml",  # gitlab.local.bruj0.net  → gitlab-webservice
    "httproute-openbao.yaml", # openbao.local.bruj0.net → openbao-ui
)


# Standard channel for `Gateway`/`HTTPRoute` (the GA channel).
# We pin the channel explicitly so the install is reproducible.
GATEWAY_API_STANDARD_URL = (
    "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml"
)


class GatewayApplier:
    """Apply every Gateway API manifest in MANIFESTS, idempotently."""

    def __init__(self, runner: CommandRunner, paths: Paths, log: Logger) -> None:
        self._r = runner
        self._paths = paths
        self._log = log

    def apply_all(self) -> list[Path]:
        """Apply every manifest. Returns the list of applied file paths."""
        refs_dir = self._paths.phase2_refs_dir
        applied: list[Path] = []
        for name in MANIFESTS:
            path = refs_dir / name
            if not path.exists():
                raise FileNotFoundError(
                    f"Phase 2 reference manifest not found: {path}. "
                    f"Did you forget to commit the YAML under "
                    f"`infra/scripts/bootstrap/phase2/references/`?"
                )
            self._r.run(["kubectl", "apply", "-f", str(path)])
            self._log.ok(f"Applied Gateway manifest: {name}")
            applied.append(path)
        return applied

    def ensure_crds(self) -> None:
        """Install the upstream Gateway API CRDs (idempotent).

        The Traefik chart does NOT ship the kubernetes-sigs Gateway API
        CRDs (`Gateway`, `GatewayClass`, `HTTPRoute`). They must be
        installed upstream, otherwise `kubectl apply` of any Gateway or
        HTTPRoute fails with `no matches for kind "GatewayClass" in
        version "gateway.networking.k8s.io/v1"`.

        We apply the "standard" channel directly from the upstream
        release URL. `kubectl apply` is idempotent so re-runs are safe.
        """
        # Skip the network fetch in dry-run — the URL would otherwise be
        # printed and confusing.
        from ..shell import DryRunRunner
        if isinstance(self._r, DryRunRunner):
            self._log.info("[dry-run] skipping Gateway API CRD install")
            return
        self._log.info("Installing Gateway API CRDs (standard channel v1.2.1)")
        # `kubectl apply --server-side` ensures CRDs land cleanly with the
        # spec.retain annotation; the URL is fetched by kubectl itself.
        self._r.run(["kubectl", "apply", "--server-side", "-f", GATEWAY_API_STANDARD_URL], check=True)
        self._log.ok("Gateway API CRDs are installed")


class GatewayCRDsInstaller:
    """Install the Gateway API CRDs."""

    def __init__(self, runner: CommandRunner, paths: Paths, log: Logger) -> None:
        self._r = runner
        self._paths = paths
        self._log = log

    def install(self) -> list[Path]:
        """Install the Gateway API CRDs."""
        self._r.run(["kubectl", "apply", "-f", GATEWAY_API_STANDARD_URL])
        self._log.ok("Installed Gateway API CRDs")
        return [Path(GATEWAY_API_STANDARD_URL)]