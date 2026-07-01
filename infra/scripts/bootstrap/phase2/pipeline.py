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
    3. local-path SC    rancher/local-path-provisioner + mark as default SC
                        so chart-managed PVCs (postgres, gitaly, minio,
                        registry, redis, kas, prometheus, …) each get a
                        persistent host directory under infra/data/shared/
    4. stable storage   pre-create PV/PVC pairs at known host paths for
                        the services whose state we want to preserve
                        across `tofu destroy && tofu apply` (OpenBao,
                        PostgreSQL, Gitaly, MinIO, Redis, Prometheus)
    5. OpenBao          install + init + unseal
    6. wildcard certs   mint a self-signed wildcard TLS cert via
                        openssl and materialise the four Gateway
                        listener Secrets (gitlab-wildcard-tls +
                        registry-tls + kas-tls + minio-tls).
                        MUST run before the GitLab chart install so
                        the listener `certificateRefs` resolve.
    7. persistent       restore chart-managed Secrets (postgres/
       secrets          redis/minio/rails/gitaly/kas passwords) from
                        a host-side snapshot, so the on-disk data
                        in the preserved PVs keeps matching the
                        chart's expected credentials. Skipped on
                        fresh installs (no snapshot yet).
    8. GitLab           chart + chart-managed Gateway + chart-managed
                        HTTPRoutes (gitlab/registry/kas/minio).
                        (The chart's own self-signed-cert Job is
                        skipped because its `gitlab.ingress.tls.configured`
                        helper returns "true" when Gateway API is on;
                        we override `global.ingress.tls.secretName` in
                        helm-values to make that explicit, then mint
                        the Secret in step 6.)
    9. GitLab Runner    install with the runner registration token from OpenBao

The GitLab chart would normally own the TLS path: with
`configureCertmanager: false` it mints a self-signed wildcard cert
for `*.global.hosts.domain` (default `*.local.bruj0.net`) via a
pre-install Job (`templates/shared-secrets/self-signed-cert-job.yml`)
and stores it in Secret `gitlab-wildcard-tls`. In practice that Job
is gated by `include "gitlab.ingress.tls.configured"` and we found
no combination of values that reliably returns `!= "true"` while
also keeping the chart's Gateway listener cert-ref sane. So we
mint the cert ourselves and rely on the chart's pre-install
`gitlab-shared-secrets` job for the random-secret materialisation
(Secret/initial-root-password, etc.). The bootstrap does NOT
install cert-manager or a custom CA chart — we only use
`openssl`.

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
        log.info("    (chart-managed Gateway + Envoy sub-chart +")
        log.info("     bootstrap-minted self-signed wildcard cert +")
        log.info("     local-path StorageClass backed by infra/data/shared/ +")
        log.info("     stable PV/PVC pairs so cluster recreate preserves identity +")
        log.info("     restored chart-managed Secrets so PostgreSQL/Redis/MinIO")
        log.info("     keep matching the on-disk data)")
        log.info("=" * 72)
        log.info("")

        try:
            self._step_preflight()
            self._step_gateway_crds()
            self._step_local_path()
            self._step_stable_storage()
            self._step_openbao()
            self._step_wildcard_certs()
            self._step_persistent_secrets_restore()
            self._step_gitlab()
            self._step_persistent_secrets_snapshot()
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
        log.info("  - Trust the self-signed CA on your host:")
        log.info("      cat infra/tls/wildcard/ca.pem | sudo trust anchor --store")
        log.info("      # (or, on macOS: sudo security add-trusted-cert ...")
        log.info("  - Start the GitLab port-forward so https://gitlab.local.bruj0.net works:")
        log.info("      uv run python -m bootstrap.cli --port-forward gitlab &")
        log.info("      # spawns `kubectl port-forward` to the chart-managed Envoy")
        log.info("      # Gateway on 127.0.0.1:8443 → cluster :443; survives until you")
        log.info("      # kill the job. We use 8443 (not 443) because 443 on the host")
        log.info("      # is reserved for kind's own extraPortMappings.")
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
        self.log.info("[bootstrap] Step 1/7  Pre-flight (cluster reachable)")
        self.runner.run(["kubectl", "cluster-info"], check=True)
        self.runner.run(["helm", "version", "--short"], check=True)
        self.log.ok("cluster + helm are reachable")

    # ---------- gateway api crds ----------

    def _step_gateway_crds(self) -> None:
        self.log.info(
            "[bootstrap] Step 2/7  Install Gateway API CRDs "
            "(upstream standard + chart-shipped Envoy CRDs)"
        )
        self.installers.crds.install()
        self.log.ok("Gateway API CRDs are installed")

    # ---------- local-path provisioner + default SC ----------

    def _step_local_path(self) -> None:
        self.log.info(
            "[bootstrap] Step 3/7  Install rancher/local-path-provisioner "
            "+ mark `local-path` as the default StorageClass + patch "
            "the provisioner to write PVs to /var/local/shared "
            "(chart PVCs land on infra/data/shared/)"
        )
        self.installers.local_path.install()
        self.log.ok("local-path is the cluster's default StorageClass + bound to /var/local/shared")

    # ---------- stable PV/PVC pairs for stateful services ----------

    def _step_stable_storage(self) -> None:
        self.log.info(
            "[bootstrap] Step 4/7  Pre-create stable PV/PVC pairs for "
            "OpenBao + GitLab sub-components (so `tofu destroy && "
            "tofu apply` preserves identity — chart subcomponents "
            "are pinned to these via `existingClaim`)"
        )
        self.installers.stable_storage.install()
        self.log.ok("Stable PV/PVC pairs are in place (OpenBao, GitLab subcharts)")

    # ---------- openbao ----------

    def _step_openbao(self) -> None:
        self.log.info("[bootstrap] Step 5/8  Install + initialise + unseal OpenBao")
        self.installers.openbao.install()
        self.log.ok("OpenBao is initialised and unsealed")

    # ---------- wildcard TLS certs (must precede gitlab so Gateway listeners resolve) ----------

    def _step_wildcard_certs(self) -> None:
        self.log.info(
            "[bootstrap] Step 6/9  Mint self-signed wildcard cert for "
            "*.local.bruj0.net and materialise the four Gateway listener Secrets "
            "(the chart's own self-signed-cert Job is skipped when Gateway API is on)"
        )
        self.installers.wildcard_certs.install()
        self.log.ok("Wildcard cert + Gateway listener Secrets applied to gitlab namespace")

    # ---------- persistent secrets (restore before chart, snapshot after) ----------

    def _step_persistent_secrets_restore(self) -> None:
        self.log.info(
            "[bootstrap] Step 7/9  Restore chart-managed Secrets from the "
            "host-side snapshot (postgres/redis/minio/rails/gitaly/kas "
            "passwords) so the chart picks up the same credentials as the "
            "data already in the preserved PVs — without this, PostgreSQL "
            "logs `password authentication failed` after every recreate"
        )
        self.installers.persistent_secrets.restore()

    # ---------- gitlab (this is where Envoy + cert-manager come up too) ----------

    def _step_gitlab(self) -> None:
        self.log.info(
            "[bootstrap] Step 8/9  Install GitLab "
            "(bundles Envoy Gateway sub-chart; Gateway listeners resolve "
            "against the Secrets we just minted; chart sees the restored "
            "Secret data and reuses it instead of re-minting)"
        )
        self.installers.gitlab.install()
        self.log.ok("GitLab installed + credentials captured")

    def _step_persistent_secrets_snapshot(self) -> None:
        self.log.info(
            "[bootstrap] Step 8b/9  Snapshot chart-managed Secrets to "
            "infra/secrets/gitlab-runtime-secrets.yaml for next cluster recreate"
        )
        self.installers.persistent_secrets.snapshot()

    # ---------- runner ----------

    def _step_runner(self) -> None:
        self.log.info(
            "[bootstrap] Step 9/9  Install GitLab Runner "
            "(registers against in-cluster Service DNS)"
        )
        self.installers.runner.install()
        self.log.ok("GitLab Runner installed and registered")