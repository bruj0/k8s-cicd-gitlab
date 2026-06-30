"""Phase 2 pipeline orchestrator.

Drives the install sequence with strict ordering. Every step is
idempotent so re-runs are safe. The pipeline also has a `--check`-
mode (pre-flight only) used by `bootstrap.py --phase 2 --check` to
report status without modifying anything.

Step order matters:

    1. Pre-flight       cluster reachable, helm/kubectl on PATH
    2. Gateway CRDs     upstream gateway-api standard channel
                        + chart-shipped EnvoyProxy + ClientTrafficPolicy
                        + gateway-api experimental (TCPRoute, BackendTLSPolicy)
    3. OpenBao          install + init + unseal
    4. GitLab           chart + chart-managed self-signed wildcard cert
                        (via pre-install Job) + chart-managed Gateway
                        + chart-managed HTTPRoutes (gitlab/registry/kas/minio)
    5. GitLab Runner    install with the runner registration token from OpenBao

The GitLab chart owns the TLS path: with `configureCertmanager:
false` it mints a self-signed wildcard cert for
`*.global.hosts.domain` (default `*.local.bruj0.net`) via a
pre-install Job (`templates/shared-secrets/self-signed-cert-job.yml`)
and stores it in Secret `gitlab-wildcard-tls`. The bootstrap does
NOT install cert-manager or a custom CA chart — the chart handles
it. The only TLS-related work we do is override the chart's
Gateway listener `certificateRefs[0].name` from the cert-manager
default (`gitlab-tls`) to the self-signed-cert default
(`gitlab-wildcard-tls`).

Each step delegates to a single installer so the pipeline stays
declarative. The pipeline owns ordering and error reporting, not the
install details.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner
from .catalog import Phase2Installers


@dataclass
class Phase2Pipeline:
    """Orchestrates the Phase 2 install steps."""

    paths: Paths
    runner: CommandRunner
    log: Logger
    installers: Phase2Installers

    def run(self) -> int:
        log = self.log
        log.info("")
        log.info("=" * 72)
        log.info("  [bootstrap] Phase 2: install GitLab + Runner + OpenBao")
        log.info("    (chart-managed self-signed wildcard cert +")
        log.info("     chart-managed Gateway + Envoy sub-chart)")
        log.info("=" * 72)
        log.info("")

        try:
            self._step_preflight()
            self._step_gateway_crds()
            self._step_openbao()
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
        log.info("  - Add local.bruj0.net → 127.0.0.1 to /etc/hosts (or your DNS):")
        log.info("      127.0.0.1   gitlab.local.bruj0.net registry.local.bruj0.net \\")
        log.info("                 kas.local.bruj0.net minio.local.bruj0.net")
        log.info("  - Trust the chart's self-signed CA on your host:")
        log.info("      kubectl get secret -n gitlab gitlab-wildcard-tls-ca \\")
        log.info("        -o jsonpath='{.data.cfssl_ca}' | base64 -d > infra/tls/public/ca.crt")
        log.info("      sudo trust anchor infra/tls/public/ca.crt")
        log.info("  - Visit https://gitlab.local.bruj0.net (login: root)")
        log.info("  - Read the initial password from OpenBao:")
        log.info("      uv run python -m bootstrap.secrets_cli read gitlab initial_root_password")
        log.info("      # (hvac auto-port-forwards 127.0.0.1:8200 — no kubectl exec needed)")
        log.info("  - Reach the OpenBao UI (no chart-managed route for it):")
        log.info("      uv run python -m bootstrap.secrets_cli ui")
        log.info("      # (spawns port-forward in the background, prints the URL + root token)")
        log.info("  - See SKILL.md at .agents/skills/provision-gitlab/ for the iteration loop.")
        return 0

    # ---------- pre-flight ----------

    def _step_preflight(self) -> None:
        self.log.info("[bootstrap] Step 1/5  Pre-flight (cluster reachable)")
        self.runner.run(["kubectl", "cluster-info"], check=True)
        self.runner.run(["helm", "version", "--short"], check=True)
        self.log.ok("cluster + helm are reachable")

    # ---------- gateway api crds ----------

    def _step_gateway_crds(self) -> None:
        self.log.info(
            "[bootstrap] Step 2/5  Install Gateway API CRDs "
            "(upstream standard + chart-shipped Envoy CRDs)"
        )
        self.installers.crds.install()
        self.log.ok("Gateway API CRDs are installed")

    # ---------- openbao ----------

    def _step_openbao(self) -> None:
        self.log.info("[bootstrap] Step 3/5  Install + initialise + unseal OpenBao")
        self.installers.openbao.install()
        self.log.ok("OpenBao is initialised and unsealed")

    # ---------- gitlab (this is where Envoy + cert-manager come up too) ----------

    def _step_gitlab(self) -> None:
        self.log.info(
            "[bootstrap] Step 4/5  Install GitLab "
            "(bundles Envoy Gateway sub-chart; pre-install Job"
            " mints self-signed wildcard cert for *.local.bruj0.net)"
        )
        self.installers.gitlab.install()
        self.log.ok("GitLab installed + credentials captured")

    # ---------- runner ----------

    def _step_runner(self) -> None:
        self.log.info(
            "[bootstrap] Step 5/5  Install GitLab Runner "
            "(registers against in-cluster Service DNS)"
        )
        self.installers.runner.install()
        self.log.ok("GitLab Runner installed and registered")