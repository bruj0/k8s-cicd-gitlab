"""Phase 2 pipeline orchestrator.

Drives the 7-step install sequence with strict ordering. Every step is
idempotent so re-runs are safe. The pipeline also has a `--check`-mode
(pre-flight only) used by `bootstrap.py --phase 2 --check` to report
status without modifying anything.

Step order matters:

    1. Pre-flight     cluster reachable, helm/kubectl on PATH
    2. TLS Secret     publish the Phase 1 wildcard cert into each namespace
    3. Gateway CRDs   install kubernetes-sigs/gateway-api CRDs (GatewayClass,
                       Gateway, HTTPRoute)
    4. Traefik        reverse proxy (uses CRDs from step 3)
    5. OpenBao        install + init + unseal
    6. Gateway+HTTPRoutes   apply GatewayClass, Gateway, HTTPRoutes
    7. GitLab         install + capture initial creds into OpenBao
    8. GitLab Runner  install using the runner registration token from OpenBao

Each step delegates to a single installer so the pipeline stays
declarative. The pipeline owns ordering and error reporting, not the
install details.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..app_installer import HelmAppInstaller, AppPrepResult
from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner
from .cert import WildcardCertInstaller
from .gateway import GatewayApplier


@dataclass
class Phase2Pipeline:
    """Orchestrates the 7 Phase 2 install steps.

    Single responsibility: order the installers. No business logic about
    how to install a chart (lives in the installer); no logic about how
    to set up OpenBao (lives in OpenBaoInstaller).
    """

    paths: Paths
    runner: CommandRunner
    log: Logger
    installers: "Phase2Installers"   # forward ref; see phase2/catalog.py
    gateway: GatewayApplier
    cert: WildcardCertInstaller

    def run(self) -> int:
        log = self.log
        log.info("")
        log.info("=" * 72)
        log.info("  [bootstrap] Phase 2: install GitLab + Runner + OpenBao + Traefik")
        log.info("=" * 72)
        log.info("")

        try:
            self._step_preflight()
            self._step_cert()
            self._step_gateway_crds()
            self._step_traefik()
            self._step_openbao()
            self._step_gateway()
            self._step_gitlab()
            self._step_runner()
        except Exception as e:
            log.err(f"Phase 2 install failed: {e}")
            log.err("Fix the failing step, then re-run `bootstrap.py --phase 2`.")
            log.err("Every step is idempotent — you can safely re-run.")
            return 1

        log.info("")
        log.info("=" * 72)
        log.info("  [bootstrap] Phase 2 install complete.")
        log.info("=" * 72)
        log.info("")
        log.info("Next steps:")
        log.info("  - Trust the local CA: `sudo trust anchor infra/tls/public/ca.crt`")
        log.info("  - Visit https://gitlab.local.bruj0.net (login: root)")
        log.info("  - Read the initial password from OpenBao:")
        log.info("      kubectl exec -n openbao openbao-0 -- bao kv get -format=json secret/gitlab \\")
        log.info("        | jq -r '.data.data.initial_root_password'")
        log.info("  - See SKILL.md at .agents/skills/provision-gitlab/ for the iteration loop.")
        return 0

    # ---------- pre-flight ----------

    def _step_preflight(self) -> None:
        self.log.info("[bootstrap] Step 1/8  Pre-flight (cluster reachable)")
        self.runner.run(["kubectl", "cluster-info"], check=True)
        self.runner.run(["helm", "version", "--short"], check=True)
        self.log.ok("cluster + helm are reachable")

    # ---------- cert ----------

    def _step_cert(self) -> None:
        self.log.info("[bootstrap] Step 2/8  Publish wildcard TLS Secret in gitlab + openbao namespaces")
        self.cert.publish()
        self.log.ok("TLS Secrets are in place")

    # ---------- gateway api crds ----------

    def _step_gateway_crds(self) -> None:
        # Not part of Traefik's chart; must be installed before any
        # Gateway or HTTPRoute (manifest + Traefik's chart-rendered
        # resources) can land in the cluster.
        self.log.info("[bootstrap] Step 3/8  Install Gateway API CRDs (standard channel)")
        self.gateway.ensure_crds()
        self.log.ok("Gateway API CRDs are installed")

    # ---------- traefik ----------

    def _step_traefik(self) -> None:
        self.log.info("[bootstrap] Step 4/8  Install Traefik (uses Gateway API CRDs)")
        self.installers.traefik.install()
        self.log.ok("Traefik installed")

    # ---------- openbao ----------

    def _step_openbao(self) -> None:
        self.log.info("[bootstrap] Step 5/8  Install + initialise + unseal OpenBao")
        self.installers.openbao.install()
        self.log.ok("OpenBao is initialised and unsealed")

    # ---------- gateway ----------

    def _step_gateway(self) -> None:
        self.log.info("[bootstrap] Step 6/8  Apply Gateway + HTTPRoute manifests")
        self.gateway.apply_all()
        self.log.ok("Gateway + HTTPRoutes applied")

    # ---------- gitlab ----------

    def _step_gitlab(self) -> None:
        self.log.info("[bootstrap] Step 7/8  Install GitLab + capture credentials into OpenBao")
        self.installers.gitlab.install()
        self.log.ok("GitLab installed + credentials captured")

    # ---------- runner ----------

    def _step_runner(self) -> None:
        self.log.info("[bootstrap] Step 8/8  Install GitLab Runner (registers against gitlab.local.bruj0.net)")
        self.installers.runner.install()
        self.log.ok("GitLab Runner installed and registered")