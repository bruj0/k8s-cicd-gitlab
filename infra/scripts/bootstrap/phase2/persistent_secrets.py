"""Persist the GitLab chart's runtime secrets to the host filesystem.

Why this exists:
    The GitLab chart's first install mints random passwords for every
    internal service (PostgreSQL, Redis, MinIO, Rails, Gitaly, KAS).
    Those passwords are stored in `Secret/gitlab-*` resources that
    the chart claims via `heritage: Helm`. When `tofu destroy &&
    tofu apply` recreates the cluster, those Secrets disappear — but
    the **on-disk PostgreSQL/Redis/MinIO data is preserved** because
    we use stable hostPath-backed PVs.

    On reinstall, the chart tries to re-mint the same Secrets. The
    new passwords don't match the on-disk data, so PostgreSQL logs
    `FATAL: password authentication failed for user "gitlab"` and
    the GitLab webservice never comes up.

    The fix: snapshot the chart's runtime Secrets to a host-side file
    at the end of every successful install, and re-apply them BEFORE
    the chart install on the next run. The chart sees the Secrets
    already exist, leaves them alone, and the on-disk data keeps
    working with the original password.

What's persisted (all in the `gitlab` namespace):
    gitlab-postgresql-password    postgres + admin passwords
    gitlab-redis-password         redis password
    gitlab-minio-secret           minio accesskey + secretkey
    gitlab-rails-secret           secrets.yml (Rails `secret_key_base`,
                                  `otp_key_base`, `db_key_base`,
                                  `encrypted_settings_key_base`,
                                  `openid_connect_signing_key`)
    gitlab-gitaly-secret          gitaly token
    gitlab-gitlab-kas-secret      kas secret

Why a list (not "all secrets with heritage=Helm"):
    The cluster also runs cert-manager, Envoy Gateway, OpenBao,
    Headlamp, GitLab Runner, etc. — each with its own Secrets. We
    only persist the ones whose **data must match across cluster
    recreates**. cert-manager's CA, OpenBao's root token, the
    GitLab wildcard cert, the OpenBao unseal key are all already
    handled by their own persistence mechanisms (PVs, OpenBao
    auto-init from `infra/secrets/openbao-init.json`, the
    wildcard-cert installer that re-uses its on-disk cert, etc.).

Re-run semantics:
    - install() is idempotent: re-applying an unchanged Secret YAML
      is a no-op; re-applying a changed YAML (because the chart
      rotated a value, or the cluster is fresh) updates the
      Secret with what we have on disk.
    - snapshot() is idempotent: overwrites the host file with the
      current cluster state on every successful install.

Lifecycle:
    Order in the pipeline (Phase 2):
        5. OpenBao install + init + unseal
        6. wildcard cert mint
        7. (this installer) restore chart secrets from host if any
        8. GitLab chart install (the Secrets are already there, the
            chart sees `AlreadyExists` and reuses them).
        9. (after chart install) this installer also writes the
            current state to the host for next time.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, DryRunRunner


# Server-side fields we MUST strip before writing the snapshot —
# they're cluster-assigned and a stale `resourceVersion` will make
# `kubectl apply -f` fail with a conflict on the next restore.
_STRIPPED_METADATA_KEYS: tuple[str, ...] = (
    "resourceVersion",
    "uid",
    "creationTimestamp",
    "selfLink",
    "managedFields",
)


# Secrets the chart generates on first install and that we need to
# persist so subsequent cluster recreates don't rotate the passwords
# out from under the preserved hostPath-backed data.
PERSISTED_GITLAB_SECRETS: tuple[str, ...] = (
    "gitlab-postgresql-password",
    "gitlab-redis-password",
    "gitlab-minio-secret",
    "gitlab-rails-secret",
    "gitlab-gitaly-secret",
    "gitlab-gitlab-kas-secret",
)


@dataclass(frozen=True)
class PersistentSecretsInstaller:
    """Snapshot + restore of GitLab chart-managed Secrets.

    Snapshot goes to `<infra>/secrets/gitlab-runtime-secrets.yaml`
    (one big `List` of all the Secrets we care about) so a single
    `kubectl apply -f` brings the cluster back to the exact prior
    state on the next recreate.
    """

    runner: CommandRunner
    paths: Paths
    log: Logger
    namespace: str = "gitlab"
    secrets: tuple[str, ...] = PERSISTED_GITLAB_SECRETS

    @property
    def snapshot_file(self) -> Path:
        return self.paths.secrets_dir / "gitlab-runtime-secrets.yaml"

    # ---------- public ----------

    def restore(self) -> int:
        """Apply the host-side snapshot to the cluster BEFORE chart install.

        Idempotent: if the snapshot file doesn't exist (fresh install)
        we skip silently — the chart will mint new passwords and
        `snapshot()` will then write them for next time.

        Returns the number of Secrets actually applied (for logging).
        """
        if isinstance(self.runner, DryRunRunner):
            self.log.info("[dry-run] skipping persistent secrets restore")
            return 0
        if not self.snapshot_file.exists():
            self.log.info(
                f"No prior snapshot at {self.snapshot_file} — fresh install, "
                f"chart will mint fresh passwords"
            )
            return 0

        out = self.runner.run(
            ["kubectl", "apply", "-n", self.namespace, "-f", str(self.snapshot_file)],
            check=False,
        )
        if not out.ok:
            self.log.warn(
                f"Failed to restore Secrets from {self.snapshot_file}: "
                f"{out.stderr.strip()}. Continuing — chart may regenerate."
            )
            return 0

        # Count `configured` lines (one per Secret that was created
        # or updated). This is a slightly heuristic metric but it's
        # the right shape for a status log.
        applied = sum(
            1 for line in out.stdout.splitlines()
            if line.endswith(" configured")
            or line.endswith(" created")
            or line.endswith(" unchanged")
        )
        self.log.ok(
            f"Restored {applied} chart-managed Secret(s) from "
            f"{self.snapshot_file.name} (so PostgreSQL/Redis/MinIO data "
            f"in the preserved PVs keeps matching the existing password)"
        )
        return applied

    def snapshot(self) -> None:
        """Capture the current cluster Secrets to a host-side YAML file.

        Called after the chart install + wait-for-webservice, so the
        Secrets are guaranteed to exist (the chart creates them
        before any pod becomes ready).
        """
        if isinstance(self.runner, DryRunRunner):
            self.log.info("[dry-run] skipping persistent secrets snapshot")
            return
        if not self.paths.secrets_dir.exists():
            self.paths.ensure_secrets_dir()

        items: list[str] = []
        for name in self.secrets:
            doc = self.runner.run(
                ["kubectl", "-n", self.namespace, "get", "secret", name, "-o", "yaml"],
                check=False,
            )
            if not doc.ok or not doc.stdout.strip():
                # Not all secrets exist on every chart variant (e.g.
                # Redis password may not be there if the chart
                # rendered with a sidecar). Skip silently.
                continue
            items.append(_strip_server_side_fields(doc.stdout))

        if not items:
            self.log.warn(
                "No chart-managed Secrets found to snapshot — the GitLab "
                "chart may not have rendered the expected subcharts."
            )
            return

        # Concatenate as a multi-doc YAML (k8s-style `---`-separated
        # documents). `kubectl apply -f` accepts this format.
        body = "\n---\n".join(doc.rstrip() for doc in items) + "\n"
        self.snapshot_file.write_text(body)
        # Mode 0600 — these Secrets include the postgres password,
        # minio secretkey, etc. Anyone who can read this file can
        # impersonate the chart's admin users.
        try:
            self.snapshot_file.chmod(0o600)
        except OSError:
            # chmod can fail on Windows / FAT mounts — we did the
            # best we could, don't blow up the install.
            pass
        self.log.ok(
            f"Snapshotted {len(items)} chart-managed Secret(s) to "
            f"{self.snapshot_file} (mode 0600) for next cluster recreate"
        )


def _strip_server_side_fields(yaml_doc: str) -> str:
    """Strip cluster-assigned metadata fields that block `kubectl apply`.

    We do this textually (no PyYAML dependency — the bootstrap venv
    might not have it). `kubectl get -o yaml` always emits the
    fields at a known indentation (one level under `metadata:`)
    and as scalars on a single line. We just drop any line whose
    first non-space characters match a known-strip key + `:`.

    Safe because the Secret data we care about (`data:`) lives at
    indent 2 spaces (deeper than `metadata:` keys at 4 spaces),
    so a key like `name: foo` at indent 4 is never confused with
    a data key.
    """
    out: list[str] = []
    skip = set(_STRIPPED_METADATA_KEYS)
    for line in yaml_doc.splitlines():
        # Determine this line's content after the leading spaces.
        content = line.lstrip(" ")
        indent = len(line) - len(content)
        # Only consider lines that look like `key: value` at indent
        # 4 (typical kubectl output for `metadata:` children). If
        # the key is in our skip list, drop the line.
        if indent == 4 and ":" in content:
            key = content.split(":", 1)[0].strip()
            if key in skip:
                continue
        out.append(line)
    return "\n".join(out)