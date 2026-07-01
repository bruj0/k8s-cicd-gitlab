"""MinIO — install standalone + bootstrap buckets for chart 10.x's external S3.

GitLab chart 10.x consumes an *external* S3-compatible object store for
lfs / artifacts / uploads / packages / ci-secure-files / terraform-state /
dependency-proxy. We deploy a single-node MinIO (`mode: standalone`) and
create the buckets the chart expects via the MinIO `mc` CLI.

**Minimum-resource architecture**:
  - Single MinIO pod (no distributed mode, no erasure coding).
  - One default bucket per GitLab object_store type; chart consumes
    them via `appConfig.object_store.<bucket>.connection` in
    helm-values-gitlab.yaml.
  - Storage via hostPath PV pinned by `stable_storage.py` so cluster
    recreate preserves bucket data.
  - Resources: 500m CPU / 512Mi RAM.

The minio chart exposes Service `minio` on port 9000 (S3 API) and 9001
(console UI). GitLab chart consumes the S3 endpoint via
`appConfig.object_store.connection: { host: minio.minio.svc:9000, ... }`.
"""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass

from ..app_installer import HelmAppInstaller, HelmAppSpec
from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, DryRunRunner

MINIO_NAMESPACE = "minio"
MINIO_RELEASE = "minio"
MINIO_ROOT_USER_SECRET = "minio-root-user"
MINIO_ROOT_PASSWORD_SECRET = "minio-root-password"
MINIO_API_SERVICE = "minio"
MINIO_API_PORT = 9000
MINIO_CONSOLE_PORT = 9001

# Buckets the GitLab chart expects (per docs/charts/advanced/external-object-storage).
# These names are referenced from helm-values-gitlab.yaml.
GITLAB_BUCKETS = (
    "gitlab-lfs",
    "gitlab-artifacts",
    "gitlab-uploads",
    "gitlab-packages",
    "gitlab-backups",
    "gitlab-terraform-state",
    "gitlab-ci-secure-files",
    "gitlab-pages",
    "gitlab-dependency-proxy",
    "gitlab-snippets",
    "gitlab-registry",
)

ROOT_USER_FILE = "minio-root-user.txt"
ROOT_PASSWORD_FILE = "minio-root-password.txt"


@dataclass(frozen=True)
class MinIOInstaller:
    """Single-node MinIO for chart 10.x's external object storage."""

    runner: CommandRunner
    paths: Paths
    log: Logger
    installer: HelmAppInstaller

    def install(self) -> None:
        """Install + wait + bootstrap buckets + snapshot credentials."""
        self.install_chart()
        self.wait_for_ready()
        self.bootstrap_buckets()
        self.snapshot_credentials()

    def install_chart(self) -> None:
        """Install the minio chart (mode: standalone, single pod)."""
        if isinstance(self.runner, DryRunRunner):
            return
        self.installer.install()
        self.log.ok(
            f"MinIO chart installed (release={MINIO_RELEASE}, ns={MINIO_NAMESPACE})"
        )

    def wait_for_ready(self, timeout_s: int = 180) -> None:
        """Block until MinIO pod is Ready + S3 endpoint responds."""
        if isinstance(self.runner, DryRunRunner):
            return
        self.runner.run(
            [
                "kubectl", "-n", MINIO_NAMESPACE,
                "wait", "pod", "-l", "app=minio",
                "--for", "condition=Ready", f"--timeout={timeout_s}s",
            ],
            check=True,
        )
        self.log.ok("MinIO pod is Ready")

    def bootstrap_buckets(self) -> None:
        """Create the GitLab buckets via mc client (run in-cluster).

        The minio chart v5.4.0 image ships `mc` in the main container,
        so we exec a single shell that aliases the local MinIO and
        creates all buckets idempotently.
        """
        if isinstance(self.runner, DryRunRunner):
            return
        alias = (
            f"http://{MINIO_API_SERVICE}.{MINIO_NAMESPACE}.svc.cluster.local"
            f":{MINIO_API_PORT}"
        )
        # `mc alias set` uses MINIO_ROOT_USER / MINIO_ROOT_PASSWORD from
        # the chart-injected env. The bucket loop uses --ignore-existing
        # so re-running bootstrap is safe.
        script = (
            f"mc alias set local {alias} \"$MINIO_ROOT_USER\" \"$MINIO_ROOT_PASSWORD\""
            + "".join(
                f" && mc mb --ignore-existing local/{b}" for b in GITLAB_BUCKETS
            )
        )
        self.runner.run(
            ["kubectl", "-n", MINIO_NAMESPACE, "exec", "deploy/minio",
             "--", "sh", "-c", script],
            check=True,
        )
        self.log.ok(f"{len(GITLAB_BUCKETS)} GitLab buckets provisioned via mc")

    def snapshot_credentials(self) -> None:
        """Copy the MinIO root credentials to infra/secrets/ + create
        the in-cluster `gitlab-rails-storage` Secret.

        Two-step:
          1. Persist root user + password to infra/secrets/ files
             (mode 0600) so the bootstrap can wipe them on --destroy.
          2. Materialize Secret `gitlab-rails-storage` in the `gitlab`
             namespace. This Secret has a single key `connection`
             containing the S3 connection YAML the GitLab chart
             expects for all per-bucket configs (`lfs`, `artifacts`,
             `uploads`, `packages`, `terraformState`, `ciSecureFiles`,
             `dependencyProxy`, `pages`). The chart's
             `appConfig.object_store.connection.secret` value points
             at this Secret.
        """
        if isinstance(self.runner, DryRunRunner):
            return
        user_out = self.runner.run(
            ["kubectl", "-n", MINIO_NAMESPACE, "get", "secret", MINIO_ROOT_USER_SECRET,
             "-o", "jsonpath={.data.rootUser}"],
            check=True,
        )
        pass_out = self.runner.run(
            ["kubectl", "-n", MINIO_NAMESPACE, "get", "secret", MINIO_ROOT_PASSWORD_SECRET,
             "-o", "jsonpath={.data.rootPassword}"],
            check=True,
        )
        user = base64.b64decode(user_out.stdout.strip()).decode()
        password = base64.b64decode(pass_out.stdout.strip()).decode()
        self.paths.ensure_secrets_dir()
        for filename, content in (
            (ROOT_USER_FILE, user),
            (ROOT_PASSWORD_FILE, password),
        ):
            path = self.paths.secrets_dir / filename
            path.write_text(content + "\n")
            try:
                path.chmod(0o600)
            except OSError:
                pass
        self.log.ok(
            f"Persisted MinIO root credentials to {ROOT_USER_FILE} + {ROOT_PASSWORD_FILE}"
        )

        # Step 2: in-cluster Secret with two keys consumed by chart 10.x:
        #   - `connection`: Rails-format S3 credentials object (parsed
        #     by GitLab rails-side object_store configurations:
        #     lfs/artifacts/uploads/packages/terraform-state/ci-secure-
        #     files/dependency-proxy/pages/backups). Format is the
        #     Fog/AWS provider schema (provider, aws_access_key_id,
        #     aws_secret_access_key, region, endpoint, path_style).
        #   - `config`: Docker-registry-format S3 driver block. The
        #     chart's registry sub-chart mounts this as the
        #     `storage:` block via `registry.storage.key: config` (see
        #     helm-values-gitlab.yaml). It uses Docker registry's
        #     native s3 driver fields (accesskey/secretkey/region/
        #     regionendpoint/secure/v4auth/pathstyle/rootdirectory/
        #     bucket).
        # We keep both keys in one Secret so chart 10.x's two
        # consumers (Rails-side and registry-side) share the same
        # root-credential source.
        self._ensure_gitlab_namespace()
        connection_yaml = (
            f"provider: AWS\n"
            f"aws_access_key_id: {user}\n"
            f"aws_secret_access_key: {password}\n"
            f"region: us-east-1\n"
            f"endpoint: http://{MINIO_API_SERVICE}.{MINIO_NAMESPACE}.svc.cluster.local:{MINIO_API_PORT}\n"
            f"path_style: true\n"
        )
        registry_s3_block = (
            "s3:\n"
            f"  bucket: gitlab-registry\n"
            f"  accesskey: {user}\n"
            f"  secretkey: {password}\n"
            f"  region: us-east-1\n"
            f"  regionendpoint: http://{MINIO_API_SERVICE}.{MINIO_NAMESPACE}.svc.cluster.local:{MINIO_API_PORT}\n"
            f"  secure: false\n"
            f"  v4auth: true\n"
            f"  pathstyle: true\n"
            f"  rootdirectory: /\n"
        )
        # Idempotent: delete (no-op if absent) then create from the
        # fresh MinIO credentials. The Secret is consumed by the
        # GitLab chart install (downstream step) so no race here.
        self.runner.run(
            ["kubectl", "-n", "gitlab", "delete", "secret",
             "gitlab-rails-storage", "--ignore-not-found"],
            check=True,
        )
        self.runner.run(
            ["kubectl", "-n", "gitlab", "create", "secret", "generic",
             "gitlab-rails-storage",
             f"--from-literal=connection={connection_yaml}",
             f"--from-literal=config={registry_s3_block}",
             "--save-config"],
            check=True,
        )
        self.log.ok(
            "Created Secret `gitlab-rails-storage` in namespace `gitlab` "
            "(keys: connection [Rails], config [registry s3], target: minio.minio.svc:9000)"
        )

    def _ensure_gitlab_namespace(self) -> None:
        """Idempotent namespace creation."""
        self.runner.run(
            ["kubectl", "create", "namespace", "gitlab"],
            check=False,
        )


def build_minio_installer(runner: CommandRunner, paths: Paths, cache, log: Logger) -> HelmAppInstaller:
    """Construct the HelmAppInstaller for the MinIO chart.

    Uses `mode: standalone` (single pod, no distributed). Pinned to
    the hostPath PV `pv-minio-data` created by `phase2/stable_storage.py`
    (Flavor A `existing_claim`). Authentication via auto-generated
    Secrets (`minio-root-user`, `minio-root-password`).
    """
    spec = HelmAppSpec(
        repo_key="minio",
        release=MINIO_RELEASE,
        namespace=MINIO_NAMESPACE,
        wait=True,
        create_namespace=True,
        values_files=(str(paths.phase2_refs_dir / "helm-values-minio.yaml"),),
    )
    return HelmAppInstaller(runner, paths, cache, log, spec)