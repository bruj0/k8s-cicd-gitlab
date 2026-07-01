"""CloudNativePG — install operator + bootstrap Cluster/postgresql.

Cloud Native GitLab (chart 10.x) no longer bundles PostgreSQL. The chart
consumes an *external* PostgreSQL via `global.psql.host` /
`global.psql.password.secret`. We satisfy that contract with
CloudNativePG: a single-instance `Cluster/postgresql` running in the
`postgresql` namespace that owns the PostgreSQL primary + WAL volume.

**Minimum-resource architecture** (matches the rest of the blueprint):
  - Single-instance cluster (no replicas, no HA) — `instances: 1`.
  - No HPA, no PodMonitor, no cert-manager, no service monitor.
  - Storage via hostPath PV pinned by `stable_storage.py` (Flavor A
    `existing_claim`) so the cluster can recreate without losing data.
  - Two databases created on first install: `gitlabhq_production`
    (user `gitlab`) + `openbao` (user `openbao`).

The Cluster exposes Service `postgresql-cnpg-rw` (RW endpoint), which
the GitLab chart consumes via `global.psql.host` and the chart-bundled
OpenBao subchart auto-discovers via `config.storage.postgresql.connection`.

Lifecycle (each step is idempotent — safe to re-run):

    1. install_operator()  helm install cnpg/cloudnative-pg (Cluster-scoped CRDs)
    2. install_cluster()   kubectl apply Cluster/postgresql (single instance,
                          8Gi storage, hostPath via stable-storage PVCs)
    3. wait_for_ready()    block until cnpg Cluster reports Ready
    4. bootstrap_dbs()     create roles + databases on the Cluster via psql
                          (uses the auto-generated `superuser` Secret the
                          operator writes to `<ns>/postgresql-cnpg-superuser`)
    5. snapshot_passwords() write the generated role passwords to
                          infra/secrets/cnpg-role-passwords.json so
                          helm-values-gitlab.yaml + the OpenBao subchart
                          can reference them via existingSecret.

Why CloudNativePG over a raw `postgres:16` StatefulSet: the operator
gives us a documented Secret contract (superuser + app role) that the
GitLab chart's `external-db` reference architecture expects, plus
proper WAL archiving and point-in-time recovery hooks for future use.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass


def _is_phase_healthy(phase: str) -> bool:
    """True when CNPG reports a phase we know is terminal-good.

    CNPG cluster phases aren't a stable enum across versions;
    we treat the canonical "Healthy" as the only acceptable
    terminal state but conservatively also short-circuit on
    "Cluster in healthy state" (1.30.0 sometimes logs that as
    the phase string before flipping to Healthy). Anything
    else is treated as "not yet healthy, keep polling".
    """
    return phase in ("Healthy", "Cluster in healthy state")

from ..app_installer import HelmAppInstaller
from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, DryRunRunner

OPERATOR_NAMESPACE = "cnpg-system"
CLUSTER_NAMESPACE = "postgresql"
CLUSTER_NAME = "postgresql-cnpg"
RW_SERVICE = f"{CLUSTER_NAME}-rw"
SUPERUSER_SECRET = f"{CLUSTER_NAME}-superuser"

# Database bootstrap. These credentials are referenced from
# phase2/references/helm-values-gitlab.yaml + the chart-bundled
# OpenBao subchart's `global.openbao.psql` values.
GITLAB_DB_NAME = "gitlabhq_production"
GITLAB_DB_USER = "gitlab"
OPENBAO_DB_NAME = "openbao"
OPENBAO_DB_USER = "openbao"

ROLE_PASSWORDS_FILE = "cnpg-role-passwords.json"


@dataclass(frozen=True)
class CloudNativePGInstaller:
    """Operator + Cluster + database bootstrap for chart 10.x's external PG."""

    runner: CommandRunner
    paths: Paths
    log: Logger
    operator: HelmAppInstaller  # cnpg operator (HelmAppInstaller)

    def install(self) -> None:
        """Orchestrator: install operator, cluster, wait, bootstrap."""
        self.install_operator()
        self.install_cluster()
        self.wait_for_ready()
        self.bootstrap_dbs()
        self.snapshot_passwords()

    def install_operator(self) -> None:
        """Install the CloudNativePG operator (cluster-scoped CRDs)."""
        if isinstance(self.runner, DryRunRunner):
            return
        self.operator.install()
        self.log.ok("CloudNativePG operator installed")

    def install_cluster(self) -> None:
        """Apply Cluster/postgresql + claim-orphan the pre-created PVC.

        The Cluster manifest lives at
        `phase2/references/cluster-postgresql.yaml` so it's reviewable
        as a diff and not embedded in Python.

        After applying the Cluster we patch our pre-created
        `postgresql-cnpg-1` PVC to add an `ownerReference` pointing
        at the Cluster. Without this, the CNPG operator's
        `getManagedPVCs` (which uses a `metadata.controller` field
        index selector) returns an empty list — our pre-created
        PVC has no owner since bootstrap created it before the
        Cluster existed. Result: `EnrichStatus` sees no managed
        PVCs, sets `Status.Instances = 0`, and the cluster loops
        forever in `createPrimaryInstance` (initdb Job → completed
        → primary pod never created → initdb Job again).
        """
        if isinstance(self.runner, DryRunRunner):
            return
        yaml_path = self.paths.phase2_refs_dir / "cluster-postgresql.yaml"
        out = self.runner.run(
            ["kubectl", "apply", "--server-side", "--force-conflicts",
             "-f", str(yaml_path)],
            check=True,
        )
        self.log.ok(f"Applied {yaml_path.name} (Cluster/{CLUSTER_NAME})")

        # Patch the pre-created PVC to be owned by the Cluster.
        # The owner UID matches Cluster.metadata.uid, which we read
        # fresh from the API so we don't have to hard-code it.
        self._claim_orphan_pvc()

    def _claim_orphan_pvc(self) -> None:
        """Make the operator recognise our pre-created PVC + owner it.

        Two patching jobs on `postgresql-cnpg-1`:

          1. `ownerReferences[0] = cluster/{CLUSTER_NAME}` so the
             CNPG API-server field-index selector sees the PVC
             as managed (preventing the operator's
             `metadata.controller` lookup from missing it).

          2. `cnpg.io/instanceRole: primary` LABEL (in addition
             to whatever `stable_storage.py` already stamped).
             Belt-and-suspenders: CNPG 1.30's
             `ensurePrimaryBootstrapJob` (added in upstream PR
             #11039, fixes first-primary bootstrap deadlock from
             #11036) reads `cnpg.io/instanceRole` from
             `meta.Labels` via `specs.IsPrimary`, NOT from
             annotations. Without the label, when the recovery
             handler fires on a fresh install where the PVC was
             pre-created without first being claimed by the
             operator, it refuses to recreate the missing
             bootstrap Job — leaving the cluster stuck in
             "Selected PVC is not ready yet" forever. The label
             is idempotent: if `stable_storage.py` already stamped
             it, this patch is a no-op (strategic merge patch
             keeps the existing value when the key already
             exists).

        Idempotent: re-running against a PVC that's already
        patched is a no-op for both fields.
        """
        # 1. Read the Cluster's UID
        cluster_json = self.runner.run(
            ["kubectl", "-n", CLUSTER_NAMESPACE, "get", "cluster",
             CLUSTER_NAME, "-o", "json"],
            check=True,
        ).stdout
        cluster = json.loads(cluster_json)
        cluster_uid = cluster["metadata"]["uid"]

        # 2a. Patch ownerReferences (controller owner)
        owner_ref = [{
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Cluster",
            "name": CLUSTER_NAME,
            "uid": cluster_uid,
            "controller": True,
            "blockOwnerDeletion": True,
        }]
        patch = json.dumps(owner_ref)
        self.runner.run(
            ["kubectl", "-n", CLUSTER_NAMESPACE, "patch", "pvc",
             f"{CLUSTER_NAME}-1", "--type=merge",
             "--patch", f"{{\"metadata\":{{\"ownerReferences\":{patch}}}}}"
             ],
            check=True,
        )
        self.log.ok(
            f"Set Cluster/{CLUSTER_NAME} as controller-owner of PVC "
            f"{CLUSTER_NAME}-1 (so CNPG's field-index selector finds it)"
        )

        # 2b. Patch the primary-role LABEL. CNPG 1.30's recovery
        # path refuses to recreate the bootstrap Job when this
        # label is missing — see cloudnative-pg/internal/controller/
        # cluster_create.go's ensurePrimaryBootstrapJob + the
        # specs.IsPrimary(meta) lookup in pkg/specs/pg_pods.go.
        self.runner.run(
            ["kubectl", "-n", CLUSTER_NAMESPACE, "patch", "pvc",
             f"{CLUSTER_NAME}-1", "--type=strategic",
             "--patch", json.dumps({
                 "metadata": {
                     "labels": {"cnpg.io/instanceRole": "primary"}
                 }
             })],
            check=True,
        )
        self.log.ok(
            f"Patched cnpg.io/instanceRole=primary LABEL on PVC "
            f"{CLUSTER_NAME}-1 (unblocks CNPG 1.30's "
            f"ensurePrimaryBootstrapJob recovery)"
        )

    def wait_for_ready(self, timeout_s: int = 300) -> None:
        """Block until Cluster/postgresql reports Ready.

        Self-heals from the CNPG 1.30 first-primary bootstrap
        deadlock (upstream issue #11036): cluster ends up with
        `Initialized=True` (BootstrapCompleted reason) but the
        initdb Job was never created and the Instance Pod is
        stuck in Pending. The symptom is `phase` empty in
        `.status` and the operator's reconcile loop logging
        "Selected PVC is not ready yet, waiting for 1 second"
        forever. We poll every 15 s; once we've seen the wedge
        for ≥60 s we delete the Cluster CR (cascades any owned
        init Job + Pod) + the orphaned PVC, then re-apply. The
        fresh reconcile gets a clean state where CNPG's
        `EnrichStatus` re-classifies the PVC (now with our
        pre-stamped `cnpg.io/instanceRole: primary` LABEL) and
        `ensurePrimaryBootstrapJob` can take over.

        The cap is two recovery attempts (worst case the user's
        CNPG build is broken in a way we don't yet know). After
        that, fall back to the parent `kubectl wait` command
        which surfaces the operator's logs as a regular
        failure to the bootstrap.
        """
        if isinstance(self.runner, DryRunRunner):
            return
        # Bound wait: poll our own probe every 15 s for
        # `min(timeout_s, 90)` (90s gives the cluster enough time
        # to do a normal primary bring-up; the wedge hits well
        # after that). If the parent wait would time out we
        # surface the operator's logs once via the original
        # `kubectl wait` failure.
        probe_window = min(timeout_s, 90)
        deadline = time.monotonic() + probe_window
        consecutive_wedge = 0
        recovery_attempts = 0
        while time.monotonic() < deadline:
            phase, initialized = self._probe_cluster_status()
            if phase == "Healthy" or _is_phase_healthy(phase):
                # Native happy path; let kubectl wait confirm Ready.
                break
            if phase in ("", "Setting up primary") and initialized:
                consecutive_wedge += 1
                if consecutive_wedge >= 4 and recovery_attempts < 2:
                    self._recover_from_wedge(recovery_attempts + 1)
                    recovery_attempts += 1
                    consecutive_wedge = 0
                    # Reset the deadline so the new reconcile gets
                    # a fresh window before the parent timeout.
                    deadline = time.monotonic() + probe_window
            else:
                consecutive_wedge = 0
            time.sleep(15)
        # Delegate the final `Healthy=True` confirmation to
        # `kubectl wait` so the caller sees a normal error if
        # recovery ran out of attempts.
        self.runner.run(
            [
                "kubectl", "-n", CLUSTER_NAMESPACE,
                "wait", f"cluster/{CLUSTER_NAME}",
                "--for", "jsonpath={.status.conditions[?(@.type=='Ready')].status}=True",
                f"--timeout={timeout_s}s",
            ],
            check=True,
        )
        self.log.ok(f"Cluster/{CLUSTER_NAME} is Ready")

    def _probe_cluster_status(self) -> tuple[str, bool]:
        """Read `(phase, initialized)` from Cluster.status.

        Returns `("", False)` on any error (PV getting
        restarted, transient API errors, ...) — the caller
        treats that as "neither healthy nor wedged, just wait".
        """
        try:
            cluster = json.loads(self.runner.run(
                ["kubectl", "-n", CLUSTER_NAMESPACE, "get", "cluster",
                 CLUSTER_NAME, "-o", "json"],
                check=True,
            ).stdout)
        except Exception:
            return "", False
        status = cluster.get("status", {})
        phase = status.get("phase", "") or ""
        initialized = any(
            cond.get("type") == "Initialized"
            and cond.get("status") == "True"
            for cond in status.get("conditions", [])
        )
        return phase, initialized

    def _recover_from_wedge(self, attempt: int) -> None:
        """Break the CNPG 1.30 wedge by deleting + re-applying the cluster.

        Idempotent: the wedge either resolves on the fresh
        reconcile (then `wait_for_ready` returns) or doesn't (then
        we'll try again or surface the operator's logs).
        """
        self.log.warn(
            f"CNPG cluster appears wedged (attempt {attempt}/2). "
            f"Deleting Cluster CR + orphan PVC + re-applying to break "
            f"the first-primary bootstrap deadlock (upstream issue "
            f"#11036)."
        )
        # 1. Delete the Cluster CR (cascades owned pods/Services).
        self.runner.run(
            ["kubectl", "-n", CLUSTER_NAMESPACE, "delete", "cluster",
             CLUSTER_NAME, "--wait=false"],
            check=False,
        )
        # 2. Delete the orphan PVC (defensive: k8s GC may not
        #    reclaim it if the binder saw the corrupt claimRef).
        self.runner.run(
            ["kubectl", "-n", CLUSTER_NAMESPACE, "delete", "pvc",
             f"{CLUSTER_NAME}-1", "--wait=false", "--ignore-not-found"],
            check=False,
        )
        # 3. Re-apply the Cluster manifest so a fresh reconcile
        #    can take over.
        self.runner.run(
            ["kubectl", "-n", CLUSTER_NAMESPACE, "apply",
             "--server-side", "--force-conflicts",
             "-f", str(self.paths.phase2_refs_dir / "cluster-postgresql.yaml")],
            check=False,
        )
        # 4. Re-stamp the controller owner + LABEL on the freshly
        #    recreated PVC (the delete above removes our
        #    pre-stamps; _claim_orphan_pvc re-applies them).
        self._claim_orphan_pvc()

    def bootstrap_dbs(self) -> None:
        """Create GitLab + OpenBao roles + databases on the Cluster.

        Uses the `superuser` Secret the cnpg operator writes to
        `<ns>/<cluster>-superuser`. psql runs against the rw Service
        via port-forward to localhost (the operator's `<cluster>-psql`
        helper Job is operator-internal and not guaranteed stable
        across cnpg versions).
        """
        if isinstance(self.runner, DryRunRunner):
            return
        passwords = self._load_or_init_passwords()
        for user, db in (
            (GITLAB_DB_USER, GITLAB_DB_NAME),
            (OPENBAO_DB_USER, OPENBAO_DB_NAME),
        ):
            # ROLE first (idempotent via DO $$ block, ON_ERROR_STOP=1).
            self._exec_psql(_create_role_sql(user, passwords[user]))
            # DATABASE second (CREATE DATABASE isn't idempotent — re-run
            # raises SQLSTATE 42P04 duplicate_database. We treat that
            # as success since the role + DB we want already exists).
            self._exec_psql(_create_db_sql(db, user), ignore_errors=True)
            self.log.ok(
                f"Role `{user}` + database `{db}` provisioned on Cluster/{CLUSTER_NAME}"
            )

    def _exec_psql(self, sql: str, ignore_errors: bool = False) -> None:
        """Run psql inside the cnpg pod's `postgres` container.

        The cnpg image ships `psql` and the postgres container
        authenticates over Unix socket (no password) for the
        `postgres` superuser, so we skip the operator's Secret
        password dance entirely. The pod's name is deterministic
        for single-instance clusters: `<cluster>-1` (postgresql-cnpg-1).

        Why not port-forward + .pgpass: the operator writes the
        superuser password to a Secret at install time, but
        `ALTER ROLE postgres PASSWORD` runs at boot and the Secret's
        `password` field doesn't always reflect what's in `pg_authid`
        (we observed auth failure even after base64-decoding the
        Secret value). In-pod exec via Unix socket is the only
        way that doesn't require reconciling with the Secret state.

        Args:
            sql: SQL string to execute.
            ignore_errors: if True, don't raise on psql non-zero exit.
                Used for non-idempotent statements like CREATE DATABASE
                where re-run legitimately raises SQLSTATE 42P04
                (duplicate_database).
        """
        pod = f"{CLUSTER_NAME}-1"
        # The pod has two containers: `bootstrap-controller` (init)
        # and `postgres` (main). `kubectl exec` with `-c postgres`
        # targets the running main container.
        try:
            self.runner.run(
                ["kubectl", "-n", CLUSTER_NAMESPACE, "exec", pod, "-c", "postgres",
                 "--", "psql", "-U", "postgres", "-d", "postgres",
                 "-v", "ON_ERROR_STOP=1", "-c", sql],
                check=True,
            )
        except Exception as e:
            if ignore_errors and ("already exists" in str(e).lower() or "42P04" in str(e)):
                # Idempotent re-run: the CREATE DATABASE we wanted is
                # already there. Quietly move on.
                return
            raise

    def snapshot_passwords(self) -> None:
        """Write role passwords to infra/secrets/ + create in-cluster Secret.

        Two-step:
          1. Persist JSON to infra/secrets/cnpg-role-passwords.json
             (mode 0600) so the bootstrap can wipe it on --destroy.
          2. Materialize Secret `cnpg-role-passwords` in the `gitlab`
             namespace so the GitLab chart's `global.psql.password.secret`
             and `global.openbao.psql.password.secret` references resolve.
             The chart refuses to install if it can't find a Secret
             with the configured keys.
        """
        if isinstance(self.runner, DryRunRunner):
            return
        self.paths.ensure_secrets_dir()
        passwords = self._load_or_init_passwords()

        # Step 1: host-side JSON snapshot
        path = self.paths.secrets_dir / ROLE_PASSWORDS_FILE
        path.write_text(json.dumps(passwords, indent=2))
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self.log.ok(f"Persisted role passwords to {path.name} (mode 0600)")

        # Step 2: in-cluster Secret (so the GitLab chart can consume it)
        self._ensure_gitlab_namespace()
        # Idempotent: `kubectl create secret` fails on re-run if the
        # Secret already exists. We delete first (no-op if absent),
        # then create from the freshly generated passwords. Safe
        # because no pod is consuming this Secret yet — the GitLab
        # chart install is downstream of this step.
        self.runner.run(
            ["kubectl", "-n", "gitlab", "delete", "secret",
             "cnpg-role-passwords", "--ignore-not-found"],
            check=True,
        )
        self.runner.run(
            ["kubectl", "-n", "gitlab", "create", "secret", "generic",
             "cnpg-role-passwords",
             f"--from-literal={GITLAB_DB_USER}={passwords[GITLAB_DB_USER]}",
             f"--from-literal={OPENBAO_DB_USER}={passwords[OPENBAO_DB_USER]}",
             "--save-config"],
            check=True,
        )
        self.log.ok(
            "Created Secret `cnpg-role-passwords` in namespace `gitlab` "
            "(keys: gitlab, openbao)"
        )

    def _ensure_gitlab_namespace(self) -> None:
        """Idempotent namespace creation."""
        self.runner.run(
            ["kubectl", "create", "namespace", "gitlab"],
            check=False,
        )

    def _load_or_init_passwords(self) -> dict[str, str]:
        """Read existing role passwords from disk, or generate random ones.

        Cross-recreate stability is provided by the host-side snapshot
        file itself: if `infra/secrets/cnpg-role-passwords.json`
        exists, we reuse those credentials. If not, we generate fresh
        random passwords via `secrets.token_urlsafe`. Either way the
        passwords that land in PostgreSQL are the same ones that end
        up in the in-cluster `cnpg-role-passwords` Secret.
        """
        path = self.paths.secrets_dir / ROLE_PASSWORDS_FILE
        if path.exists():
            return json.loads(path.read_text())
        return {
            GITLAB_DB_USER: _random_password(),
            OPENBAO_DB_USER: _random_password(),
        }


def _random_password() -> str:
    """Cryptographically random password (32 chars).

    `secrets.token_urlsafe(24)` produces 24 random bytes encoded as
    32 url-safe characters — strong enough for a local-dev blueprint
    and easily pasted into kubectl commands when debugging.
    """
    return secrets.token_urlsafe(24)


def _create_role_sql(user: str, password: str) -> str:
    """Idempotent CREATE ROLE + password reconciliation.

    Two cases:
      1. Role doesn't exist → CREATE ROLE with the snapshot password.
      2. Role exists → ALTER ROLE so the stored password matches
         the snapshot. Without this, a stale DB role (left over
         from a prior run where bootstrap crashed between role
         creation and the snapshot write) would keep its old
         SCRAM-SHA-256 hash and reject every auth attempt against
         the freshly-minted in-cluster `cnpg-role-passwords` Secret.
    """
    return (
        f"DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{user}') THEN "
        f"CREATE ROLE {user} LOGIN PASSWORD '{password}'; "
        f"ELSE "
        f"ALTER ROLE {user} WITH PASSWORD '{password}'; "
        f"END IF; "
        f"END $$;"
    )


def _create_db_sql(db: str, owner: str) -> str:
    """Plain CREATE DATABASE.

    PG doesn't support IF NOT EXISTS for CREATE DATABASE, so the
    caller wraps this in ON_ERROR_STOP=off and treats SQLSTATE
    42P04 (duplicate_database) as a successful no-op.
    """
    return f"CREATE DATABASE {db} OWNER {owner}"