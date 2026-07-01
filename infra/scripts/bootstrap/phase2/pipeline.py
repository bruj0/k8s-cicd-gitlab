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
    7. CloudNativePG    install operator + Cluster/postgresql +
                        bootstrap the GitLab (`gitlabhq_production`)
                        and OpenBao (`openbao`) databases. Chart 10.x
                        no longer bundles PostgreSQL, so this is now
                        a first-class install step.
    8. Redis            install single-node bitnami/redis +
                        snapshot the auto-generated password Secret
                        to infra/secrets/. Chart 10.x no longer
                        bundles Redis.
    9. MinIO            install standalone minio + create the
                        GitLab object-store buckets (lfs, artifacts,
                        uploads, packages, backups, terraform-state,
                        ci-secure-files, pages, dependency-proxy,
                        snippets) via in-cluster `mc`. Chart 10.x no
                        longer bundles object storage.
   10. OpenBao          install + init + unseal. The chart-bundled
                        OpenBao subchart in GitLab uses the PostgreSQL
                        cluster we just stood up (via
                        `global.openbao.psql.host`), so the user
                        password is in `infra/secrets/cnpg-role-passwords.json`.
   11. persistent       restore chart-managed Secrets (rails/gitaly/
       secrets          kas passwords, initial-root-password, etc.)
                        from a host-side snapshot, so the on-disk
                        data in the preserved PVs keeps matching
                        the chart's expected credentials. PG/Redis/MinIO
                        passwords are NOT in this snapshot anymore —
                        they live in cnpg-role-passwords.json +
                        redis-password.txt + minio-root-{user,password}.txt.
   12. GitLab           chart + chart-managed Gateway + chart-managed
                        HTTPRoutes (gitlab/registry/kas/minio) +
                        chart-bundled OpenBao subchart connected
                        to the external PG/Redis/MinIO we just
                        stood up.
   13. persistent       snapshot any chart-managed Secrets that aren't
       secrets snapshot  PG/Redis/MinIO credentials (those have their
                        own infra/secrets/ files already) — keeps the
                        `gitlab-runtime-secrets.yaml` snapshot small
                        and accurate.
   14. GitLab Runner    install with the runner registration token from OpenBao

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
        log.info("  [bootstrap] Phase 2: install CloudNativePG + Redis + MinIO +")
        log.info("                 OpenBao + GitLab + Runner (chart 10.x)")
        log.info("    (chart-managed Gateway + Envoy sub-chart +")
        log.info("     bootstrap-minted self-signed wildcard cert +")
        log.info("     local-path StorageClass backed by infra/data/shared/ +")
        log.info("     stable PV/PVC pairs so cluster recreate preserves identity +")
        log.info("     restored chart-managed Secrets so chart-bundled components")
        log.info("     keep matching the on-disk data)")
        log.info("=" * 72)
        log.info("")

        try:
            self._step_preflight()
            self._step_gateway_crds()
            self._step_local_path()
            self._step_stable_storage()
            self._step_cnpg()
            self._step_redis()
            self._step_minio()
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
        self.log.info("[bootstrap] Step 1/13  Pre-flight (cluster reachable)")
        self.runner.run(["kubectl", "cluster-info"], check=True)
        self.runner.run(["helm", "version", "--short"], check=True)
        self.log.ok("cluster + helm are reachable")

    # ---------- gateway api crds ----------

    def _step_gateway_crds(self) -> None:
        self.log.info(
            "[bootstrap] Step 2/13  Install Gateway API CRDs "
            "(upstream standard + chart-shipped Envoy CRDs)"
        )
        self.installers.crds.install()
        self.log.ok("Gateway API CRDs are installed")

    # ---------- local-path provisioner + default SC ----------

    def _step_local_path(self) -> None:
        self.log.info(
            "[bootstrap] Step 3/13  Install rancher/local-path-provisioner "
            "+ mark `local-path` as the default StorageClass + patch "
            "the provisioner to write PVs to /var/local/shared "
            "(chart PVCs land on infra/data/shared/)"
        )
        self.installers.local_path.install()
        self.log.ok("local-path is the cluster's default StorageClass + bound to /var/local/shared")

    # ---------- stable PV/PVC pairs for stateful services ----------

    def _step_stable_storage(self) -> None:
        self.log.info(
            "[bootstrap] Step 4/13  Pre-create stable PV/PVC pairs for "
            "CloudNativePG + Redis + MinIO + OpenBao + Gitaly (so "
            "`tofu destroy && tofu apply` preserves identity — chart "
            "10.x no longer bundles these, so they're pinned here)"
        )
        self.installers.stable_storage.install()
        self.log.ok("Stable PV/PVC pairs are in place (cnpg / redis / minio / openbao / gitaly)")

    # ---------- CloudNativePG (operator + Cluster + DB bootstrap) ----------

    def _step_cnpg(self) -> None:
        self.log.info(
            "[bootstrap] Step 5/13  Install CloudNativePG operator "
            "+ Cluster/postgresql (single instance, 8Gi) + bootstrap "
            "the `gitlabhq_production` + `openbao` databases. "
            "Chart 10.x consumes this via `global.psql.host`."
        )
        self.installers.cnpg.install()
        self.log.ok("CloudNativePG + databases are up (git, openbao roles provisioned)")

    # ---------- Redis (single-node bitnami chart) ----------

    def _step_redis(self) -> None:
        self.log.info(
            "[bootstrap] Step 6/13  Install single-node Redis "
            "(bitnami/redis, architecture=standalone, no replicas, "
            "no Sentinel). Chart 10.x consumes this via "
            "`global.redis.host`. Password snapshot to "
            "infra/secrets/redis-password.txt."
        )
        self.installers.redis.install()
        self.log.ok("Redis is up + password snapshot saved")

    # ---------- MinIO (standalone single-pod minio chart) ----------

    def _step_minio(self) -> None:
        self.log.info(
            "[bootstrap] Step 7/13  Install standalone MinIO "
            "+ create GitLab object-store buckets via in-cluster `mc`. "
            "Chart 10.x consumes this via "
            "`appConfig.object_store.<bucket>.connection`."
        )
        self.installers.minio.install()
        self.log.ok("MinIO is up + 10 GitLab buckets provisioned")

    # ---------- openbao ----------

    def _step_openbao(self) -> None:
        self.log.info(
            "[bootstrap] Step 8/13  Install + initialise + unseal OpenBao "
            "(uses the PostgreSQL cluster we just stood up as its "
            "storage backend)"
        )
        self.installers.openbao.install()
        self.log.ok("OpenBao is initialised and unsealed")

    # ---------- wildcard TLS certs (must precede gitlab so Gateway listeners resolve) ----------

    def _step_wildcard_certs(self) -> None:
        self.log.info(
            "[bootstrap] Step 9/13  Mint self-signed wildcard cert for "
            "*.local.bruj0.net and materialise the four Gateway listener Secrets "
            "(the chart's own self-signed-cert Job is skipped when Gateway API is on)"
        )
        self.installers.wildcard_certs.install()
        self.log.ok("Wildcard cert + Gateway listener Secrets applied to gitlab namespace")

    # ---------- persistent secrets (restore before chart, snapshot after) ----------

    def _step_persistent_secrets_restore(self) -> None:
        self.log.info(
            "[bootstrap] Step 10/13  Restore chart-managed Secrets from the "
            "host-side snapshot (rails/gitaly/kas passwords, initial-root-password, "
            "etc.) so the chart picks up the same credentials as the data already "
            "in the preserved PVs. PG/Redis/MinIO passwords are NOT in this snapshot — "
            "they live in their own infra/secrets/ files."
        )
        self.installers.persistent_secrets.restore()

    # ---------- gitlab (this is where Envoy + cert-manager come up too) ----------

    def _step_gitlab(self) -> None:
        self.log.info(
            "[bootstrap] Step 11/13  Install GitLab "
            "(bundles Envoy Gateway sub-chart + chart-bundled OpenBao "
            "subchart; chart sees the external PG/Redis/MinIO we stood "
            "up + the wildcard cert Secrets we minted)"
        )
        self.installers.gitlab.install()
        self.log.ok("GitLab installed + credentials captured")

    def _step_persistent_secrets_snapshot(self) -> None:
        self.log.info(
            "[bootstrap] Step 12/13  Snapshot chart-managed Secrets to "
            "infra/secrets/gitlab-runtime-secrets.yaml for next cluster recreate"
        )
        self.installers.persistent_secrets.snapshot()

    # ---------- runner ----------

    def _step_runner(self) -> None:
        self.log.info(
            "[bootstrap] Step 13/13  Install GitLab Runner "
            "(registers against in-cluster Service DNS)"
        )
        self.installers.runner.install()
        self.log.ok("GitLab Runner installed and registered")