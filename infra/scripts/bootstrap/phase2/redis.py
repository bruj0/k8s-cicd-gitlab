"""Redis — install single-node + auth Secret for chart 10.x's external Redis.

GitLab chart 10.x consumes an *external* Redis via `global.redis.host`
+ `global.redis.auth.secret`. We deploy a single-node Redis (no
Sentinel, no replicas) using the bitnami/redis chart with `architecture:
standalone`.

**Minimum-resource architecture**:
  - Single Redis master pod, no replicas, no Sentinel sidecar.
  - Authentication via auto-generated `redis-password` Secret
    (also written to `infra/secrets/redis-password.txt` so the
    GitLab chart values can reference it).
  - Storage via hostPath PV pinned by `stable_storage.py` so
    cluster recreate preserves the AOF / RDB snapshot.
  - Resources: 250m CPU / 256Mi RAM (Redis is small).

The bitnami chart exposes Service `redis-master` (RW endpoint), which
the GitLab chart consumes via `global.redis.host: redis-master.<ns>.svc:6379`.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..app_installer import HelmAppInstaller, HelmAppSpec
from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, DryRunRunner

REDIS_NAMESPACE = "redis"
REDIS_RELEASE = "redis"
REDIS_MASTER_SERVICE = "redis-master"
REDIS_PASSWORD_SECRET = "redis-password"
REDIS_PASSWORD_FILE = "redis-password.txt"


@dataclass(frozen=True)
class RedisInstaller:
    """Single-node Redis for chart 10.x's external Redis."""

    runner: CommandRunner
    paths: Paths
    log: Logger
    installer: HelmAppInstaller

    def install(self) -> None:
        """Install + wait + snapshot password."""
        self.install_chart()
        self.wait_for_ready()
        self.snapshot_password()

    def install_chart(self) -> None:
        """Install the bitnami/redis chart as a standalone architecture."""
        if isinstance(self.runner, DryRunRunner):
            return
        self.installer.install()
        self.log.ok(f"Redis chart installed (release={REDIS_RELEASE}, ns={REDIS_NAMESPACE})")

    def wait_for_ready(self, timeout_s: int = 180) -> None:
        """Block until redis-master pod is Ready."""
        if isinstance(self.runner, DryRunRunner):
            return
        self.runner.run(
            [
                "kubectl", "-n", REDIS_NAMESPACE,
                "wait", "pod", "-l", "app.kubernetes.io/name=redis,role=master",
                "--for", "condition=Ready", f"--timeout={timeout_s}s",
            ],
            check=True,
        )
        self.log.ok("Redis master pod is Ready")

    def snapshot_password(self) -> None:
        """Copy the auto-generated password Secret to infra/secrets/.

        GitLab chart values reference this password via
        `global.redis.auth.existingSecret: redis-password` (set in
        phase2/references/helm-values-gitlab.yaml). We mirror the
        Secret into infra/secrets/ so the bootstrap's `--destroy`
        command can wipe host-side state cleanly.
        """
        if isinstance(self.runner, DryRunRunner):
            return
        out = self.runner.run(
            ["kubectl", "-n", REDIS_NAMESPACE, "get", "secret", REDIS_PASSWORD_SECRET,
             "-o", "jsonpath={.data.redis-password}"],
            check=True,
        )
        import base64
        password = base64.b64decode(out.stdout.strip()).decode()
        self.paths.ensure_secrets_dir()
        path = self.paths.secrets_dir / REDIS_PASSWORD_FILE
        path.write_text(password + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self.log.ok(f"Persisted Redis password to {path.name} (mode 0600)")


def build_redis_installer(runner: CommandRunner, paths: Paths, cache, log: Logger) -> HelmAppInstaller:
    """Construct the HelmAppInstaller for the Redis chart.

    Uses `architecture: standalone` (single master, no replicas, no
    Sentinel), `auth.enabled: true` (the chart auto-generates a
    `redis-password` Secret), and `master.persistence.size: 4Gi`
    pinned to the hostPath PV `pv-redis-data` that
    `phase2/stable_storage.py` pre-creates (Flavor A
    `existing_claim`).
    """
    spec = HelmAppSpec(
        repo_key="redis",
        release=REDIS_RELEASE,
        namespace=REDIS_NAMESPACE,
        wait=True,
        create_namespace=True,
        values_files=(str(paths.phase2_refs_dir / "helm-values-redis.yaml"),),
    )
    return HelmAppInstaller(runner, paths, cache, log, spec)