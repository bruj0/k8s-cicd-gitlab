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
     inside the kind container, so without this patch PVCs would
     be lost on cluster recreate. The teardown script inherits the
     upstream default (`rm -rf "$VOL_DIR"`) — a PVC removal wipes
     its data dir. This matches the new (2026-07) contract that
     `tofu destroy` is a full reset (cluster + cluster-side data
     + PVs go away).

     PRE-2026-07 history: an earlier override patched the teardown
     to `mv` the dir to `<name>.preserved-<ts>` instead of
     deleting it, with the goal of preserving chart-managed data
     across `tofu destroy && apply` cycles. That contract was
     inverted in 2026-07 because the cross-recreate PG password
     mismatch (chart mints fresh random passwords each install,
     stale on-disk data rejects them) made `tofu destroy &&
     apply` non-recoverable unless followed by `bootstrap
     --destroy --yes` to wipe data anyway. The earlier override
     is preserved in git history; flip it back by replacing the
     teardown script with the `mv` form (and combine with a
     snapshot+restore mechanism on `bootstrap --destroy
     --preserve-data`, which would be the corresponding CLI
     toggle if reintroduced).

     See also: `infra/tofu/cluster.tf`'s `null_resource.wipe_data`
     destroy provisioner — that's what guarantees
     `infra/data/shared/stable/*` (the hostPath PV host source
     dirs that local-path-provisioner doesn't know about) also
     go away on `tofu destroy` instead of surviving as orphans
     for the next cluster to re-bind against.
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
        #    We also write the teardown script. The upstream
        #    default (`rm -rf "$VOL_DIR"`) DELETES the PV's data
        #    when the PVC is removed — including implicit removal
        #    triggered by `tofu destroy` when the kind container
        #    is destroyed. We keep that default (set it explicitly
        #    so future readers can see the contract at a glance):
        #    `tofu destroy` is a full reset, and the data dir
        #    going away with the container is what we want.
        #
        #    The earlier `mv <dir> <dir>.preserved-<ts>` form is
        #    preserved in git history for users who want the
        #    recreate-with-data workflow. To opt back into that,
        #    replace the `rm -rf` below with the `mv` form AND
        #    ALSO disable the `null_resource.wipe_data` destroy
        #    provisioner in `infra/tofu/cluster.tf` (set
        #    `var.preserve_stateful_data = true` so the same
        #    `Bidirectional` mode contract decides both: with
        #    `preserve_stateful_data = true`, the `wipe_data`
        #    resource becomes a no-op and the teardown script
        #    reverts to `mv`).
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
                    # Upstream default — explicit for documentation.
                    "teardown": (
                        "#!/bin/sh\n"
                        "set -eu\n"
                        "if [ -d \"$VOL_DIR\" ]; then\n"
                        "  rm -rf \"$VOL_DIR\"\n"
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
        self._log.ok("local-path-config patched (PVs at /var/local/shared, teardown wipes data)")

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
