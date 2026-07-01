"""Gateway API CRD installer.
TODO: somplify this by creating a helm chart that installs the CRDs
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

So we apply:

  1. upstream gateway-api standard channel (v1.5.0) — Gateway,
     HTTPRoute, ReferenceGrant, GRPCRoute, etc. with channel
     `standard` (GA). Anything < v1.5.0 is rejected by the
     `safe-upgrades.gateway.networking.k8s.io`
     ValidatingAdmissionPolicy that the chart's `gateway-helm`
     sub-chart installs (bundle-version regex `v1.[0-4].\\d+`).
  2. The chart's vendored Envoy policy CRDs only
     (`BackendTrafficPolicy`, `EnvoyProxy`, `EnvoyPatchPolicy`,
     `SecurityPolicy`, `EnvoyExtensionPolicy`,
     `ClientTrafficPolicy`, `HTTPRouteFilter`, `Backend`).
     These live in the chart at
     `gitlab/charts/gateway-helm/charts/crds/crds/generated/`
     and are NOT owned by the GitLab chart's Helm release
     (because the chart's `--skip-crds` flag skips the Gateway
     API CRDs but not these Envoy ones). We extract them into
     `infra/scripts/bootstrap/phase2/references/gateway-api-crds/`
     on first run so the install has no network dependency for
     the CRD set the chart already shipped.

We deliberately skip the chart's `gatewayapi-crds.yaml`
(experimental-channel Gateway/HTTPRoute/TCPRoute bundle) —
the chart applies it itself during install. Re-applying it
is blocked by the safe-upgrades VAP (experimental cannot
sit on top of standard).

`kubectl apply` is idempotent so re-runs are safe.
"""

from __future__ import annotations

from pathlib import Path

from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner


# Standard channel for `Gateway`/`HTTPRoute` (the GA channel).
# We pin v1.5.0: anything < v1.5.0 is rejected by the
# `safe-upgrades.gateway.networking.k8s.io` ValidatingAdmissionPolicy
# that the chart's `gateway-helm` subchart installs (bundle-version
# regex `v1.[0-4].\d+` denies older CRDs). v1.5.0 standard is
# API-compatible with the chart-bundled Envoy Gateway v1.7.1.
GATEWAY_API_STANDARD_URL = (
    "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.5.0/standard-install.yaml"
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
        self._log.ok("Gateway API CRDs (standard channel v1.5.0) applied")
        installed.append(Path(GATEWAY_API_STANDARD_URL))

        # 2. Chart-shipped Envoy policy CRDs only. We deliberately
        # skip `gatewayapi-crds.yaml` (the experimental-channel
        # Gateway/HTTPRoute/TCPRoute/etc. bundle that ships in
        # `gitlab/charts/gateway-helm/charts/crds/crds/`): the
        # GitLab chart's `--skip-crds` does NOT cover its
        # `gateway-helm` sub-chart's CRDs, so the chart applies
        # them itself during install — including a
        # `safe-upgrades.gateway.networking.k8s.io`
        # ValidatingAdmissionPolicy that blocks re-applying
        # experimental-channel CRDs on top of our standard-channel
        # upstream CRDs. The Envoy policy CRDs under `generated/`
        # (`BackendTrafficPolicy`, `EnvoyProxy`, …) are NOT owned
        # by the chart's Helm release, so we own them.
        crd_dir = self._ensure_chart_crds_staged()
        crd_files = sorted(p for p in crd_dir.glob("**/*.yaml")
                           if p.name != "gatewayapi-crds.yaml")
        if not crd_files:
            raise RuntimeError(
                f"No Envoy CRD YAMLs staged under {crd_dir} — "
                f"chart extraction failed"
            )
        # `--server-side --force-conflicts` lets us take ownership of
        # fields the chart's own CRD install annotated previously
        # (`gateway.networking.k8s.io/bundle-version`,
        # `gateway.networking.k8s.io/channel`, `.spec.versions`).
        # Without --force-conflicts a re-run errors with
        # "conflicts with helm". Safe here because we're replacing
        # the same logical CRDs.
        self._r.run(
            ["kubectl", "apply", "--server-side", "--force-conflicts"]
            + [arg for p in crd_files for arg in ("-f", str(p))],
            check=True,
        )
        self._log.ok(
            f"Gateway API CRDs (chart-shipped Envoy policy, "
            f"{len(crd_files)} files) applied"
        )
        installed.extend(crd_files)

        self._log.ok("Gateway API CRDs are installed")
        return installed

    # ---------- helpers ----------

    def _ensure_chart_crds_staged(self) -> Path:
        """Extract the gitlab chart's gateway-helm CRDs into a stable
        directory the first time and return that directory's path.

        Chart 10.x ships the upstream Gateway API CRDs at
        `gitlab/charts/gateway-helm/charts/crds/crds/{gatewayapi-crds.yaml,
        generated/*.yaml}`. Chart 9.x used a flatter layout at
        `gitlab/charts/gateway-helm/crds/`. We accept both layouts
        by globbing the chart tarball.

        The extracted files land at
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
        if stage_dir.exists() and any(stage_dir.glob("**/*.yaml")):
            return stage_dir

        # Locate the cached chart (the main gitlab chart, NOT
        # gitlab-runner — the latter is a separate chart with no
        # CRDs). Filter on exact prefix `gitlab-<version>.tgz`.
        chart_tgzs = sorted(
            p for p in self._paths.helm_charts_dir.glob("gitlab-*.tgz")
            if not p.name.startswith("gitlab-runner")
        )
        if not chart_tgzs:
            raise RuntimeError(
                f"No gitlab-*.tgz chart cached in "
                f"{self._paths.helm_charts_dir}. "
                f"Run `bootstrap --phase 2` once to cache it."
            )
        chart_tgz = chart_tgzs[-1]  # newest version
        self._log.info(f"Extracting Gateway API CRDs from {chart_tgz.name}")

        stage_dir.mkdir(parents=True, exist_ok=True)
        # Use `tar` to extract only the CRDs we need. This avoids
        # pulling the whole ~150 MB chart onto disk just for CRDs.
        import tarfile
        with tarfile.open(chart_tgz) as tf:
            for member in tf.getmembers():
                p = member.name
                # Accept both chart 10.x nested layout and chart 9.x
                # flat layout. We just want any yaml under either
                # `gateway-helm/charts/crds/crds/` or
                # `gateway-helm/crds/`.
                if "/crds/" not in p:
                    continue
                if "gateway-helm" not in p:
                    continue
                if not p.endswith(".yaml"):
                    continue
                if "/templates/" in p:
                    continue
                # Strip the chart-internal prefix so files land
                # flat under stage_dir. Both layouts:
                #   gitlab/charts/gateway-helm/charts/crds/crds/<file>
                #   gitlab/charts/gateway-helm/crds/<file>
                # strip the longest common prefix.
                for prefix in (
                    "gitlab/charts/gateway-helm/charts/crds/crds/",
                    "gitlab/charts/gateway-helm/crds/",
                ):
                    if p.startswith(prefix):
                        rel = p[len(prefix):]
                        break
                else:
                    continue
                out = stage_dir / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                out.write_bytes(extracted.read())

        if not any(stage_dir.glob("**/*.yaml")):
            raise RuntimeError(
                f"Extracted zero CRD files into {stage_dir} — "
                f"chart layout may have changed."
            )
        self._log.info(f"Staged Gateway API CRDs into {stage_dir}")
        return stage_dir