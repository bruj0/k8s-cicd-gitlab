---
name: provision-gitlab
description: 'Install Phase 2 of the blueprint on a running Phase-1 kind cluster: GitLab CE, Runner, OpenBao, and the chart-managed Envoy Gateway that fronts *.local.bruj0.net. Iterates via `uv run blueprint-bootstrap --phase 2`; every step is idempotent so re-runs are safe.'
---

# Provision GitLab (Phase 2)

Install the rest of the blueprint on top of a working Phase-1 kind
cluster. Drives the iteration loop: `uv run blueprint-bootstrap --phase 2`,
observe, fix, re-run.

This skill is the **source of truth** for known-good Phase 2
configuration. The Python install logic lives in
`infra/scripts/bootstrap/phase2/`; the install-time configuration
(YAML values, vendored Gateway API CRDs) lives in
`infra/scripts/bootstrap/phase2/references/`. **When you fix a problem,
update both the relevant source AND the "Common pitfalls" section
below** — that is what makes this skill converge to one-shot for any
fresh Phase-1 cluster.

## Pre-flight

```sh
cd blueprint
export KUBECONFIG=$PWD/infra/tofu/kubeconfig

# One-time: install the bootstrap's Python deps into .venv (committed
# lockfile means this is reproducible).
uv sync

# Phase 1 must already be done: 5-node kind cluster up, kubectl reachable.
uv run blueprint-bootstrap --phase 2 --check
```

`--check` runs only the pre-flight (cluster + helm reachable). If it
fails, fix the underlying issue before continuing.

## Install (one-shot when the pitfalls below are known)

```sh
uv run blueprint-bootstrap --phase 2
```

Five steps, all idempotent. Re-running the command after a partial
failure resumes from the failed step.

| Step | What it does |
| ---- | ------------ |
| 1/5  | Pre-flight (cluster + helm reachable) |
| 2/5  | Install the Gateway API CRDs (upstream standard channel + the 2 chart-shipped Envoy CRDs the GitLab chart needs) |
| 3/5  | Install + initialise + unseal OpenBao |
| 4/5  | Install GitLab CE (the chart sub-installs Envoy Gateway 1.7.1 and mints a self-signed wildcard cert for `*.local.bruj0.net` via its pre-install cfssl Job); set the root password via Rails; capture the runner registration token into OpenBao |
| 5/5  | Install GitLab Runner (uses the registration token from OpenBao) |

## Smoke tests

Run these after install completes; all six should pass without manual
intervention:

```sh
# 1. All Phase-2 workloads are Running.
kubectl -n openbao        get pods
kubectl -n gitlab         get pods
kubectl -n gitlab-runner  get pods
# Envoy Gateway (managed by the GitLab chart's sub-chart) lives
# under the gateway-helm release in the same namespace as the
# gateway API resources it serves — usually `gitlab`:
kubectl -n gitlab get pods -l app.kubernetes.io/name=gateway-helm

# 2. The chart-managed Gateway + HTTPRoute are bound.
kubectl get gateway,httproute -A
# Expected: one Gateway (PROGRAMMED=True) + GitLab HTTPRoute
# referencing it.

# 3. Runner registered against GitLab.
kubectl -n gitlab exec deploy/gitlab-toolbox -- \
  bash -lc 'gitlab-rails runner "puts Ci::Runner.all.map { |r| %Q[#{r.description} (#{r.active})] }"'
# Expected: a line per runner ending in "(true)".

# 4. OpenBao has the GitLab initial password + runner token.
#    hvac auto-port-forwards 127.0.0.1:8200 — no kubectl exec needed.
uv run blueprint-secrets read gitlab initial_root_password
uv run blueprint-secrets read gitlab/runner registration_token

# 5. GitLab UI is reachable via Envoy Gateway (port-forward if your
#    kind node IPs aren't routable from this host — see URL table
#    below for the canonical alternatives).
kubectl -n gitlab port-forward svc/gitlab-webservice-default 18443:8181 &
curl -ksf https://localhost:18443/-/health | jq .

# 6. OpenBao UI is reachable.
uv run blueprint-secrets ui    # prints the URL + root token, keeps the forward alive
```

## URLs you can reach after install

All of these are `https://<hostname>/`, served by the chart's
Envoy Gateway sub-chart on the kind cluster, terminated with the
self-signed wildcard cert the chart minted in step 4. Trust the
chart's CA on your host first (one-time):

```sh
kubectl -n gitlab get secret gitlab-wildcard-tls-ca \
  -o jsonpath='{.data.cfssl_ca}' | base64 -d > infra/tls/public/ca.crt
sudo trust anchor infra/tls/public/ca.crt
```

| URL                                  | Login                                              | What it is                                |
| ------------------------------------ | -------------------------------------------------- | ----------------------------------------- |
| `https://gitlab.local.bruj0.net`           | `root` / OpenBao secret                            | GitLab web UI + API                       |
| `https://registry.local.bruj0.net`         | `root` / OpenBao secret                            | GitLab Container Registry                 |
| `https://kas.local.bruj0.net`              | `root` / OpenBao secret                            | GitLab Agent Server (KAS)                 |
| `https://minio.local.bruj0.net`            | `root` / OpenBao secret                            | MinIO (LFS, artifacts, packages)          |
| `https://openbao.local.bruj0.net`          | root token from `infra/secrets/openbao-init.json`  | OpenBao UI                                |

The hostnames resolve on the **developer's machine** via
`/etc/hosts` (one-time, see below). Inside the cluster, pods use
Service DNS (`gitlab-webservice-default.gitlab.svc:8181`,
`openbao.openbao.svc:8200`, etc.) — `*.local.bruj0.net` does
**not** resolve in-cluster.

```sh
# One-time: map the wildcard to 127.0.0.1 so the browser reaches
# the Envoy Gateway on the kind node.
echo "127.0.0.1 gitlab.local.bruj0.net registry.local.bruj0.net \
             kas.local.bruj0.net minio.local.bruj0.net openbao.local.bruj0.net" \
  | sudo tee -a /etc/hosts
```

## Iteration loop

When a step fails:

1. **Read the failure** (`Phase 2 install failed: <error>`).
2. **Map to the component** — each step has exactly one source file:
   - Step 2 (CRDs)         → `phase2/gateway.py` (`GatewayCRDsInstaller`) or `references/gateway-api-crds/`
   - Step 3 (OpenBao)      → `phase2/openbao.py` (chart install / init / unseal) or `phase2/secrets.py` (`OpenBaoClient`, hvac) or `references/helm-values-openbao.yaml`
   - Step 4 (GitLab)       → `phase2/gitlab.py` or `references/helm-values-gitlab.yaml`
   - Step 5 (Runner)       → `phase2/runner.py` or `references/helm-values-runner.yaml`
3. **Re-run** `uv run blueprint-bootstrap --phase 2`. Every step
   has its own idempotency probe (e.g. OpenBao init only fires when
   `infra/secrets/openbao-init.json` is missing; `kv v2 enable` only
   if the mount isn't present).
4. **Append one line to "Common pitfalls" below** describing:
   `symptom → root cause → fix (file)` so the next run is one-shot.

## Canonical (known-good) pinned versions

These are the versions that have been observed to install end-to-end
on a 5-node Phase-1 cluster. Bumping one of these is the most common
source of a new failure mode — check this list before chasing a deeper
cause.

| Chart            | Version | Repo                                  | Notes                                                                |
| ---------------- | ------- | ------------------------------------- | -------------------------------------------------------------------- |
| Gateway API CRDs | `v1.2.1`   | upstream `standard-install.yaml`      | Installed by `phase2/gateway.py:GatewayCRDsInstaller` from the upstream URL. |
| OpenBao          | `0.10.1`   | `https://openbao.github.io/openbao-helm` | Server image `2.2.0`. Single-replica, HA disabled.                  |
| GitLab           | `9.11.7`   | `https://charts.gitlab.io`            | **Use `charts.gitlab.io`, NOT the deprecated `gitlab.gitlab.io/charts` (returns 403).** GitLab v18.11.6. Sub-installs Envoy Gateway 1.7.1 via its `gatewayApi` sub-chart; mints the self-signed wildcard cert via a pre-install cfssl Job. |
| GitLab Runner    | `0.71.0`   | `https://charts.gitlab.io`            | Kubernetes executor. Token is fetched from OpenBao at install time via `hvac`.  |

The Envoy Gateway sub-chart is **not** pinned separately — its
version is whatever the GitLab chart ships. If you need a
different Envoy version, override it in `references/helm-values-gitlab.yaml`
under `gatewayApi.envoyGateway.image.tag`.

## Common pitfalls (frozen — append, don't rewrite)

Each entry below is one observation from the iteration loop, written
once and never re-edited. Future readers can correlate the symptom,
edit the right file, and move on.

- `error: timed out waiting for the condition on pods/openbao-0` (OpenBao chart's readiness probe fails until unsealed) → bootstrap now waits only for `phase=Running` before running `bao operator init`, then unseals, then waits for `Ready`. See `phase2/openbao.py:_wait_for_pod_running` vs `_wait_for_pod_ready`. Don't try to install cert-manager to "make readiness pass" — the init-then-unseal sequence is the answer.
- `403 Forbidden` on `kv put secret/...` after init → previous versions exec'd `bao login` inside the openbao-0 pod via `kubectl exec`, and the token helper didn't always survive across separate `kubectl exec` calls. Fixed by switching the OpenBao client to `hvac` (token lives in the Python process for the lifetime of `OpenBaoClient`, no token helper involved). See `phase2/secrets.py:OpenBaoClient.login` / `_authenticated`.
- `404 no handler for path "secret/..."` on `kv put` → OpenBao boots with **no mounts**. `OpenBaoClient.enable_kv_v2("secret")` mounts the kv-v2 engine at `secret/`. Idempotent against re-runs.
- `gitlab.gitlab.io/charts/index.yaml : 403 Forbidden` when running `helm repo add` → GitLab moved their chart repo. Use `https://charts.gitlab.io` (without `gitlab.gitlab.io/`). Old pinned URL `https://gitlab.gitlab.io/charts` is deprecated.
- `chart "gitlab" matching 8.10.0 not found` (in `charts.gitlab.io`) → the GitLab chart jumped major versions when it moved repos. Use `9.11.7` (covers GitLab v18.11.x).
- `You must provide an email to associate with your TLS certificates. Please set certmanager-issuer.email` (GitLab chart template) → the GitLab chart has `certmanager-issuer` as an **unconditional** sub-chart dependency. Set `certmanager-issuer.email: dev@local.bruj0.net` in `references/helm-values-gitlab.yaml`. The actual issuer is never used (Envoy terminates TLS).
- `undefined method 'initial_root_password' for an instance of ApplicationSetting` (GitLab ≥ 17) → that method is gone. Bootstrap now sets `User.password` + `password_automatically_set=false` via `gitlab-rails runner`, then stores the password in OpenBao at `secret/gitlab/initial_root_password`. Don't try to read initial_root_password from the K8s Secret — the chart no longer writes it.
- `undefined method 'keys' for an instance of ApplicationSetting` → use `ApplicationSetting.current.attributes.keys` or skip the introspection entirely. The bootstrap never needs to read ApplicationSetting; it only writes a User password.
- `kubectl exec --namespace gitlab deploy/gitlab-toolbox -- gitlab-rails runner -e "..."` → `command terminated with exit code 1` with **no** stdout/stderr (the Ruby error code was lost). Always go through a `bash -lc` intermediate (and `gitlab-rails runner "Ruby script"` with the script as a single quoted string) so the container's actual exit code surfaces. See `phase2/gitlab.py:_capture_credentials` and `_ensure_initial_password`.
- `a bytes-like object is required, not 'str'` from `helm list --output json | json.loads(...)` → `SubprocessRunner` was leaving `subprocess.CompletedProcess.stdout/stderr` as `bytes`. Fixed at the `_finalise` boundary in `bootstrap/shell.py` so both real and dry-run runners return `str` consistently. If you see this error after touching the shell module, check the `_decode` helper exists there.
- OpenBao pod in `Running 0/1` phase is **expected** before init runs. Don't restart it; just run `bao operator init` once.
- `+++/etc/hosts: Permission denied` when adding `gitlab.local.bruj0.net` → add the entry with `sudo`: `echo "127.0.0.1 gitlab.local.bruj0.net openbao.local.bruj0.net" | sudo tee -a /etc/hosts`. Adjust the IPs if you want node-IP routing instead of port-forward.
- **CRITICAL: `*.local.bruj0.net` is for browsers, not for cluster traffic.**
  Pods that try to reach `https://gitlab.local.bruj0.net/...` resolve the
  hostname to `127.0.0.1` (no CoreDNS rewrite exists inside the cluster),
  so the request hits the pod itself and fails. Symptom: gitlab-runner
  logs `dial tcp 127.0.0.1:443: connect: connection refused` and GitLab
  shows `Ci::Runner.all` empty. Fix: pass the **in-cluster Service**
  to every chart value that has a `gitlabUrl`-style flag, e.g.
  `gitlab-webservice-default.gitlab.svc:8181` and
  `openbao.openbao.svc:8200`. Add a CoreDNS rewrite if you really need
  pods to use `*.local.bruj0.net`. See the matching rule in `AGENTS.md`.
- `httproute-openbao.yaml` references `Service/openbao-ui` but
  `kubectl -n openbao get svc` shows only `openbao` (and
  `openbao-internal`). The OpenBao chart's `ui-service.yaml` template
  is rendered with the *fullname* helper, which for release=openbao,
  chart=openbao yields `openbao-openbao-ui` in some chart revisions
  but a bare `openbao-ui` in others — and the v0.10.1 chart we use
  produces `openbao` only (the "ui" service merges into the main
  Service). Fix: `backendRefs.name: openbao` (not `openbao-ui`).
- Gateway listener reports `Ready=False` with reason
  `InvalidCertificateRef` → the listener's
  `tls.certificateRefs[0].name` doesn't match the Secret the
  chart's pre-install Job minted. The chart default is `gitlab-tls`
  (the cert-manager-style name), but the self-signed Job produces
  `gitlab-wildcard-tls`. Fix in
  `references/helm-values-gitlab.yaml`:
  `gatewayApiResources.gateway.listeners.gitlab-web.tls.certificateRefs[0].name: gitlab-wildcard-tls`.
- `ModuleNotFoundError: No module named 'bootstrap.cli'` (or
  `bootstrap.secrets_cli`) from `uv run blueprint-bootstrap` →
  `uv sync` hasn't been run in this checkout, or `.venv/` was
  deleted. Run `uv sync` once; both entry points install into
  `.venv/bin/`.
- `OpenBaoClient` raises `OpenBao rejected root token from
  infra/secrets/openbao-init.json: ...` → the init file is
  stale (server was re-initialised without your knowing), or the
  file is empty / corrupted. Re-run the install (the
  `OpenBaoInstaller` detects a missing init file and reruns init),
  or copy the token from
  `kubectl -n openbao logs openbao-0 | grep 'Root Token'`.
- `port-forward to 127.0.0.1:8200 did not become ready` from
  `blueprint-secrets` → the `openbao` Service isn't running, or
  the cluster context is wrong. Check
  `kubectl -n openbao get svc openbao` and
  `kubectl config current-context`.
- `curl -k https://gitlab.local.bruj0.net/-/health` returns
  `502 Bad Gateway` from Envoy → the chart-managed HTTPRoute isn't
  bound to a healthy backend. Check
  `kubectl -n gitlab get httproute,svc` and confirm
  `gitlab-webservice-default` is `Ready`. Usually means the
  pre-install Job (root password) hasn't completed; wait and retry.

## Rules of thumb (apply when adding Phase-2 charts)

When you set a chart's `*Url`, `*Host`, or `*Endpoint` flag, ask
**who is the caller?** — a developer's browser, or a pod inside the
cluster? Use the rule:

- Browser / host machine → `*.local.bruj0.net` (relies on `/etc/hosts`
  on the developer's laptop; no service mesh, no TLS offloading).
- Pod inside the cluster → in-cluster Service DNS: `svc-name.ns.svc:port`
  (no DNS rewrites required; works on a fresh `kind` cluster).

This applies to at least: GitLab Runner `gitlabUrl`, GitLab `sshHost`,
the future GitLab Runner registration, anything chart-level that takes
a hostname.

## Rules of thumb (apply when adding Phase-2 charts)

When you set a chart's `*Url`, `*Host`, or `*Endpoint` flag, ask
**who is the caller?** — a developer's browser, or a pod inside the
cluster? Use the rule:

- Browser / host machine → `*.local.bruj0.net` (relies on `/etc/hosts`
  on the developer's laptop; no service mesh, no TLS offloading).
- Pod inside the cluster → in-cluster Service DNS: `svc-name.ns.svc:port`
  (no DNS rewrites required; works on a fresh `kind` cluster).

This applies to at least: GitLab Runner `gitlabUrl`, GitLab `sshHost`,
the future GitLab Runner registration, anything chart-level that takes
a hostname.

## When the install is green

All smoke tests pass with no manual intervention. Commit the changes
(charts, YAML references, and any **new** "Common pitfalls" entries).
Future Phase-2 installs on a fresh Phase-1 cluster should converge to
one-shot.

## How to undo

```sh
# Drop every Phase-2 chart release (OpenBao init JSON stays on disk
# until the next step, so you can recover unseal keys if you want).
helm uninstall -n openbao      openbao
helm uninstall -n gitlab       gitlab
helm uninstall -n gitlab-runner gitlab-runner

# Wipe the Gateway API CRDs (upstream standard + the 2 chart-shipped
# Envoy CRDs the GitLab chart needs). Safe to omit if other charts
# on the cluster also use them.
kubectl delete -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml
kubectl delete -f infra/scripts/bootstrap/phase2/references/gateway-api-crds/

# Wipe the namespaces.
kubectl delete namespace openbao gitlab gitlab-runner 2>/dev/null

# Drop the secret-bootstrap state.
rm -rf infra/secrets/

# Phase 1 cluster stays up.
```

After undo, a fresh `uv run blueprint-bootstrap --phase 2`
re-installs everything.
