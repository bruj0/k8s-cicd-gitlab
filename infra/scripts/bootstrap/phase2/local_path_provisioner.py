"""local-path StorageClass installer.

Rancher's local-path-provisioner (the upstream project is
`rancher/local-path-provisioner`, the upstream manifest is at
https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.30/deploy/local-path-storage.yaml)
provides a default StorageClass that back-provisions a host
directory per PersistentVolumeClaim. With it installed as the
cluster's default, every GitLab subchart (postgresql, redis,
minio, gitaly, registry, sidekiq, kas, prometheus, …) creates its
data dir on the cluster's single shared hostPath bind
(`/var/local/shared` inside each node, mapped to
`infra/data/shared/` on the developer host by
`infra/tofu/cluster.tf`).

Without this StorageClass, chart PVCs stay `Pending` forever
because the chart defaults `storageClass: ""` and a fresh kind
cluster has no default StorageClass. With it, `tofu destroy &&
tofu apply` keeps every service's data because it lives on the
host filesystem, not inside the ephemeral kind containers.

This installer is intentionally minimal:

  1. Applies the upstream `rancher/local-path-provisioner`
     manifest at the pinned tag (from
     `VERSIONS.json[helm_repositories][local-path-provisioner].source`).
     Idempotent: kubectl apply is a no-op when the resources exist.
  2. Patches the resulting `local-path` StorageClass with
     `storageclass.kubernetes.io/is-default-class=true` so any
     PVC that omits a storageClassName lands here. Idempotent.
  3. Patches the `local-path-config` ConfigMap so the provisioner
     writes PVs to `/var/local/shared` instead of the default
     `/opt/local-path-provisioner`. CRITICAL: the default is
     inside the kind container, so without this patch PVCs do NOT
     survive cluster recreate — which defeats the whole point.
     Also overrides the teardown script: the upstream default
     (`rm -rf "$VOL_DIR"`) DELETES the data when the PVC is
     removed — including implicit removal during `tofu
     destroy` — so we replace it with a `mv` to
     `<name>.preserved-<epoch>` that orphans the dir instead of
     wiping it. Cleanup of stale `*.preserved-*` dirs is left to
     the user (or a future prune phase).
  4. Restarts the `local-path-provisioner` Deployment so the new
     config is loaded (the DS only reads the configmap at
     startup).

Re-run: idempotent. Tolerates an existing install + an already-
default SC + a configmap that's already pointing at
/var/local/shared (kubectl patch is a no-op when the data is
already in the desired state).
"""

from __future__ import annotations

import json

from .. import versions
from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, DryRunRunner


class LocalPathProvisionerInstaller:
    """Install rancher/local-path-provisioner + mark `local-path` as default SC."""

    def __init__(
        self,
        runner: CommandRunner,
        paths: Paths,
        log: Logger,
    ) -> None:
        self._r = runner
        self._paths = paths
        self._log = log

    def install(self) -> None:
        if isinstance(self._r, DryRunRunner):
            self._log.info("[dry-run] skipping local-path-provisioner install")
            return

        # 1. Apply the upstream manifest. The pinned URL lives in the
        #    module-level VERSIONS dict (populated by load_versions()),
        #    so bumping the version is one edit to VERSIONS.json.
        #    --force-conflicts is required because re-runs cause a
        #    field-manager conflict on the DaemonSet's pod-template
        #    env (POD_NAMESPACE fieldRef is client-side-applied, so
        #    the server-side apply would otherwise fail on re-runs).
        repo = versions.helm_repo("local-path-provisioner")
        manifest_url = repo["source"]
        self._log.info("Applying local-path-provisioner manifest:")
        self._log.info(f"  {manifest_url}")
        self._r.run(
            [
                "kubectl", "apply", "--server-side",
                "--force-conflicts", "-f", manifest_url,
            ],
            check=True,
        )
        self._log.ok("local-path-provisioner manifest applied")

        # 2. Patch the SC as default. The modern annotation is the only
        #    one k8s >= 1.27 honours; we patch it idempotently.
        patch = json.dumps(
            {
                "metadata": {
                    "annotations": {
                        "storageclass.kubernetes.io/is-default-class": "true",
                    }
                }
            }
        )
        self._r.run(
            [
                "kubectl", "patch", "storageclass", "local-path",
                "--type=merge", "-p", patch,
            ],
            check=True,
        )
        self._log.ok("local-path marked as default StorageClass")

        # 3. Patch the local-path-config ConfigMap so the provisioner
        #    writes PVs to /var/local/shared (the kind hostPath bind)
        #    instead of the default /opt/local-path-provisioner
        #    (which is inside the kind container and is destroyed on
        #    `tofu destroy`). The ConfigMap's `config.json` is JSON
        #    inside a string, so we have to patch the whole JSON
        #    node — a strategic merge patch on data.config.json.
        #
        #    We also override the teardown script. The upstream
        #    default is `rm -rf "$VOL_DIR"`, which DELETES the PV's
        #    data when the PVC is removed — including implicit
        #    removal triggered by `tofu destroy`. We replace it
        #    with a `mv` to <name>.deleted-<epoch>, so the data
        #    is preserved across cluster recreates. Users can
        #    `rm -rf preserved-*` manually when they actually want
        #    to reclaim disk.
        config_patch = json.dumps(
            {
                "data": {
                    "config.json": json.dumps(
                        {
                            "nodePathMap": [
                                {
                                    "node": "DEFAULT_PATH_FOR_NON_LISTED_NODES",
                                    "paths": ["/var/local/shared"],
                                }
                            ]
                        }
                    ),
                    "teardown": (
                        "#!/bin/sh\n"
                        "set -eu\n"
                        "if [ -d \"$VOL_DIR\" ]; then\n"
                        "  mv \"$VOL_DIR\" \"${VOL_DIR}.preserved-$(date +%s)\"\n"
                        "fi\n"
                    ),
                }
            }
        )
        self._r.run(
            [
                "kubectl", "patch", "configmap", "local-path-config",
                "--namespace=local-path-storage",
                "--type=merge", "-p", config_patch,
            ],
            check=True,
        )
        self._log.ok("local-path-config patched (PVs at /var/local/shared, teardown preserves data)")

        # 4. Restart the deployment so the new config is loaded.
        #    The provisioner reads config.json only at process start
        #    (it watches the configmap for *path additions* but not
        #    for the default path), so a rollout is required to make
        #    the new path effective.
        self._r.run(
            [
                "kubectl", "rollout", "restart",
                "deployment/local-path-provisioner",
                "--namespace=local-path-storage",
            ],
            check=True,
        )
        self._log.ok("local-path-provisioner restarted with new config")

        # 5. Block until the new pod is Ready so downstream steps
        #    (PVC provisioning) see the new config.
        self._r.run(
            [
                "kubectl", "rollout", "status",
                "deployment/local-path-provisioner",
                "--namespace=local-path-storage",
                "--timeout=120s",
            ],
            check=True,
        )
        self._log.ok("local-path-provisioner Ready")
