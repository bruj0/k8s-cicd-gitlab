"""Gateway API CRD installer.

The GitLab chart's `gateway-helm` sub-chart (Envoy Gateway) does NOT
ship the kubernetes-sigs Gateway API CRDs — only its own policy
CRDs (`EnvoyProxy`, `EnvoyPatchPolicy`, `BackendTrafficPolicy`,
…). The upstream CRDs (`Gateway`, `GatewayClass`, `HTTPRoute`,
`ReferenceGrant`, `TCPRoute`) must be installed before any chart
templating runs.

We apply the standard channel from the gateway-api SIG release.
The `TCPRoute` (used by GitLab Shell) is in the experimental
channel — but the chart ships `gatewayapi-crds.yaml` (v1.4.1)
under `charts/gateway-helm/crds/` and references it from a
template only when `installEnvoy: true`. With the experimental
CRDs missing, the chart's helm install fails with:

  resource mapping not found for name: "..." namespace: "..."
  from "": no matches for kind "BackendTrafficPolicy" in version
  "gateway.envoyproxy.io/v1alpha1"
  ensure CRDs are installed first

So we apply both:

  1. upstream gateway-api standard channel (v1.2.1)
  2. the chart's vendored experimental-channel CRDs + Envoy policy
     CRDs (extracted by us into
     infra/scripts/bootstrap/phase2/references/gateway-api-crds/ so
     the install has no network dependency for the CRD set the
     chart already shipped)

`kubectl apply` is idempotent so re-runs are safe.
"""

from __future__ import annotations

from pathlib import Path

from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner


# Standard channel for `Gateway`/`HTTPRoute` (the GA channel).
# Envoy Gateway v1.7.1 (bundled by the GitLab chart) is compatible
# with gateway-api v1.2.x standard. We pin v1.2.1 explicitly for
# reproducibility.
GATEWAY_API_STANDARD_URL = (
    "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml"
)


class GatewayCRDsInstaller:
    """Install upstream + Envoy Gateway API CRDs (idempotent)."""

    def __init__(self, runner: CommandRunner, paths: Paths, log: Logger) -> None:
        self._r = runner
        self._paths = paths
        self._log = log

    def install(self) -> list[Path]:
        """Install standard channel + chart-shipped experimental CRDs.

        Returns the list of installed paths.
        """
        from ..shell import DryRunRunner
        if isinstance(self._r, DryRunRunner):
            self._log.info("[dry-run] skipping Gateway API CRD install")
            return []

        installed: list[Path] = []

        # 1. upstream standard channel
        self._r.run(
            ["kubectl", "apply", "--server-side", "--force-conflicts",
             "-f", GATEWAY_API_STANDARD_URL],
            check=True,
        )
        self._log.ok("Gateway API CRDs (standard channel v1.2.1) applied")
        installed.append(Path(GATEWAY_API_STANDARD_URL))

        # 2. chart-shipped experimental gateway-api CRDs + Envoy policy
        # CRDs. We extract them from the bundled gitlab chart's
        # `gateway-helm/crds/` directory the first time the step
        # runs; subsequent runs reuse the staged files.
        crd_dir = self._ensure_chart_crds_staged()
        crd_files = sorted(crd_dir.glob("*.yaml"))
        if not crd_files:
            raise RuntimeError(
                f"No CRD YAMLs staged under {crd_dir} — chart extraction failed"
            )
        # `--server-side --force-conflicts` lets us take ownership of
        # fields the chart's own CRD install annotated previously
        # (`gateway.networking.k8s.io/bundle-version`,
        # `gateway.networking.k8s.io/channel`, `.spec.versions`).
        # Without --force-conflicts a re-run errors with
        # "conflicts with helm". Safe here because we're replacing
        # the same logical CRDs.
        self._r.run(
            ["kubectl", "apply", "--server-side", "--force-conflicts",
             "-f", str(crd_dir)],
            check=True,
        )
        self._log.ok(
            f"Gateway API CRDs (chart-shipped experimental + Envoy policy, "
            f"{len(crd_files)} files) applied"
        )
        installed.extend(crd_files)

        self._log.ok("Gateway API CRDs are installed")
        return installed

    # ---------- helpers ----------

    def _ensure_chart_crds_staged(self) -> Path:
        """Extract the gitlab chart's gateway-helm CRDs into a stable
        directory the first time and return that directory's path.

        The CRDs ship inside `infra/helm-charts/gitlab-9.11.7.tgz`
        at `gitlab/charts/gateway-helm/crds/{gatewayapi-crds.yaml,
        generated/*.yaml}`. We extract them into
        `infra/scripts/bootstrap/phase2/references/gateway-api-crds/`
        on first run so subsequent runs don't re-tar the chart.

        If the chart tarball moves or the CRD directory already
        exists with content, we just return the staged path.
        """
        stage_dir = (
            self._paths.bootstrap_dir
            / "phase2"
            / "references"
            / "gateway-api-crds"
        )
        if stage_dir.exists() and any(stage_dir.glob("*.yaml")):
            return stage_dir

        # Locate the cached chart.
        chart_tgz = self._paths.helm_charts_dir / "gitlab-9.11.7.tgz"
        if not chart_tgz.exists():
            raise RuntimeError(
                f"GitLab chart not cached at {chart_tgz}. "
                f"Run Phase 2 step 5 once first to cache it."
            )

        stage_dir.mkdir(parents=True, exist_ok=True)
        # Use `tar` to extract only the CRDs we need. This avoids
        # pulling the whole ~150 MB chart onto disk just for CRDs.
        import tarfile
        with tarfile.open(chart_tgz) as tf:
            for member in tf.getmembers():
                p = member.name
                if not p.startswith("gitlab/charts/gateway-helm/crds/"):
                    continue
                if not (p.endswith("gatewayapi-crds.yaml")
                        or "/generated/" in p):
                    continue
                if not p.endswith(".yaml"):
                    continue
                # Strip the leading chart-internal prefix so files
                # land flat under stage_dir.
                base = Path(p).relative_to("gitlab/charts/gateway-helm/crds")
                out = stage_dir / base
                out.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                out.write_bytes(extracted.read())

        if not any(stage_dir.glob("*.yaml")):
            raise RuntimeError(
                f"Extracted zero CRD files into {stage_dir} — "
                f"chart layout may have changed."
            )
        self._log.info(f"Staged Gateway API CRDs into {stage_dir}")
        return stage_dir