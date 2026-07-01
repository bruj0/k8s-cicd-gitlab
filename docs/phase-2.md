# Phase 2 — Install GitLab + Runner + OpenBao on a Phase-1 cluster

This phase installs the application stack on top of the 5-node kind
cluster that Phase 1 created. Everything here is **idempotent** —
re-running the install resumes from the failed step and the success
path is a no-op.

If you only need to *use* the stack, the [README](../README.md) is
the entry point. This doc is for the team mate who has to **modify
or extend** Phase 2: add a new component, change a chart value,
debug a failure, or understand why something is the way it is.

## 1. What Phase 2 delivers

After `uv run blueprint-bootstrap --phase 2` finishes on a healthy
Phase-1 cluster, you have:

  - **CloudNativePG operator + Cluster/postgresql-cnpg** in the
    `postgresql` namespace — single instance, 8Gi, bound to the
    stable PV via a pre-created PVC named `postgresql-cnpg-1`.
    The bootstrap mints the `gitlab` and `openbao` PG roles
    (their passwords are persisted to
    `infra/secrets/cnpg-role-passwords.json`, mode 0600) and the
    `gitlabhq_production` + `openbao` databases.
  - **Redis single-node** in the `redis` namespace
    (`architecture=standalone`, no Sentinel, no replicas), bound
    to the stable PV. Password snapshot at
    `infra/secrets/redis-password.txt`.
  - **MinIO single-node** in the `minio` namespace — single pod,
    no distributed mode, no erasure coding. 11 GitLab buckets are
    created via in-cluster `mc`. Root credentials snapshotted to
    `infra/secrets/minio-root-{user,password}.txt`. A dual-key
    `gitlab-rails-storage` Secret in the `gitlab` namespace
    carries both the Rails-side `connection` and the
    Docker-registry-native `config` (see § *Stable storage / MinIO
    object store* below).
  - **OpenBao** in the `openbao` namespace — the
    **bootstrap-installed OpenBao**, initialised + unsealed,
    KV-v2 mount at `secret/`, root token + unseal key persisted to
    `infra/secrets/openbao-init.json` (gitignored, mode 0600).
    Used by the bootstrap itself as a hand-off point
    (`OpenBaoClient` + `blueprint-secrets` CLI).
  - **Wildcard TLS** — self-signed CA + wildcard cert for
    `*.local.bruj0.net` materialised as `gitlab-wildcard-tls` (plus
    3 listener-specific aliases `registry-tls` / `kas-tls` /
    `minio-tls`) in the `gitlab` namespace.
  - **GitLab CE** in the `gitlab` namespace. Chart 10.x
    sub-installs **Envoy Gateway** as a managed sub-chart
    (`gateway-helm`) and bundles **OpenBao** as a managed
    sub-chart (`chart-bundled OpenBao` → `gitlab-openbao`
    Deployment; the GitLab rails app uses it for Secrets Manager).
    The chart also re-uses the externally-installed CloudNativePG,
    Redis, and MinIO via `global.psql.host`, `global.redis.host`,
    and `appConfig.object_store.*.connection.secret`.
  - **GitLab Runner** in `gitlab` (sub-chart) registered against
    GitLab using a token that was captured into OpenBao at
    `secret/gitlab/runner/registration_token` during the GitLab
    install step. The Runner registers against
    `http://gitlab-webservice-default.gitlab.svc:8181` (in-cluster
    Service DNS, not the public hostname — see § *Runner URL*
    below).
  - All the secrets a developer needs to bootstrap the cluster in
    their `/etc/hosts` + browser, in OpenBao — not in git, not on
    disk outside the gitignored init JSON.

### URLs the user can reach

All served by the chart's Envoy Gateway, terminated with the
self-signed wildcard. Trust the CA on the host first (see
[README](../README.md#what-you-the-user-need-to-do-post-install)).

| URL                                  | Login                                              | What it is                       |
| ------------------------------------ | -------------------------------------------------- | -------------------------------- |
| `https://gitlab.local.bruj0.net`           | `root` / OpenBao secret                            | GitLab web UI + API              |
| `https://registry.local.bruj0.net`         | `root` / OpenBao secret                            | GitLab Container Registry        |
| `https://kas.local.bruj0.net`              | `root` / OpenBao secret                            | GitLab Agent Server (KAS)        |
| `https://minio.local.bruj0.net`            | `root` / OpenBao secret (Snapshot → MinIO creds)   | MinIO (LFS, artifacts, packages) |
| `https://openbao.local.bruj0.net`          | root token in `infra/secrets/openbao-init.json`    | **Bootstrap-installed** OpenBao UI (KV-v2 hand-off) |

The **chart-bundled OpenBao** is internal-only — it has no public
Gateway listener, it talks to the rails app over
`openbao.gitlab.svc:8200` from inside the cluster. Web admin UI is
deliberately not exposed. The Secrets Manager API path lives at
`https://gitlab.local.bruj0.net/api/v4/secrets_manager/...` once
the admin enables Secrets Manager in the GitLab UI.

## 2. The 13-step pipeline

```
 1. Pre-flight              cluster + helm reachable
 2. Gateway CRDs            standard v1.5.0 + chart-shipped Envoy CRDs
                            (8 in `gateway-api-crds/generated/`)
 3. local-path provisioner  rancher/local-path with `local-path` as default
                            StorageClass + bind pathBase → /var/local/shared
 4. Stable PV/PVC pairs     pre-create hostPath-backed PVs for the stateful
                            services we want to preserve across cluster
                            recreates (CloudNativePG, Redis, MinIO, OpenBao,
                            Gitaly) — 3 flavors depending on how the chart
                            mints its PVC. The CNPG case enforces an extra
                            PVC name + annotation contract (see § Stable
                            storage).
 5. CloudNativePG           install the operator + Cluster/postgresql-cnpg
                            (1 instance, 8Gi, FQDN selector) + bootstrap
                            `gitlabhq_production` + `openbao` databases
                            and the corresponding roles
 6. Redis                   install bitnami/redis single-node
                            (architecture=standalone)
 7. MinIO                   install minio (mode: standalone) + 11 buckets
                            + dual-key `gitlab-rails-storage` Secret
 8. OpenBao                 install + init + unseal (PG backend =
                            CloudNativePG, database `openbao`)
 9. Wildcard TLS certs      mint a self-signed CA + wildcard cert for
                            *.local.bruj0.net and materialise the 4 Gateway
                            listener Secrets. Idempotent: re-uses a cert that
                            has ≥30 days of validity left.
10. Persistent Secrets      restore chart-managed Secrets (postgres/redis/
                            minio/rails/gitaly/kas) from the host-side
                            snapshot so the chart sees them already exist
                            and re-uses the same credentials as the on-disk
                            data — without this, PG logs
                            `password authentication failed` after every
                            cluster recreate.
11. GitLab                  install chart 10.x (bundles Envoy Gateway +
                            chart-bundled OpenBao subchart) + token
                            capture + write initial root password to
                            OpenBao + re-create the bootstrap-minted
                            PG roles if the cluster was recreated
                            without `--destroy`
12. Migrations              wait for the chart-managed migrations Job
                            to complete (skipped on subsequent
                            re-installs)
13. GitLab Runner           install + register against
                            `http://gitlab-webservice-default.gitlab.svc:8181`
                            using the token captured by step 11
```

Each step is one method on `Phase2Pipeline` (`_step_*`) and one
`XxxInstaller` class. The pipeline owns ordering, logging, and
error reporting; each installer owns the actual work.

The pipeline is **idempotent** at the step level: re-running
resumes from the failed step and the success path is a no-op.
Idempotency is achieved by:

  - probing the cluster (`kubectl get`) before doing anything;
  - reading the init JSON / KV state to detect "already done";
  - reading the cert validity to decide whether to re-mint the
    wildcard;
  - reading the host-side Secrets snapshot to decide whether to
    restore (`gitlab-runtime-secrets.yaml` missing → fresh
    install, chart will mint new passwords and the snapshot
    gets re-written at the end of the install);
  - using `helm upgrade --install` (creates or upgrades, never
    errors on existing release);
  - `kubectl apply --server-side --force-conflicts` for the
    CRDs and PVs (re-applying is fine, the server resolves
    conflicts).

### Wiping everything

The bootstrap ships a one-shot teardown:

```sh
uv run blueprint-bootstrap --destroy [--yes] [--dry-run]
```

Runs `tofu destroy` (cluster lifecycle is owned by OpenTofu;
see `AGENTS.md § 4 rule #3`), then recursively wipes the
bootstrap-owned host-side state: `infra/data/shared/stable/`,
`infra/data/shared/*.preserved-*` orphan dirs, `infra/tls/wildcard/`,
`infra/secrets/openbao-init.json`, `infra/secrets/gitlab-runtime-secrets.yaml`.
PV dirs that the local-path-provisioner mv'd to `*.preserved-<ts>`
on past helm uninstalls are also cleaned up. If a stable dir is
owned by a pod UID that our user can't unlink (openbao=100,
postgres=1001), `--destroy` falls back to a one-shot
`docker run --rm` (or `podman run`) bind-mounting the parent
dir, so the privileged container can `chmod -R a+rwX && rm -rf`
the child. `--destroy` never recreates — the user must run
`tofu apply` themselves, per `AGENTS.md § 4 rule #1`.

## 3. The big idea: the chart owns the rest

Phase 2 *used to* install Traefik + a custom GatewayClass chart +
a custom CA chart + HTTPRoute manifests. It now does **none of
that** — the GitLab chart owns it all:

  - The chart's `gateway-helm` sub-chart installs Envoy Gateway.
  - The chart's pre-install cfssl Job mints `*.local.bruj0.net`.
  - The chart's own templates render a `Gateway` + the four
    `HTTPRoute` resources (gitlab / registry / kas / minio) and
    point them at the Secret the Job created.
  - The chart's `configureCertmanager: false` setting tells it
    to use the self-signed path instead of requiring a
    public-CA issuer (which can't validate `*.local.bruj0.net`
    anyway).

This means Phase 2 has exactly **one** value override that touches
the TLS path: we patch the chart's Gateway listener
`tls.certificateRefs[0].name` from the cert-manager default
(`gitlab-tls`) to the self-signed cert name
(`gitlab-wildcard-tls`). That override lives in
`phase2/references/helm-values-gitlab.yaml` and is the only
non-default value the bootstrap puts in for the TLS path.

Why this matters for new contributors: **don't add custom
ingress / cert / GatewayClass charts.** Anything you might be
tempted to install alongside the GitLab chart almost certainly
has a chart value that turns it on. Read the chart's
`values.yaml` first.

## 4. The code map

```
infra/scripts/bootstrap/
├── cli.py                        # click wrapper: `blueprint-bootstrap` entry point
│                                 # + `--destroy` / `--port-forward` / `--dry-run`
├── secrets_cli.py                # click wrapper: `blueprint-secrets` (post-install helper)
├── app.py                        # composition root — wires Phase2Installers into BootstrapApp
├── app_installer.py              # base class HelmAppInstaller + generic helm plumbing
├── phase2/
│   ├── pipeline.py               # Phase2Pipeline — orchestrates the 13 steps
│   ├── catalog.py                # Phase2Installers dataclass — bundle of every installer
│   ├── gateway.py                # GatewayCRDsInstaller (standard v1.5.0 + 8 chart-shipped Envoy CRDs)
│   ├── local_path_provisioner.py # local-path StorageClass + teardown patches
│   ├── stable_storage.py         # pre-create stable PV/PVC pairs (3 flavors + CNPG name/annotation contract)
│   ├── cloudnative_pg.py         # CNPG operator + Cluster/postgresql-cnpg + role/db bootstrap
│   ├── redis.py                  # bitnami/redis single-node installer (architecture=standalone)
│   ├── minio.py                  # MinIO single-node installer + 11 buckets + dual-key Secret
│   ├── openbao.py                # OpenBaoInstaller (chart + init + unseal; PG backend)
│   ├── wildcard_certs.py         # mint CA + wildcard cert + 4 Gateway listener Secrets
│   ├── persistent_secrets.py     # snapshot + restore chart-managed Secrets
│   ├── gitlab.py                 # GitlabInstaller (chart 10.x + token capture + write root pw to OpenBao)
│   ├── runner.py                 # GitLabRunnerInstaller (in-cluster Service URL, HTTP)
│   ├── secrets.py                # OpenBaoClient (hvac + auto port-forward)
│   └── references/
│       ├── cluster-postgresql.yaml
│       ├── helm-values-{cnpg,redis,minio,openbao,gitlab,runner}.yaml
│       └── gateway-api-crds/    # generated/ subdir contains 8 chart-shipped Envoy CRDs;
│                                  # the standard v1.5.0 CRDs are applied directly from URL
```

### Who calls who

```
BootstrapApp (app.py)
  └─> Phase2Pipeline (phase2/pipeline.py)
        ├─> _step_preflight                     → CommandRunner probes
        ├─> _step_gateway_crds                  → GatewayCRDsInstaller
        ├─> _step_local_path                    → LocalPathProvisionerInstaller
        ├─> _step_stable_storage                → StableStorageInstaller
        │                                            (3 flavors: existing_claim,
        │                                             volume_claim_template,
        │                                             pvc_with_volume_name)
        ├─> _step_cloudnative_pg                → CloudNativePGInstaller
        │                                            (operator + Cluster + role/db bootstrap)
        ├─> _step_redis                         → RedisInstaller
        ├─> _step_minio                         → MinIOInstaller
        │                                            (deploy + 11 buckets + dual-key
        │                                             gitlab-rails-storage Secret)
        ├─> _step_openbao                       → OpenBaoInstaller
        │                                            └─> OpenBaoClient (init, unseal, kv mounts)
        ├─> _step_wildcard_certs                → WildcardCertsInstaller
        ├─> _step_persistent_secrets_restore    → PersistentSecretsInstaller.restore()
        ├─> _step_gitlab                        → GitlabInstaller
        │                                            └─> OpenBaoClient (write root password
        │                                                 + capture runner token)
        ├─> _step_migrations                    → wait for migrations Job to Complete
        └─> _step_runner                        → GitLabRunnerInstaller
                                                     └─> OpenBaoClient (read runner token)
```

`OpenBaoClient` is **the only stateful helper** Phase 2 shares
across steps. The `GitlabInstaller` writes the GitLab initial
root password and the runner registration token into OpenBao;
the `GitLabRunnerInstaller` reads the runner token back. Without
this handoff, Runner would have to authenticate against GitLab
at install time with a token we don't have yet.

## 5. Conventions every contributor must follow

These are non-negotiable. They are restated in `AGENTS.md` § 4
and `.agents/skills/provision-gitlab/SKILL.md`, but they bear
repeating because every Phase 2 PR will be measured against
them.

1. **No `kubectl exec ... bao ...` in install code.** Phase 2
   talks to OpenBao via `OpenBaoClient` (hvac over HTTP). The
   client auto-port-forwards 127.0.0.1:8200 on first use.
   Direct execs are fragile (the kubelet round-trips every
   call, and the pod's token helper doesn't always survive
   across separate exec invocations — that bug bit us once,
   see SKILL.md common pitfalls).

2. **All template / config content lives in
   `phase2/references/`.** No openssl cnf strings, no inline
   YAML, no f-strings building chart values. The Python code
   reads from these files at install time. The reason: changes
   to install-time config are reviewable as diffs, not
   embedded in code.

3. **All pinned versions live in `bootstrap/VERSIONS.json`.**
   No class hardcodes a chart version, a container image, or a
   tool version. Bump versions in JSON, never in code. The
   bootstrap uses this file as the single source of truth for
   what gets downloaded and what version of what is installed.

4. **Chart values are merged, not overridden.** Each installer's
   values file (under `references/`) is a *partial* override
   layered on top of the chart's own `values.yaml`. Don't copy
   the whole chart values into our override — just the keys we
   want to change. The GitLab chart's defaults are the
   baseline.

5. **`*.local.bruj0.net` is for browsers, not for cluster
   traffic.** Pods that try to reach `gitlab.local.bruj0.net`
   resolve it to `127.0.0.1` and break. Every chart value
   that takes a `*Url`/`*Host`/`*Endpoint` flag and is read
   by a **pod** must point at the in-cluster Service DNS
   instead:
   - `gitlab-webservice-default.gitlab.svc:8181`
   - `openbao.openbao.svc:8200`
   - `postgresql-cnpg-rw.postgresql.svc.cluster.local:5432`

   If you add a new chart and find yourself wanting to put
   `gitlab.local.bruj0.net` in a value, stop and use the
   Service DNS.

6. **Python follows SOLID.** One class per concern. `app.py`
   is the only place that knows about every class.
   `catalog.py` is the only place that knows about every
   installer. The pipeline knows the order, the installers
   know the work.

7. **Dry-run must work end-to-end.** `uv run blueprint-bootstrap
   --phase 2 --dry-run` exercises every step without touching
   the cluster. The `DryRunRunner` is the single seam: every
   command goes through it. If your installer calls
   `subprocess.run` directly, the dry-run mode will silently
   execute the real command — fix it by routing through the
   injected `CommandRunner`.

## 6. How to add a new install step

Use case: you want to add **Vault** (or `trivy-operator`, or
`prometheus`, or anything that sits alongside GitLab) so it
installs on the same Phase-1 cluster as part of the same
`--phase 2` run.

### Checklist

- [ ] Add the chart to `bootstrap/VERSIONS.json` under
  `helm_repositories` (name, url, chart, chart_version,
  values_overrides). This is what `helm_cache.py` reads to
  download the chart on first run.
- [ ] Create `phase2/<name>.py` with a `<Name>Installer` class
  that subclasses `HelmAppInstaller`. See § 7 for the
  skeleton.
- [ ] Create `phase2/references/helm-values-<name>.yaml` with
  your partial value override. Don't copy the chart's full
  values — just the keys you need to change.
- [ ] If the component needs a Secret in OpenBao, do the
  write in `install()` via `OpenBaoClient.kv_put(...)`. If
  later steps need that Secret, read it back the same way.
  Never read a Secret directly from a K8s Secret — go through
  OpenBao.
- [ ] Add the installer to `phase2/catalog.py`:
  `Phase2Installers` (frozen dataclass) + `all()`.
- [ ] Wire the installer in `app.py` (where the other Phase 2
  installers are constructed — look for
  `self._phase2_installers = Phase2Installers(...)`).
- [ ] Add a `_step_<name>` method to `Phase2Pipeline` and call
  it from `run()`. **Place it after OpenBao if it needs
  OpenBao**, otherwise anywhere after the pre-flight.
- [ ] Update `__init__.py` to re-export the new class.
- [ ] If the new component changes the smoke-test surface,
  append to the "Smoke tests" block in
  `.agents/skills/provision-gitlab/SKILL.md`.
- [ ] If the new component changed any pinned version or
  made a non-obvious design choice, append to the "Common
  pitfalls" section of the SKILL.
- [ ] Run `uv run blueprint-bootstrap --phase 2 --dry-run` and
  confirm the new step appears with the right shape.

### Step-order rules of thumb

- Anything that needs OpenBao → **after** `_step_openbao`.
- Anything that needs GitLab → **after** `_step_gitlab`.
- CRDs that the GitLab chart depends on → **before** `_step_gitlab`.
- Components that the GitLab Runner needs to talk to → **after**
  `_step_gitlab` and probably after `_step_runner` (so the runner
  is registered before the workload starts).

## 7. Installer skeleton

```python
# infra/scripts/bootstrap/phase2/<name>.py
from __future__ import annotations

from ..app_installer import HelmAppInstaller, HelmAppSpec
from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner
from .secrets import OpenBaoClient  # only if you need it


class VaultInstaller(HelmAppInstaller):
    """One-paragraph description of what this installs + post-install steps.

    Idempotency: describe how this installer knows when to do nothing.
    """

    NAMESPACE = "vault"
    RELEASE = "vault"
    REPO_KEY = "vault"   # must match a key under helm_repositories in VERSIONS.json

    def __init__(
        self,
        runner: CommandRunner,
        paths: Paths,
        cache,                      # HelmChartCache
        log: Logger,
        openbao: OpenBaoClient | None = None,   # inject if you need it
    ) -> None:
        super().__init__(
            runner, paths, cache, log,
            HelmAppSpec(
                repo_key=self.REPO_KEY,
                release=self.RELEASE,
                namespace=self.NAMESPACE,
                wait=True,
                values_files=(
                    str(paths.phase2_refs_dir / "helm-values-vault.yaml"),
                ),
            ),
        )
        self._openbao = openbao

    def install(self):  # type: ignore[override]
        result = super().install()  # helm upgrade --install
        if self._openbao is not None:
            # Post-install: write any secrets the chart needs at boot.
            # All output goes through self._log so the dry-run path
            # works without touching a real OpenBao.
            self._log.info("writing vault init token to OpenBao")
            self._openbao.kv_put("vault", {"init_token": "..."})
        return result
```

Three things to be careful of:

  - `paths.phase2_refs_dir` is the absolute path to
    `infra/scripts/bootstrap/phase2/references/`. Use it for
    every file the installer reads. Don't hardcode paths.
  - `HelmAppSpec` has more fields than shown — see
    `app_installer.py` for the full set. The most useful
    non-obvious one is `set_overrides: dict[str, object]`
    for things that can't be expressed in the values file
    (e.g. `set gitlabUrl=...` from a runtime-resolved value).
  - `super().install()` returns the `SubprocessRunner` result
    (or a `DryRunRunner` fake). Don't ignore it — the dry-run
    smoke test depends on every code path going through the
    runner.

## 8. How to change a chart value

Three places, in order of preference:

1. **Add to `phase2/references/helm-values-<chart>.yaml`.** This
   is the values file `HelmAppInstaller` passes to `helm upgrade
   --install`. The chart's own `values.yaml` is the baseline;
   this file is a *partial override* on top.
2. **Bump a pinned version.** Edit
   `bootstrap/VERSIONS.json` under `helm_repositories.<chart>`
   (`chart_version` for the chart version, `values_overrides`
   for the default override that gets applied even without a
   per-installer values file). The `helm_cache.py` re-downloads
   the chart on the next run.
3. **Pass at install time.** `HelmAppSpec.set_overrides` is a
   dict of `--set` flags. Use this for values that have to be
   computed at install time (e.g. a token read from OpenBao).
   Avoid if you can — `references/` files are reviewable as
   diffs, `--set` flags aren't.

The chart's *own* `values.yaml` is the documentation of record
for what's settable. Read it before guessing. The GitLab chart
in particular has a deep `global.*` tree that drives most of
the behaviour; the override we ship
(`phase2/references/helm-values-gitlab.yaml`) only changes ~10
keys.

## 9. How to debug a failing step

When `--phase 2` fails, the message looks like
`Phase 2 install failed at step N/9: <error>`. The mapping
from step to source file is:

| Step | Source                                                       |
| ---- | ------------------------------------------------------------ |
| 1/13 | `phase2/pipeline.py:_step_preflight`                          |
| 2/13 | `phase2/gateway.py` + `phase2/references/gateway-api-crds/`   |
| 3/13 | `phase2/local_path_provisioner.py` (StorageClass + bind-mount patches) |
| 4/13 | `phase2/stable_storage.py` (3 flavors: `existing_claim` / `volume_claim_template` / `pvc_with_volume_name` + the CNPG name + annotation contract) |
| 5/13 | `phase2/cloudnative_pg.py` + `phase2/references/cluster-postgresql.yaml` (operator + Cluster + role/db bootstrap) |
| 6/13 | `phase2/redis.py` + `phase2/references/helm-values-redis.yaml` (bitnami/redis single-node) |
| 7/13 | `phase2/minio.py` + `phase2/references/helm-values-minio.yaml` (MinIO + 11 buckets + dual-key Secret) |
| 8/13 | `phase2/openbao.py` + `phase2/secrets.py` (`OpenBaoClient`) + `phase2/references/helm-values-openbao.yaml` |
| 9/13 | `phase2/wildcard_certs.py` (CA + cert mint + 4 Gateway listener Secrets) |
| 10/13 | `phase2/persistent_secrets.py:restore()` + `infra/secrets/gitlab-runtime-secrets.yaml` (host-side snapshot) |
| 11/13 | `phase2/gitlab.py` + `phase2/references/helm-values-gitlab.yaml` (chart-bundled OpenBao subchart + external PG/Redis/MinIO) |
| 12/13 | `phase2/pipeline.py:_step_migrations` — wait for `gitlab-migrations-*` Job to reach `Complete` |
| 13/13 | `phase2/runner.py` + `phase2/references/helm-values-runner.yaml` |

In addition:

  - **The "Common pitfalls" section of
    `.agents/skills/provision-gitlab/SKILL.md` is the canonical
    log of every symptom we've seen and how it was fixed.**
    Read it before going deep. If your fix isn't there, append
    to it (the rule is "append, don't rewrite").
  - **Dry-run is your friend.** `uv run blueprint-bootstrap
    --phase 2 --dry-run` runs every step against the
    `DryRunRunner`, which logs every command without executing.
    If the dry-run shape doesn't match what you expect, the
    install is going to fail in the same way.
  - **The post-install helper is the easiest way to check
    OpenBao.** `uv run blueprint-secrets read <path> [key]`
    auto-port-forwards 127.0.0.1:8200 and reads via hvac — no
    `kubectl exec`, no token helper fragility.

## 10. Stable storage (the contract)

`phase2/stable_storage.py` owns the three flavours of stable PV/PVC
pair. Two of them are pre-created PVCs with explicit
`claimRef`/`matchLabels`; the third (CNPG) has an extra contract
that you will trip over if you don't know it.

### General pairing rules

  - **All PVs use `hostPath`** pointing to
    `infra/data/shared/stable/<service>/`. The path is created
    by the bootstrap (mode 0777) and survives `tofu destroy`.
  - **PVs use `Retain` persistentVolumeReclaimPolicy** so the
    volume isn't reaped on PVC delete. The bootstrap patches the
    claimRef out of the PV before re-applying, since the UID of
    the PVC changes per install.
  - **Three flavours** depending on how the chart mints its PVC:
    | Flavour | Used by | Mechanism |
    | --- | --- | --- |
    | `existing_claim` | Redis, MinIO, CNPG | chart's `persistence.existingClaim: <name>` (or `spec.selector.matchLabels` for CNPG) |
    | `volume_claim_template` | OpenBao, Gitaly | StatefulSet picks labels on its PVC; PV uses `matchLabels` |
    | `pvc_with_volume_name` | (legacy) | non-templated PVC with explicit `volumeName: <pv-name>` |

### CNPG-specific contract

The CloudNativePG operator resolves PVC ownership from the
field-index selector `.metadata.controller` — it lists every
PVC whose `metadata.ownerReferences[?(@.controller==true)].uid`
matches the Cluster's UID. **A pre-created PVC without
`ownerReferences` is invisible to the operator's reconcile loop**:

  1. **PVC name must match `<cluster-name>-<serial>`.** For our
     Cluster `postgresql-cnpg` (single instance), that's
     `postgresql-cnpg-1`. The bootstrap's `StableVolume` dataclass
     computes the expected name at install time.
  2. **Required annotations on the PVC:**
     - `cnpg.io/cluster: postgresql-cnpg`
     - `cnpg.io/instanceName: postgresql-cnpg-1`
     - `cnpg.io/instanceRole: primary`
     - `cnpg.io/nodeSerial: "1"`
     - `cnpg.io/pvcRole: main`
  3. **`ownerReferences[]` must contain the Cluster as a
     `controller: true` reference.** The operator only trusts the
     `controller: true` flag, NOT just any reference. After the
     Cluster is created (step 5), `phase2/cloudnative_pg.py`
     patches the PVC with a single ownerReference pointing at the
     Cluster's UID. Without this step the operator logs
     `Refusing to create the primary instance: cluster already
     initialized` in a loop until the GC reclaims the orphan.

  Both invariants are owned by the bootstrap. Hand-editing PVCs
  to work around them is a code smell — the bootstrap should be
  the one fixing it.

## 11. MinIO object store + the dual-key Secret

`phase2/minio.py` does three things in sequence:

  1. **Deploy MinIO** (`mode: standalone`, single pod, no
     erasure coding) bound to the stable PV.
  2. **Create 11 GitLab buckets** via in-cluster `mc`
     (`gitlab-lfs`, `gitlab-artifacts`, `gitlab-uploads`,
     `gitlab-packages`, `gitlab-backups`,
     `gitlab-terraform-state`, `gitlab-ci-secure-files`,
     `gitlab-pages`, `gitlab-dependency-proxy`,
     `gitlab-snippets`, `gitlab-registry`).
  3. **Materialise the dual-key Secret** `gitlab-rails-storage`
     in the `gitlab` namespace. Two keys:
     - `connection` — Rails-side Fog/AWS provider schema
       (`provider: AWS`, `aws_access_key_id`, etc.). Consumed by
       `appConfig.object_store.lfs/artifacts/uploads/.../backups`
       via `connection.secret` + `connection.key`.
     - `config` — Docker-registry native `s3:` block
       (`bucket`, `accesskey`, `secretkey`, `region`,
       `regionendpoint`, `secure: false`, `v4auth: true`,
       `pathstyle: true`, `rootdirectory: /`). Consumed by
       `registry.storage.secret` + `registry.storage.key: config`.

  Don't merge the two keys — the chart-bundled registry and the
  Rails-side parsers want different schemas; one YAML document
  that satisfies one will confuse the other. The split is owned
  by `phase2/minio.py`; `references/helm-values-gitlab.yaml`
  points the registry at `config` (not the chart default
  `connection`).

## 12. Chart-bundled OpenBao vs bootstrap-installed OpenBao

There are **two** OpenBao deployments in the cluster, with
overlapping but distinct roles:

  | | Bootstrap-installed | Chart-bundled |
  | --- | --- | --- |
  | Chart | `openbao-0.10.1` (our cached copy in `infra/helm-charts/`) | sub-chart of GitLab 10.x |
  | Namespace | `openbao` | `gitlab` |
  | Workload | `StatefulSet/openbao` (1 replica, no HA) | `Deployment/gitlab-openbao` (2 replicas) |
  | Storage backend | PostgreSQL (`postgresql-cnpg-rw.postgresql.svc.cluster.local`, database `openbao`, role `openbao`) | PostgreSQL (same cluster, same database) |
  | Used by | The bootstrap itself (`OpenBaoClient`), the `blueprint-secrets` CLI, the developer | The GitLab rails app, for the GitLab **Secrets Manager** API |
  | Public UI? | YES — Gateway listener `https://openbao.local.bruj0.net` | NO — internal Service only |
  | Root token | `infra/secrets/openbao-init.json` | Part of the chart's chart-managed Secrets (`gitlab-openbao-secret`, snapshotted to `gitlab-runtime-secrets.yaml` via `persistent_secrets.py`) |

  The bootstrap-installed OpenBao is the **hand-off point** for
  shared bootstrap state — GitLab's initial root password, the
  Runner registration token. The chart-bundled OpenBao is a
  runtime dependency of the rails app, and the chart owns its
  lifecycle (including its persistence — see
  `phase2/persistent_secrets.py` for how it lands in
  `gitlab-runtime-secrets.yaml` so a cluster recreate keeps the
  Secrets Manager content).

  The two deployments don't interfere because they're in
  different namespaces with different Service names (`openbao`
  vs `gitlab-openbao`). The chart's `global.openbao.install:
  true` setting is what causes the chart-bundled one to deploy.

## 13. Runner URL (where the Runner pod registers)

`phase2/runner.py` installs the chart-bundled GitLab Runner
sub-chart with:

  ```yaml
  gitlab-runner:
    gitlabUrl: http://gitlab-webservice-default.gitlab.svc:8181
    unregisterRunners: false
    runners: { ... }
  ```

  Why these particular values:

  - **`gitlabUrl` is `http://` on port 8181, not `https://` on
    443 or the public hostname.** Port 8181 is the chart's
    in-cluster workhorse port; it speaks plain HTTP. TLS
    terminates at the Envoy Gateway *above* it. Routing the
    pod through `gitlab.local.bruj0.net:443` would require the
    pod to resolve that hostname (no CoreDNS rewrite inside
    the cluster → resolves to 127.0.0.1 → broken) AND trust the
    wildcard CA (extra config). Neither is worth the trouble for
    what is a privileged, in-cluster call.
  - **`unregisterRunners: false` keeps `helm upgrade` from
    wiping the registration token on every chart upgrade.** The
    default behaviour is "unregister all runners on helm
    uninstall/upgrade", which would make every `helm upgrade
    gitlab` round-trip re-register the runner (and break
    in-flight jobs).

  If you find yourself wanting to change `gitlabUrl`, **don't**
  flip it back to the public hostname — see the in-cluster rule
  in `AGENTS.md § Other rules`. The current value is the result
  of a multi-attempt debug session, not a guess.

## 14. NodePort data-plane exposure (kind + chart 10.x)

Chart 10.x's bundled envoy-gateway sub-chart defaults the
**data-plane** Service to `type: LoadBalancer`, expecting a
load-balancer provisioner (MetalLB, cloud-provider-kind, …) to
allocate an external IP for each Gateway. kind has no such
provisioner — the data-plane Service never gets a `status.loadBalancer`
entry, the Gateway condition reports `AddressNotUsable`, and
listener program-failure cascades into `PROGRAMMED=False`. The
control-plane Service (named `envoy-gateway`) is still ClusterIP
and reaches its own pods fine; it's the data-plane Service
(visible after the first Gateway is reconciled, named like
`envoy-gitlab-gitlab-gw-<id>`) that's wedged.

We rewire the data-plane to NodePort and pin each listener's
`nodePort` so kind's `extraPortMappings` can target fixed ports
in its control-plane container:

  - **kind side** —
    [`infra/tofu/cluster.tf`](../infra/tofu/cluster.tf)
    configures the kind control-plane's `extraPortMappings` to
    bind the host's loopback interface to three fixed NodePort
    ranges — host `80` → control-plane container `30080`,
    host `443` → container `30443`, host `22` → container
    `30022`. (These are mapped **to** the NodePort values
    Envoy assigns, **not** to the listener-port values — see
    below.) Trailing comments explain why the swap.

  - **chart side** —
    [`infra/scripts/bootstrap/phase2/references/helm-values-gitlab.yaml`](../infra/scripts/bootstrap/phase2/references/helm-values-gitlab.yaml)
    overrides
    `gatewayApiResources.envoy.proxySpec.provider.kubernetes.envoyService.type`
    to `NodePort` and pins each listener's `nodePort` to the
    same 30080/30443/30022 values. The EnvoyProxy CR — which
    the chart renders from this spec, in
    `gitlab/templates/envoy/proxy.yaml` — is what tells
    Envoy Gateway to publish the data-plane Service as
    NodePort with those specific port mappings.

The Gateway's listeners have `port: 80` / `port: 443` /
`port: 22` (service-port numbers); the `nodePort:` field is
separate and only meaningful when the data-plane Service type
is `NodePort`. Without explicit `nodePort:` values, the chart
would pick random ports in the 30000–32767 range and the
`extraPortMappings` pair-up wouldn't match — the kind host
forward would target ports nothing listens on.

The trade-off vs MetalLB / cloud-provider-kind:

  - **MetalLB** (BGP / L2 mode) gives each LoadBalancer
    Service a routable IP. Heavier: a separate DaemonSet, a
    config block in the chart, and a new operator to debug
    when something breaks. We don't need it.
  - **cloud-provider-kind** injects `<NodeIP>:NodePort`-style
    endpoints directly into LoadBalancer Services. Cleaner
    UX (one less chart override) but **newer and less
    battle-tested** than NodePort, and the
    `extraPortMappings` paths still have to exist for SSH /
    KAS / container-registry ports that aren't LoadBalancer
    Services in the first place.
  - **kind `extraPortMappings` + chart `NodePort`** is the
    pattern the kind upstream recommends for any chart that
    builds on Gateway API. Two-line change per side; no new
    control-plane components.

### Reaching the GitLab hostnames

Once the data-plane is reachable on host:443, the README
post-install flow works unchanged: `/etc/hosts` points
`gitlab.local.bruj0.net` at `127.0.0.1`, browser hits
`https://gitlab.local.bruj0.net`, traffic lands on the kind
control-plane at 30443, kind kube-proxy forwards to the
data-plane pod on service-port 443, Envoy routes by SNI to
`gitlab-web`.

For in-cluster/CLI tasks that don't go through the Gateway
(the Container Registry API on `127.0.0.1:5000`, the MinIO
S3 endpoint on `127.0.0.1:9000`, the GitLab workhorse HTTP
on `127.0.0.1:8181`, etc.), use the new CLI dispatcher:

  ```sh
  uv run blueprint-secrets port-forward --list           # all targets
  uv run blueprint-secrets port-forward gitlab-registry  # 127.0.0.1:5000
  ```

It picks the tofu-written kubeconfig, the right namespace,
the right Service + cluster-port, and tears the forward down
on Ctrl-C. See `bootstrap/secrets_cli.py` for the registry
and `AGENTS.md § Hard rules` for the rule "don't reintroduce
system-level `kubectl port-forward` scripts" — the CLI is
the supported way.

## 15. Trade-offs (why it's shaped this way)

- **Chart-managed TLS instead of cert-manager.** Public
  Let's Encrypt cannot validate `*.local.bruj0.net` (the host
  has no public DNS). The chart's self-signed path is the
  simplest thing that works today; the moment public DNS is
  delegated, we swap to cert-manager with DNS-01 by changing
  one value (`global.gatewayApi.configureCertmanager: true`)
  + adding an `Issuer`. The rest of the bootstrap doesn't
  change.

- **hvac instead of `kubectl exec ... bao`.** The exec path
  has bitten us twice: the kubelet round-trips every call
  (1–2 s/secret), and the pod's token helper doesn't always
  survive across separate `kubectl exec` invocations (silent
  403s on `kv put`). hvac is the official Python Vault
  client (OpenBao is API-compatible) and gives us structured
  errors. The trade-off: we have to manage a port-forward.
  The `_PortForward` class in `phase2/secrets.py` does that
  lazily per process and tears it down on atexit.

- **One phase 2 step per chart, no composite steps.** The
  pipeline is a flat list of 13 steps today. The temptation
  to collapse steps (e.g. "install GitLab + Runner + capture
  token" into one mega-step) makes failure diagnosis much
  harder. Keep steps fine-grained: one chart per step, with
  post-install work in the same `install()` method.
- **Bootstrap-minted wildcard cert, not the chart's cfssl
  Job.** The chart ships a pre-install `cfssl` Job that mints
  the wildcard cert and writes it to a Secret the Gateway
  listeners reference. That Job **only runs when
  `gitlab.ingress.tls.configured` returns `"false"`** — i.e.
  when no external ingress is set up. With
  `global.gatewayApi.enabled=true`, the chart's
  `gitlab.ingress.tls.configured` getter returns `"true"` and
  the cfssl Job is skipped, leaving the Gateway listeners
  pointing at the wrong Secret name (`gitlab-tls`, the
  cert-manager default). We work around this by minting the
  CA + cert ourselves (`phase2/wildcard_certs.py`), writing
  it to `infra/tls/wildcard/`, and materialising 4 Secrets
  (`gitlab-wildcard-tls`, `registry-tls`, `kas-tls`,
  `minio-tls`) before the GitLab chart install. The values
  override in `references/helm-values-gitlab.yaml` flips
  `global.ingress.tls.secretName: gitlab-wildcard-tls` so the
  Gateway listeners find our Secret. Idempotent: the cert
  has 10-year validity and the installer skips regen if
  the cert has ≥30 days left.
- **Stable PVs survive `tofu destroy`, but `--destroy` wipes
  them.** The stable PV/PVC pairs (CloudNativePG, Redis,
  MinIO, OpenBao, Gitaly) are bound to hostPath
  dirs under `infra/data/shared/stable/`. After `tofu destroy
  && tofu apply && bootstrap --phase 2`, the new cluster's
  PVCs re-bind to those dirs. But the on-disk PostgreSQL
  data was written with the credentials from the *previous*
  install's chart-minted Secrets — and the chart mints fresh
  random passwords on the next install, so PG logs
  `FATAL: password authentication failed for user "gitlab"`.
  The `persistent_secrets.py:restore()` step closes that loop
  by re-applying the previous chart-managed Secrets BEFORE
  the chart install (so the chart sees them already exist
  and reuses them). For chart 10.x the externalised services
  (CloudNativePG/Redis/MinIO) have an additional layer: the
  bootstrap re-creates their PG roles + databases from a host
  snapshot (see `cnpg-role-passwords.json`) when the Cluster
  was recreated but `--destroy` wasn't run. The inverse case
  (testing fresh install on top of stale data) is broken by
  design: that's what `bootstrap --destroy` is for.
- **`*.local.bruj0.net` resolves on the host, not in the
  cluster.** The Gateway has 4 listeners, one per hostname
  (`gitlab`, `registry`, `kas`, `minio`). Browsers reach
  Envoy via the host's `/etc/hosts` mapping. Inside the
  cluster, pods use Service DNS (`gitlab-webservice-default.gitlab.svc:8181`).
  For curl from the host with port-forward, use
  `curl --resolve <host>:8443:127.0.0.1 https://<host>:8443/...`
  so the SNI matches the listener hostname — without SNI,
  Envoy returns `filter_chain_not_found` and the TLS
  handshake fails (`unexpected eof while reading`).

- **Vendored CRDs, not the upstream tarball.** The
  `gateway-api-crds/generated/` directory has 8 chart-shipped
  Envoy policy CRDs (Backends, BackendTrafficPolicies,
  ClientTrafficPolicies, EnvoyExtensionPolicies,
  EnvoyPatchPolicies, EnvoyProxies, HTTPRouteFilters,
  SecurityPolicies). The 1 upstream standard-channel CRD set
  (Gateway API v1.5.0) is applied directly from the
  `https://github.com/kubernetes-sigs/gateway-api/...`
  URL — it doesn't change between chart upgrades, so vendoring
  it just bloats the repo. We don't fetch the chart's CRDs
  from a tarball at install time because that would (a) add a
  network dependency the rest of the pipeline doesn't have,
  (b) pull in CRDs the chart doesn't need (TCPRoute,
  UDPRoute, BackendTLSPolicy, etc. — all experimental), and
  (c) make `kubectl diff` noisy. Re-vendoring when versions
  bump is a manual `curl -sL <url> | yq` against the
  chart's `crds/` dir + the chart 10.x nested layout
  (`gitlab/charts/gateway-helm/charts/crds/crds/`).

- **Idempotency over speed.** A few steps could be faster if
  we skipped the "is this already done?" probe (the openbao
  init probe is the obvious one). We keep them anyway
  because the failure mode they prevent — partial state from
  a previous interrupted run — is much worse than the 1–2 s
  the probe costs. Re-runs are safe and resumable, which is
  the property that makes the iteration loop work.
