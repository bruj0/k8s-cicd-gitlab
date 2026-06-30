# Phase 2 — Install GitLab, Runner, OpenBao on a Phase-1 cluster

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

  - **OpenBao** running in the `openbao` namespace, initialised +
    unsealed, with a KV-v2 mount at `secret/` and the root token
    persisted to `infra/secrets/openbao-init.json` (gitignored,
    mode 0600).
  - **GitLab CE** running in the `gitlab` namespace. The chart
    sub-installs **Envoy Gateway 1.7.1** as a managed sub-chart
    (`gateway-helm`) and mints a **self-signed wildcard cert**
    for `*.local.bruj0.net` via a pre-install cfssl Job.
  - **GitLab Runner** in `gitlab-runner`, registered against
    GitLab using a token that was captured into OpenBao at
    `secret/gitlab/runner/registration_token` during the GitLab
    install step.
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
| `https://minio.local.bruj0.net`            | `root` / OpenBao secret                            | MinIO (LFS, artifacts, packages) |
| `https://openbao.local.bruj0.net`          | root token in `infra/secrets/openbao-init.json`    | OpenBao UI                       |

## 2. The 5-step pipeline

```
1. Pre-flight          cluster + helm reachable
2. Gateway CRDs        upstream standard + 2 chart-shipped Envoy CRDs
3. OpenBao             install + init + unseal
4. GitLab              chart + Envoy sub-chart + self-signed cert + token capture
5. GitLab Runner       install with the token from OpenBao
```

Each step is one method on `Phase2Pipeline` (`_step_*`) and one
`XxxInstaller` class. The pipeline owns ordering, logging, and
error reporting; each installer owns the actual work.

The pipeline is **idempotent** at the step level: re-running
resumes from the failed step and the success path is a no-op.
Idempotency is achieved by:

  - probing the cluster (`kubectl get`) before doing anything;
  - reading the init JSON / KV state to detect "already done";
  - using `helm upgrade --install` (creates or upgrades, never
    errors on existing release);
  - `kubectl apply --server-side --force-conflicts` for the
    CRDs (re-applying is fine, the server resolves conflicts).

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
├── secrets_cli.py                # click wrapper: `blueprint-secrets` (post-install helper)
├── app.py                        # composition root — wires Phase2Installers into BootstrapApp
├── app_installer.py              # base class HelmAppInstaller + generic helm plumbing
├── phase2/
│   ├── pipeline.py               # Phase2Pipeline — orchestrates the 5 steps
│   ├── catalog.py                # Phase2Installers dataclass — bundle of every installer
│   ├── gateway.py                # GatewayCRDsInstaller
│   ├── openbao.py                # OpenBaoInstaller (chart + init + unseal)
│   ├── gitlab.py                 # GitlabInstaller (chart + token capture)
│   ├── runner.py                 # GitLabRunnerInstaller
│   ├── secrets.py                # OpenBaoClient (hvac + auto port-forward)
│   └── references/
│       ├── helm-values-openbao.yaml
│       ├── helm-values-gitlab.yaml
│       ├── helm-values-runner.yaml
│       └── gateway-api-crds/    # 3 vendored CRD files
│                                  #   - gatewayapi-crds.yaml (upstream standard)
│                                  #   - gateway.envoyproxy.io_envoyproxies.yaml
│                                  #   - gateway.envoyproxy.io_clienttrafficpolicies.yaml
```

### Who calls who

```
BootstrapApp (app.py)
  └─> Phase2Pipeline (phase2/pipeline.py)
        ├─> _step_preflight      → CommandRunner probes
        ├─> _step_gateway_crds   → GatewayCRDsInstaller
        ├─> _step_openbao        → OpenBaoInstaller
        │                            └─> OpenBaoClient (init, unseal, kv mounts)
        ├─> _step_gitlab         → GitlabInstaller
        │                            └─> OpenBaoClient (write root password + capture runner token)
        └─> _step_runner         → GitLabRunnerInstaller
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
`Phase 2 install failed at step N/5: <error>`. The mapping
from step to source file is:

| Step | Source                                                       |
| ---- | ------------------------------------------------------------ |
| 1/5  | `phase2/pipeline.py:_step_preflight`                          |
| 2/5  | `phase2/gateway.py` + `phase2/references/gateway-api-crds/`   |
| 3/5  | `phase2/openbao.py` + `phase2/secrets.py` (the `OpenBaoClient`) + `phase2/references/helm-values-openbao.yaml` |
| 4/5  | `phase2/gitlab.py` + `phase2/references/helm-values-gitlab.yaml` |
| 5/5  | `phase2/runner.py` + `phase2/references/helm-values-runner.yaml` |

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

## 10. Trade-offs (why it's shaped this way)

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
  pipeline is a flat list of 5 steps today. The temptation to
  collapse steps (e.g. "install GitLab + Runner + capture
  token" into one mega-step) makes failure diagnosis much
  harder. Keep steps fine-grained: one chart per step, with
  post-install work in the same `install()` method.

- **Vendored CRDs, not the upstream tarball.** The
  `gateway-api-crds/` directory has 3 files: 1 upstream
  standard channel + 2 chart-shipped Envoy CRDs. We don't
  fetch the upstream tarball at install time because that
  would (a) add a network dependency that the rest of the
  pipeline doesn't have, (b) pull in CRDs the chart doesn't
  need (TCPRoute, UDPRoute, BackendTLSPolicy, etc. — all
  experimental), and (c) make `kubectl diff` noisy.
  Re-vendoring when versions bump is a manual
  `curl -sL <url> | yq` against the upstream manifest +
  the chart's `crds/` dir.

- **Idempotency over speed.** A few steps could be faster if
  we skipped the "is this already done?" probe (the openbao
  init probe is the obvious one). We keep them anyway
  because the failure mode they prevent — partial state from
  a previous interrupted run — is much worse than the 1–2 s
  the probe costs. Re-runs are safe and resumable, which is
  the property that makes the iteration loop work.
