---
name: provision-gitlab
description: 'Install Phase 2 of the blueprint on a running Phase-1 kind cluster: GitLab CE, Runner, OpenBao, Traefik, and Gateway API routing. Iterates via `bootstrap.py --phase 2`; every step is idempotent so re-runs are safe.'
---

# Provision GitLab (Phase 2)

Install the rest of the blueprint on top of a working Phase-1 kind
cluster. Drives the iteration loop: `bootstrap.py --phase 2`, observe,
fix, re-run.

This skill is the **source of truth** for known-good Phase 2
configuration. The Python install logic lives in
`infra/scripts/bootstrap/phase2/`; the install-time configuration
(YAML values, Gateway + HTTPRoute manifests) lives in
`infra/scripts/bootstrap/phase2/references/`. **When you fix a problem,
update both the relevant source AND the "Common pitfalls" section
below** — that is what makes this skill converge to one-shot for any
fresh Phase-1 cluster.

## Pre-flight

```sh
cd blueprint
export KUBECONFIG=$PWD/infra/tofu/kubeconfig

# Phase 1 must already be done: 5-node kind cluster up, kubectl reachable,
# Phase-1 wildcard cert present at infra/tls/public/ca.crt.
python3 infra/scripts/bootstrap.py --phase 2 --check
```

`--check` runs only the pre-flight (cluster + helm reachable). If it
fails, fix the underlying issue before continuing.

## Install (one-shot when the pitfalls below are known)

```sh
python3 infra/scripts/bootstrap.py --phase 2
```

Eight steps, all idempotent. Re-running the command after a partial
failure resumes from the failed step.

| Step | What it does |
| ---- | ------------ |
| 1/8  | Pre-flight (cluster + helm reachable) |
| 2/8  | Publish the Phase-1 wildcard TLS Secret into `gitlab` + `openbao` namespaces |
| 3/8  | Install the upstream Gateway API CRDs (standard channel) — NOT shipped by Traefik's chart |
| 4/8  | Install Traefik with the Gateway API provider enabled |
| 5/8  | Install + initialise + unseal OpenBao |
| 6/8  | Apply Gateway + HTTPRoute manifests (Traefik routes `*.local.bruj0.net`) |
| 7/8  | Install GitLab CE; set the root password via Rails; capture the runner registration token into OpenBao |
| 8/8  | Install GitLab Runner (uses the registration token from OpenBao) |

## Smoke tests

Run these after install completes; all six should pass without manual
intervention:

```sh
# 1. All Phase-2 workloads are Running.
kubectl -n traefik     get pods
kubectl -n openbao     get pods
kubectl -n gitlab      get pods
kubectl -n gitlab-runner get pods

# 2. HTTPRoutes are bound to our Traefik GatewayClass.
kubectl get httproute -A
kubectl get gateway -A
# Expected:
#   traefik/local-bruj0-net   CLASS=traefik   ADDRESS=pending-or-node-IP
#   httproute/gitlab          accepted by Traefik
#   httproute/openbao         accepted by Traefik

# 3. Runner registered against GitLab.
kubectl -n gitlab exec deploy/gitlab-toolbox -- \
  bash -lc 'gitlab-rails runner "puts Ci::Runner.all.map { |r| %Q[#{r.description} (#{r.active})] }"'
# Expected: a line per runner ending in "(true)".

# 4. OpenBao has the GitLab initial password + runner token.
kubectl -n openbao exec openbao-0 -- bao kv get -format=json secret/gitlab        | jq -r '.data.data.initial_root_password'
kubectl -n openbao exec openbao-0 -- bao kv get -format=json secret/gitlab/runner | jq -r '.data.data.registration_token'

# 5. GitLab UI is reachable via Traefik (port-forward if your kind node IPs
#    aren't routable from this host).
kubectl -n traefik port-forward svc/traefik 18443:443 &
curl -ksf -H 'Host: gitlab.local.bruj0.net' https://localhost:18443/-/health | jq .

# 6. OpenBao UI is reachable.
curl -ksf -H 'Host: openbao.local.bruj0.net' https://localhost:18443/ | jq .
```

## Iteration loop

When a step fails:

1. **Read the failure** (`Phase 2 install failed: <error>`).
2. **Map to the component** — each step has exactly one source file:
   - Step 3 (CRDs)         → `phase2/gateway.py` (`ensure_crds`)
   - Step 4 (Traefik)      → `phase2/traefik.py` OR `references/helm-values-traefik.yaml`
   - Step 5 (OpenBao)      → `phase2/openbao.py`, `phase2/secrets.py`, or `references/helm-values-openbao.yaml`
   - Step 6 (Gateway)      → `references/gateway.yaml` or `references/httproute-*.yaml`
   - Step 7 (GitLab)       → `phase2/gitlab.py` or `references/helm-values-gitlab.yaml`
   - Step 8 (Runner)       → `phase2/runner.py` or `references/helm-values-runner.yaml`
3. **Re-run** `python3 infra/scripts/bootstrap.py --phase 2`. Every step
   has its own idempotency probe (e.g. OpenBao init only fires when
   `infra/secrets/openbao-init.json` is missing).
4. **Append one line to "Common pitfalls" below** describing:
   `symptom → root cause → fix (file)` so the next run is one-shot.

## Canonical (known-good) pinned versions

These are the versions that have been observed to install end-to-end
on a 5-node Phase-1 cluster. Bumping one of these is the most common
source of a new failure mode — check this list before chasing a deeper
cause.

| Chart            | Version | Repo                                  | Notes                                                                |
| ---------------- | ------- | ------------------------------------- | -------------------------------------------------------------------- |
| Traefik          | `41.0.1`   | `https://traefik.github.io/charts`    | App v3.7.5. Strict-schema values — use only what the chart's own values.yaml accepts. |
| Gateway API CRDs | `v1.2.1`   | upstream `standard-install.yaml`      | Installed by `phase2/gateway.py:ensure_crds` from the upstream URL.   |
| OpenBao          | `0.10.1`   | `https://openbao.github.io/openbao-helm` | Server image `2.2.0`. Single-replica, HA disabled.                  |
| GitLab           | `9.11.7`   | `https://charts.gitlab.io`            | **Use `charts.gitlab.io`, NOT the deprecated `gitlab.gitlab.io/charts` (returns 403).** GitLab v18.11.6. |
| GitLab Runner    | `0.71.0`   | `https://charts.gitlab.io`            | Kubernetes executor. Token is fetched from OpenBao at install time.  |

If a sub-chart's `email` field is required (cert-manager-style), set
`sub-chart.email: dev@local.bruj0.net` — TLS is terminated by Traefik,
so the actual issuer is never used.

## Common pitfalls (frozen — append, don't rewrite)

Each entry below is one observation from the iteration loop, written
once and never re-edited. Future readers can correlate the symptom,
edit the right file, and move on.

- `helm pull: chart "traefik" matching 36.4.0 not found` → bump `traefik.chart_version` in `infra/scripts/bootstrap/VERSIONS.json` to `41.0.1` (chart schema and CLI flags changed in v37+).
- `values don't meet the specifications ... ports.web: additional properties 'redirectTo' not allowed` (Traefik ≥ 37) → `helm-values-traefik.yaml`: rewrite the HTTP→HTTPS redirect as `ports.web.http.redirections.entryPoint.websecure: { to: websecure, permanent: true, scheme: https }`. The legacy `ports.web.redirectTo` key was removed.
- `Cannot create Service traefik without ports` → make sure at least one port has `expose.default: true` (or omit `expose` entirely). Setting **all** three to `false` leaves the Service with no port block. Only `traefik` (dashboard) should be `expose.default: false`.
- `no matches for kind "GatewayClass" in version "gateway.networking.k8s.io/v1"` during `helm upgrade` of Traefik → install the upstream Gateway API CRDs first. The Traefik chart does **not** ship them. The bootstrap does this in step 3 via `phase2/gateway.py:ensure_crds` pulling `https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml`.
- `error: timed out waiting for the condition on pods/openbao-0` (OpenBao chart's readiness probe fails until unsealed) → bootstrap now waits only for `phase=Running` before running `bao operator init`, then unseals, then waits for `Ready`. See `phase2/openbao.py:_wait_for_pod_running` vs `_wait_for_pod_ready`. Don't try to install cert-manager to "make readiness pass" — the init-then-unseal sequence is the answer.
- `bao kv put secret/...: permission denied` → the OpenBao client must (a) read the root token from `infra/secrets/openbao-init.json` and (b) `bao login` once with `kubectl exec -i … stdin=token` (the `-i` matters!). See `phase2/secrets.py:OpenBaoClient.login`.
- `bao kv put secret/...: 404 no handler for path "secret/..."` → OpenBao boots with **no mounts**. Run `bao secrets enable -path secret -version=2 kv` once via `OpenBaoClient.enable_kv_v2("secret")`. Idempotent against re-runs.
- `gitlab.gitlab.io/charts/index.yaml : 403 Forbidden` when running `helm repo add` → GitLab moved their chart repo. Use `https://charts.gitlab.io` (without `gitlab.gitlab.io/`). Old pinned URL `https://gitlab.gitlab.io/charts` is deprecated.
- `chart "gitlab" matching 8.10.0 not found` (in `charts.gitlab.io`) → the GitLab chart jumped major versions when it moved repos. Use `9.11.7` (covers GitLab v18.11.x).
- `You must provide an email to associate with your TLS certificates. Please set certmanager-issuer.email` (GitLab chart template) → the GitLab chart has `certmanager-issuer` as an **unconditional** sub-chart dependency. Set `certmanager-issuer.email: dev@local.bruj0.net` in `references/helm-values-gitlab.yaml`. The actual issuer is never used (Traefik terminates TLS).
- `undefined method 'initial_root_password' for an instance of ApplicationSetting` (GitLab ≥ 17) → that method is gone. Bootstrap now sets `User.password` + `password_automatically_set=false` via `gitlab-rails runner`, then stores the password in OpenBao at `secret/gitlab/initial_root_password`. Don't try to read initial_root_password from the K8s Secret — the chart no longer writes it.
- `undefined method 'keys' for an instance of ApplicationSetting` → use `ApplicationSetting.current.attributes.keys` or skip the introspection entirely. The bootstrap never needs to read ApplicationSetting; it only writes a User password.
- `kubectl exec --namespace gitlab deploy/gitlab-toolbox -- gitlab-rails runner -e "..."` → `command terminated with exit code 1` with **no** stdout/stderr (the Ruby error code was lost). Always go through a `bash -lc` intermediate (and `gitlab-rails runner "Ruby script"` with the script as a single quoted string) so the container's actual exit code surfaces. See `phase2/gitlab.py:_capture_credentials` and `_ensure_initial_password`.
- `a bytes-like object is required, not 'str'` from `helm list --output json | json.loads(...)` → `SubprocessRunner` was leaving `subprocess.CompletedProcess.stdout/stderr` as `bytes`. Fixed at the `_finalise` boundary in `bootstrap/shell.py` so both real and dry-run runners return `str` consistently. If you see this error after touching the shell module, check the `_decode` helper exists there.
- OpenBao pod in `Running 0/1` phase is **expected** before init runs. Don't restart it; just run `bao operator init` once.
- HTTPRoute binding shows `PROGRAMMED=Unknown` indefinitely → Traefik's Status condition only flips once a hostname actually resolves. With kind + port-forward, that resolves on first request; on bare clusters it may need a `resolver`. Don't chase this — verify with `curl` instead.
- `+++/etc/hosts: Permission denied` when adding `gitlab.local.bruj0.net` → add the entry with `sudo`: `echo "127.0.0.1 gitlab.local.bruj0.net openbao.local.bruj0.net" | sudo tee -a /etc/hosts`. Adjust the IPs if you want node-IP routing instead of port-forward.

## When the install is green

All smoke tests pass with no manual intervention. Commit the changes
(charts, YAML references, and any **new** "Common pitfalls" entries).
Future Phase-2 installs on a fresh Phase-1 cluster should converge to
one-shot.

## How to undo

```sh
# Drop every Phase-2 chart release (OpenBao init JSON stays on disk
# until the next step, so you can recover unseal keys if you want).
helm uninstall -n traefik      traefik
helm uninstall -n openbao      openbao
helm uninstall -n gitlab       gitlab
helm uninstall -n gitlab-runner gitlab-runner

# Wipe the Gateway / HTTPRoutes / namespaces / custom names.
kubectl delete -f infra/scripts/bootstrap/phase2/references/
kubectl delete -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml   # optional
kubectl delete namespace traefik openbao gitlab gitlab-runner 2>/dev/null

# Drop the secret-bootstrap state.
rm -rf infra/secrets/

# Phase 1 cluster stays up.
```

After undo, a fresh `python3 infra/scripts/bootstrap.py --phase 2`
re-installs everything.
