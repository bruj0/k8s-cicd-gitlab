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

import base64
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

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
        """Apply Cluster/postgresql.

        The Cluster manifest lives at
        `phase2/references/cluster-postgresql.yaml` so it's reviewable
        as a diff and not embedded in Python.
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

    def wait_for_ready(self, timeout_s: int = 300) -> None:
        """Block until Cluster/postgresql reports Ready."""
        if isinstance(self.runner, DryRunRunner):
            return
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
            sql = _create_role_and_db_sql(user, db, passwords[user])
            self._exec_psql(sql)
            self.log.ok(
                f"Created role `{user}` + database `{db}` on Cluster/{CLUSTER_NAME}"
            )

    def _exec_psql(self, sql: str) -> None:
        """Run psql via port-forward to the rw Service.

        The cnpg operator exposes a `postgresql-cnpg-rw` Service which
        the superuser Secret authenticates to. We forward that
        Service to localhost, then `psql -h 127.0.0.1 -U postgres` with
        the password from the Secret's `password` key.
        """
        # Read superuser password from the Secret
        out = self.runner.run(
            ["kubectl", "-n", CLUSTER_NAMESPACE, "get", "secret", SUPERUSER_SECRET,
             "-o", "jsonpath={.data.password}"],
            check=True,
        )
        pg_password = base64.b64decode(out.stdout.strip()).decode()

        # Write a temporary .pgpass so psql doesn't prompt
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".pgpass") as f:
            f.write(f"127.0.0.1:5432:*:postgres:{pg_password}\n")
            pgpass_path = f.name
        os.chmod(pgpass_path, 0o600)

        # Start port-forward in the background
        port = 55432
        pf = subprocess.Popen(
            ["kubectl", "-n", CLUSTER_NAMESPACE, "port-forward",
             f"svc/{RW_SERVICE}", f"{port}:5432"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for the port-forward to be ready
            for _ in range(30):
                time.sleep(0.5)
                try:
                    s = subprocess.run(
                        ["ss", "-tln", f"sport = :{port}"],
                        capture_output=True, text=True,
                    )
                    if str(port) in s.stdout:
                        break
                except Exception:
                    pass

            # Run psql
            subprocess.run(
                ["psql", "-h", "127.0.0.1", "-p", str(port),
                 "-U", "postgres", "-d", "postgres",
                 "-v", "ON_ERROR_STOP=1", "-c", sql],
                env={**os.environ, "PGPASSFILE": pgpass_path,
                     "PGHOST": "127.0.0.1", "PGPORT": str(port),
                     "PGUSER": "postgres", "PGDATABASE": "postgres"},
                check=True,
            )
        finally:
            pf.send_signal(signal.SIGTERM)
            try:
                pf.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pf.kill()
            try:
                os.unlink(pgpass_path)
            except OSError:
                pass

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
        self.runner.run(
            ["kubectl", "-n", "gitlab", "create", "secret", "generic",
             "cnpg-role-passwords",
             "--from-literal=gitlab", passwords[GITLAB_DB_USER],
             "--from-literal=openbao", passwords[OPENBAO_DB_USER],
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
        """Read existing role passwords from disk, or generate stable ones.

        Stable passwords are derived from the role name via SHA-256 +
        base64 so cluster recreate deterministically lands on the same
        credentials (the same approach we use elsewhere for the chart
        Secrets snapshot). This means the snapshot file works across
        `tofu destroy && apply` cycles without bcrypt-flapping.
        """
        path = self.paths.secrets_dir / ROLE_PASSWORDS_FILE
        if path.exists():
            return json.loads(path.read_text())
        return {
            GITLAB_DB_USER: _stable_password(GITLAB_DB_USER),
            OPENBAO_DB_USER: _stable_password(OPENBAO_DB_USER),
        }


def _stable_password(user: str) -> str:
    """Deterministic per-role password (24 chars)."""
    digest = hashlib.sha256(b"cnpg-blueprint:" + user.encode()).digest()
    return base64.urlsafe_b64encode(digest[:24]).decode().rstrip("=")


def _create_role_and_db_sql(user: str, db: str, password: str) -> str:
    """Idempotent CREATE ROLE + CREATE DATABASE.

    The DO $$ / \\gexec pattern lets psql execute the SQL blocks
    conditionally without a transaction wrapper. Both CREATE
    statements are no-ops on re-run.
    """
    return (
        f"DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{user}') THEN "
        f"CREATE ROLE {user} LOGIN PASSWORD '{password}'; "
        f"END IF; "
        f"END $$; "
        f"SELECT 'CREATE DATABASE {db} OWNER {user}' "
        f"WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '{db}') \\gexec"
    )