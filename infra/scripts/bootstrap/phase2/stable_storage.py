"""Stable storage — pre-create PV/PVC pairs at known host paths.

Problem this solves:

  `rancher/local-path-provisioner` (the default StorageClass we
  install) names PVs by PVC UID: `pvc-<UUID>_<ns>_<claim>`. When the
  cluster is destroyed and recreated, every PVC gets a fresh UUID
  and the new PV ends up in a *new* directory on the host. The old
  data is preserved on disk (we patched teardown to `mv` instead of
  `rm`), but **no pod ever mounts it again** — the new PVCs don't
  reference it. Net effect: the user loses GitLab config, repos,
  LFS artifacts, and Prometheus history every time they
  `tofu destroy && tofu apply`.

Fix:

  For the small set of services whose state we want to preserve
  across cluster recreates, we pre-create the storage ourselves
  with stable identities. Two flavors depending on what the chart
  template uses:

  Flavor A — `existingClaim` (charts that take a PVC-by-name
    override on a Deployment-style StatefulSet or single-pod
    StatefulSet):

      Host dir:   /var/local/shared/stable/<ns>-<component>/
      PV name:    pv-<ns>-<component>            (stable, no UUID)
      PVC name:   <ns>-<component>               (stable, no UUID)
      PV policy:  Retain                         (delete-PVC keeps data)
      Chart knob: postgresql.primary.persistence.existingClaim:
                    <ns>-<component>

  Flavor B — `volumeClaimTemplates` (StatefulSets that auto-mint
    PVCs from the StatefulSet's volumeClaimTemplate, e.g. `data`,
    `repo-data`):

      Host dir:   /var/local/shared/stable/<component>/
      PV name:    pv-<component>                 (stable, no UUID)
      PVC name:   (the chart's volumeClaimTemplate mint, e.g.
                   `data-openbao-0`, `repo-data-gitlab-gitaly-0`)
      PV selector: matches the labels the chart stamps on its
                    volumeClaimTemplate PVCs (configurable via the
                    chart's `*.persistence.labels` or
                    `dataStorage.labels` values overrides)
      PV policy:  Retain
      Chart knob: gitaly.persistence.storageClass: manual
                  + persistence.labels with our marker labels
                    (so volumeClaimTemplate uses storageClassName=manual,
                     and our PV selector can match the resulting PVC)

  The two flavors share infrastructure:
    - same `manual` StorageClass
    - same hostPath tree under infra/data/shared/stable/
    - same Retain policy

  What stays on local-path-provisioner:

    Any PVC we didn't pre-create (transient sidekiq queue dirs,
    buildkit cache, registry tmpfs, etc.) still flows through the
    provisioner. Those dirs are wiped via the patched `mv`-teardown,
    so they're harmless to lose.

Idempotency:

  - The host dirs are created with `mkdir -p` (no-op if exists).
  - The PV manifests are applied with `kubectl apply
    --server-side`. Pre-existing PV objects are left untouched.
  - On cluster recreate: `claimRef` on the PV points at the old
    PVC by UID, so we clear it before re-applying (a no-op on the
    first install when no PV exists yet). The hostPath data
    survives (Retain policy + bind-mounted /var/local/shared).
  - On re-create, fresh PVCs are minted and re-bind to the same
    hostPath dirs via storageClass + selector match.

Run order:

  Runs AFTER `local-path-provisioner.install()` (we don't *need*
  the provisioner for our stable PVs, but the chart subcomponents
  fall back to it for non-pinned PVCs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, DryRunRunner


@dataclass(frozen=True)
class StableVolume:
    """One pre-created PV (and possibly PVC) pair, host-backed."""

    flavor: str                       # "existing_claim", "volume_claim_template",
                                      # or "pvc_with_volume_name"
    namespace: str                    # K8s namespace (e.g. "gitlab")
    component: str                    # Short name (e.g. "postgresql")
    host_dir: str                     # Host path (relative to shared root)
    size: str                         # K8s quantity (e.g. "8Gi")
    pv_name: str                      # Stable PV name
    pvc_name: str                     # For Flavor A: stable PVC name we
                                      # create. For Flavor B: the
                                      # volumeClaimTemplate PVC the
                                      # StatefulSet will mint.
    chart_value_path: str             # Helm values path to set, e.g.
                                      # "postgresql.primary.persistence.existingClaim"
    chart_value: str                  # Value to set (PVC name for Flavor A,
                                      # StorageClass name for Flavor B)
    # For Flavor B: selector labels the PV uses to match the
    # chart-stamped PVC. The chart's `*.persistence.labels` (or
    # equivalent) value override must be set to these same labels.
    selector_labels: dict[str, str] = field(default_factory=dict)
    # For Flavor B: the chart values path that sets PVC labels
    # (e.g. "server.dataStorage.labels"). The installer doesn't
    # currently inject this; it's documented so the GitLab / OpenBao
    # value overrides know which keys to add.
    pvc_labels_path: str = ""
    # Annotations to stamp on the pre-created PVC (Flavor A only).
    # Required for CNPG: the operator's `EnrichStatus` only counts
    # PVCs whose `cnpg.io/nodeSerial` annotation parses to an int
    # AND whose name matches `<cluster>-<serial>`. Without these,
    # `cluster.Status.Instances` stays at 0 and the operator loops
    # in `createPrimaryInstance` → "refusing to create the primary
    # instance because the cluster already initialized".
    pvc_annotations: dict[str, str] = field(default_factory=dict)

    @property
    def host_path(self) -> str:
        # Container-side mount of /var/local/shared points here
        return f"/var/local/shared/stable/{self.host_dir}"


# The set of services we want to preserve across `tofu destroy &&
# tofu apply`. Sized for kind workers (3×4 GB).
#
# Flavor A (existingClaim): chart looks up a PVC by name and mounts it.
#   - postgresql: chart exposes postgresql.primary.persistence.existingClaim
#   - redis:      chart exposes redis.master.persistence.existingClaim
#   - prometheus: chart exposes server.persistence.existingClaim
#
# Flavor B (volumeClaimTemplate): chart mints its own PVC name; we
#   match via PV selector + storageClass pin.
#   - openbao:    StatefulSet with volumeClaimTemplate named `data`.
#                  The chart stamps NO labels on the PVC by default
#                  (only when server.dataStorage.labels is set), so
#                  we override dataStorage.labels with our marker.
#   - gitaly:     StatefulSet with volumeClaimTemplate named
#                  `repo-data`. The chart already stamps `app`,
#                  `release`, `storage` labels (see
#                  templates/_statefulset_spec.yaml), so we just
#                  match those.
STABLE_VOLUMES: tuple[StableVolume, ...] = (
    # --- Flavor A: existingClaim ------------------------------------
    # External CloudNativePG data — the GitLab chart 10.x no longer
    # bundles PostgreSQL, so we install cnpg ourselves and pin the
    # operator's auto-minted data PVC to a hostPath so `tofu
    # destroy && apply` preserves the GitLab database. The PV
    # pre-creates the PVC here (Flavor A), so the cnpg operator's
    # StatefulSet picks up our existing PVC instead of minting a
    # fresh one.
    #
    # CRITICAL: the PVC name must be `<cluster>-<nodeSerial>` —
    # e.g. `postgresql-cnpg-1` for serial 1 of cluster
    # `postgresql-cnpg`. The CNPG reconciler computes the
    # expected PVC name from `specs.GetInstanceName(cluster.Name,
    # serial)` and `EnrichStatus()` SKIPS any PVC in the
    # namespace whose `cnpg.io/nodeSerial` annotation is missing
    # OR whose name doesn't match. Pre-creating a PVC with the
    # wrong name (e.g. `postgresql-cnpg-data`) makes
    # `cluster.Status.Instances = 0`, which sends the operator
    # into a reconcile loop calling `createPrimaryInstance` →
    # "refusing to create the primary instance because the
    # cluster already initialized" + "PhaseUnrecoverable" on
    # every cycle (~10/s).
    StableVolume(
        "existing_claim", "postgresql", "cnpg", "postgresql/cnpg", "8Gi",
        pv_name="pv-cnpg-data",
        pvc_name="postgresql-cnpg-1",
        # CloudNativePG Cluster exposes `storage.spec.pvcName` for
        # this exact purpose. See:
        # https://cloudnative-pg.io/documentation/1.30/cloudnative-pg.v1/#postgresql-api
        chart_value_path="",
        chart_value="",
        # Stamp the CNPG labels + nodeSerial annotation on our
        # pre-created PVC so the operator's `EnrichStatus` recognises
        # it as belonging to instance 1 of `postgresql-cnpg`. Without
        # these, the PVC is filtered out → `Status.Instances = 0` →
        # reconcile loop.
        pvc_annotations={
            "cnpg.io/cluster": "postgresql-cnpg",
            "cnpg.io/instanceName": "postgresql-cnpg-1",
            "cnpg.io/instanceRole": "primary",
            "cnpg.io/nodeSerial": "1",
            "cnpg.io/pvcRole": "PG_DATA",
        },
    ),
    # External Redis data — chart 10.x consumes Redis externally
    # via `global.redis.host`. The bitnami/redis chart creates a
    # PVC named `redis-data` on install, which we pin to a hostPath
    # via existingClaim.
    StableVolume(
        "existing_claim", "redis", "data", "redis/data", "4Gi",
        pv_name="pv-redis-data",
        pvc_name="redis-data",
        chart_value_path="",
        chart_value="",
    ),
    # External MinIO data — chart 10.x's bundled object storage is
    # also dropped. We install minio/minio ourselves (standalone
    # mode) and pin its PVC (`minio-data`) to a hostPath.
    # The minio chart creates the PVC via `persistence.existingClaim`
    # when set, so we pre-create both the PV and PVC here.
    StableVolume(
        "existing_claim", "minio", "data", "minio/data", "20Gi",
        pv_name="pv-minio-data",
        pvc_name="minio-data",
        chart_value_path="",
        chart_value="",
    ),
    # --- Flavor B: volumeClaimTemplate ------------------------------
    # OpenBao init keys + KV data — losing this loses the GitLab
    # initial-root-password and runner token too.
    #
    # The OpenBao chart's data PVC has NO labels by default (the
    # `openbao.dataVolumeClaim.labels` helper only renders when
    # `server.dataStorage.labels` is set). We override that with a
    # marker label (`blueprint/stable-volume: openbao-data`) so the
    # PV can match by selector. The PVC will be auto-named
    # `data-openbao-0` by the StatefulSet controller.
    StableVolume(
        "volume_claim_template", "openbao", "data", "openbao/data", "10Gi",
        pv_name="pv-openbao-data",
        pvc_name="data-openbao-0",
        chart_value_path="server.dataStorage.storageClass",
        chart_value="manual",
        selector_labels={"blueprint/stable-volume": "openbao-data"},
        pvc_labels_path="server.dataStorage.labels",
    ),
    # Gitaly (git repos). The chart already stamps `app`,
    # `release`, `storage` labels on its volumeClaimTemplate PVCs.
    # We match those exactly.
    StableVolume(
        "volume_claim_template", "gitlab", "gitaly",
        "gitlab/gitaly", "50Gi",
        pv_name="pv-gitlab-gitaly",
        pvc_name="repo-data-gitlab-gitaly-0",
        chart_value_path="gitlab.gitaly.persistence.storageClass",
        chart_value="manual",
        selector_labels={
            "app": "gitlab-gitaly",
            "release": "gitlab",
            "storage": "default",
        },
    ),
    # --- Flavor C: pvc_with_volume_name ------------------------------
    # (Currently unused in chart 10.x. Kept in the schema for future
    # chart-bundled services that mint their own PVC. As of chart
    # 10.x the bundled PG / Redis / MinIO are gone — they all moved
    # to operator-managed installs above.)
)


class StableStorageInstaller:
    """Pre-create stable PV/PVC pairs for the services we want to preserve."""

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
            self._log.info("[dry-run] skipping stable storage install")
            return

        shared_root = Path(self._paths.data_shared)
        for vol in STABLE_VOLUMES:
            host_dir = shared_root / "stable" / vol.host_dir
            # Ensure the host dir exists with mode 0777. The chart's
            # pod will run as a non-root UID/GID (e.g. OpenBao
            # uses uid=100 gid=1000, postgres uses uid=999), and
            # kind's hostPath bind means kubelet does NOT chown for
            # us (it only does for in-container volumes). We must
            # therefore pre-chmod to 0777 — the kubelet's
            # fsGroup-based chown inside the container is a
            # no-op on hostPath (the dir lives outside the
            # container's filesystem), so the host-side mode has
            # to already permit world-write.
            host_dir.mkdir(parents=True, exist_ok=True)
            host_dir.chmod(0o777)
            self._log.info(f"Stable host dir: {host_dir} (mode 0777)")

            if vol.flavor == "existing_claim":
                pv_yaml = self._pv_existing_claim_yaml(vol)
                pvc_yaml = self._pvc_existing_claim_yaml(vol)
                # PV first so the PVC can bind to it.
                self._apply(pv_yaml, kind="pv", name=vol.pv_name)
                self._apply(pvc_yaml, kind="pvc", name=vol.pvc_name,
                            namespace=vol.namespace)
            elif vol.flavor == "volume_claim_template":
                # Only the PV is pre-created. The StatefulSet will
                # create the PVC itself via volumeClaimTemplate.
                pv_yaml = self._pv_volume_claim_template_yaml(vol)
                self._apply(pv_yaml, kind="pv", name=vol.pv_name)
            elif vol.flavor == "pvc_with_volume_name":
                # The chart creates the PVC on install (via
                # minio_pvc.yaml), pinning to our PV via volumeName +
                # matchLabels. We only need to create the PV here.
                # On cluster recreate the chart re-creates the PVC
                # (the old one was deleted with the namespace); the
                # PV's `claimRef` still points at the old PVC name +
                # namespace, which is fine — the chart's new PVC
                # matches by name in `volumeName` and binds to us.
                pv_yaml = self._pv_pvc_with_volume_name_yaml(vol)
                self._apply(pv_yaml, kind="pv", name=vol.pv_name)
            else:
                raise ValueError(f"unknown flavor: {vol.flavor}")

            self._log.ok(
                f"Stable [{vol.flavor}]: "
                f"{vol.namespace}/{vol.pvc_name} → {host_dir} "
                f"(chart: {vol.chart_value_path}={vol.chart_value!r})"
            )

    # ---------- manifest helpers ----------

    def _pv_existing_claim_yaml(self, vol: StableVolume) -> str:
        # Retain reclaim policy is critical: even if the PVC is
        # deleted (cluster recreate), the PV (and its hostPath
        # data) is kept. We explicitly DO NOT use the local-path
        # provisioner for these — we want total control.
        return f"""\
apiVersion: v1
kind: PersistentVolume
metadata:
  name: {vol.pv_name}
  labels:
    app.kubernetes.io/managed-by: blueprint-stable-storage
    app.kubernetes.io/component: {vol.component}
    app.kubernetes.io/namespace: {vol.namespace}
spec:
  capacity:
    storage: {vol.size}
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: manual
  hostPath:
    path: {vol.host_path}
    type: DirectoryOrCreate
"""

    def _pvc_existing_claim_yaml(self, vol: StableVolume) -> str:
        annotations_yaml = ""
        if vol.pvc_annotations:
            annotations_yaml = "  annotations:\n" + "".join(
                f"    {k}: {v!r}\n" for k, v in vol.pvc_annotations.items()
            )
        return f"""\
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {vol.pvc_name}
  namespace: {vol.namespace}
{annotations_yaml}  labels:
    app.kubernetes.io/managed-by: blueprint-stable-storage
    app.kubernetes.io/component: {vol.component}
    blueprint/stable-volume: "{vol.component}"
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: {vol.size}
  storageClassName: manual
  volumeName: {vol.pv_name}
"""

    def _pv_volume_claim_template_yaml(self, vol: StableVolume) -> str:
        # For StatefulSet-driven volumeClaimTemplates, the PVC is
        # auto-created by the StatefulSet controller. We pin the PV
        # to the future PVC via `claimRef` (the canonical
        # "pre-bind a specific PV to a specific PVC" mechanism —
        # see
        # https://kubernetes.io/docs/concepts/storage/persistent-volumes/#reserving-a-persistentvolume
        # "Reserving a PersistentVolume").
        #
        # claimRef binds regardless of storage class / size / access
        # mode checks, so the StatefulSet's PVC (named like
        # `data-openbao-0`, `repo-data-gitlab-gitaly-0`) is
        # guaranteed to claim THIS PV.
        #
        # On cluster recreate the PVC's UID changes (fresh
        # StatefulSet → fresh owner refs). The PV's claimRef would
        # still pin to (name, namespace) but the stale UID blocks
        # rebinding. We patch `claimRef.uid` to null before
        # re-applying (see _apply()) so the new PVC can claim us.
        # The name + namespace pin survives because the StatefulSet
        # always mints the same names for a given chart release.
        labels_lines = "\n".join(
            f"    {k}: {v!r}" for k, v in vol.selector_labels.items()
        )
        return f"""\
apiVersion: v1
kind: PersistentVolume
metadata:
  name: {vol.pv_name}
  labels:
{labels_lines}
    app.kubernetes.io/managed-by: blueprint-stable-storage
    app.kubernetes.io/component: {vol.component}
    app.kubernetes.io/namespace: {vol.namespace}
spec:
  capacity:
    storage: {vol.size}
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: manual
  # Pre-bind to the PVC the StatefulSet will mint on first
  # apply. uid is intentionally omitted — kubernetes fills it in
  # once the PVC is created. On cluster recreate, _apply() clears
  # claimRef so the new PVC can claim us.
  claimRef:
    apiVersion: v1
    kind: PersistentVolumeClaim
    name: {vol.pvc_name}
    namespace: {vol.namespace}
  hostPath:
    path: {vol.host_path}
    type: DirectoryOrCreate
"""

    def _pv_pvc_with_volume_name_yaml(self, vol: StableVolume) -> str:
        # For chart-driven PVCs that pin via `volumeName` +
        # `matchLabels` (currently just MinIO). We DO NOT set
        # `claimRef` because the chart creates the PVC itself;
        # instead we set our `selector_labels` so the chart's PVC
        # can match this PV via `spec.selector.matchLabels` (the
        # chart copies `persistence.matchLabels` into the PVC's
        # selector). The chart's PVC also sets `volumeName` to our
        # PV name (via `persistence.volumeName`), which is the
        # primary binding mechanism.
        #
        # Why no claimRef here:
        #   - claimRef is normally used to pre-bind a PV before the
        #     PVC exists. With Flavor C the chart creates the PVC
        #     on install, which would race with claimRef setup.
        #   - claimRef with stale uid would block rebind on cluster
        #     recreate, which is exactly what we want to avoid.
        #   - The chart's volumeName + matchLabels bind is enough.
        labels_lines = "\n".join(
            f"    {k}: {v!r}" for k, v in vol.selector_labels.items()
        )
        return f"""\
apiVersion: v1
kind: PersistentVolume
metadata:
  name: {vol.pv_name}
  labels:
{labels_lines}
    app.kubernetes.io/managed-by: blueprint-stable-storage
    app.kubernetes.io/component: {vol.component}
    app.kubernetes.io/namespace: {vol.namespace}
spec:
  capacity:
    storage: {vol.size}
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  # For Flavor C, the chart mints the PVC with whatever its
  # default StorageClass is (`local-path` in our case, set by
  # the chart value `minio.persistence.storageClass` which
  # defaults to `global.storageClass`). Kubernetes requires the
  # PV's storageClassName to MATCH the PVC's for binding —
  # even when volumeName is set, SC mismatch raises
  # `VolumeMismatch` and the PVC stays Pending.
  storageClassName: local-path
  hostPath:
    path: {vol.host_path}
    type: DirectoryOrCreate
"""

    def _apply(self, manifest: str, kind: str, name: str,
               namespace: str = "") -> None:
        # If the PV is in `Released` state from a previous cluster,
        # claimRef prevents rebinding. We try to clear it before apply.
        if kind == "pv":
            self._r.run(
                [
                    "kubectl", "patch", "pv", name,
                    "--type=json",
                    "-p", '[{"op": "remove", "path": "/spec/claimRef"}]',
                ],
                check=False,
            )
        # PVCs need their namespace to exist first. The chart
        # installer will normally create it, but we run before the
        # chart — so ensure it ourselves (idempotent: kubectl
        # create returns "AlreadyExists" which we treat as success).
        if kind == "pvc" and namespace:
            self._r.run(
                ["kubectl", "create", "namespace", namespace],
                check=False,
            )
        self._r.run(
            ["kubectl", "apply", "--server-side",
             "--force-conflicts", "-f", "-"],
            stdin=manifest,
            check=True,
        )
        # CRITICAL post-apply for Flavor B PVs: even after `apply`
        # succeeds, the apply server may have written claimRef.uid
        # from the OLD PVC's UID (if a PVC with the same name+ns
        # already existed in another cluster recreate cycle). That
        # stale uid blocks the new StatefulSet from claiming us,
        # leaving the PVC in `Lost` state forever. We patch
        # claimRef.uid to null after apply so the new PVC (with
        # fresh uid) can claim us.
        if kind == "pv":
            self._r.run(
                [
                    "kubectl", "patch", "pv", name,
                    "--type=json",
                    "-p",
                    '[{"op": "replace", "path": "/spec/claimRef/uid", "value": null}]',
                ],
                check=False,
            )