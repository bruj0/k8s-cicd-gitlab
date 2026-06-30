---
name: provision-phase-1
description: 'Provision Phase 1 of the blueprint: prepare the working tree (prereqs, tfvars, helm chart cache) and create a 5-node kind cluster via OpenTofu. The bootstrap is preparation-only; tofu plan/apply/destroy are manual user steps. Re-runs are safe and idempotent.'
---

# Provision the kind cluster (Phase 1)

Provision the local 5-node kind cluster that Phase 2 stacks on top
of. Drives the iteration loop: `uv run blueprint-bootstrap --phase 1`,
observe, run the printed user steps, fix, re-run.

This skill is the **source of truth** for known-good Phase 1
configuration. The Python install logic lives in
`infra/scripts/bootstrap/` (no `phase2/` subpackage — Phase 1 is
all in the top-level module); the OpenTofu config lives in
`infra/tofu/`; the host-side prerequisites are pinned in
`infra/scripts/bootstrap/VERSIONS.json`. **When you fix a problem,
update both the relevant source AND the "Common pitfalls" section
below** — that is what makes this skill converge to one-shot for any
fresh host.

If you are adding a new prereq tool or extending Phase 1, read
[`docs/phase-1.md`](../../docs/phase-1.md) first — it has the
host requirements + the trade-offs section.

If you are preparing the cluster for the application stack, run
[`provision-gitlab`](../provision-gitlab/SKILL.md) afterwards.

## Pre-flight

```sh
cd blueprint

# One-time: install the bootstrap's Python deps into .venv (committed
# lockfile means this is reproducible).
uv sync

# Phase-1-only check: prereqs present + tofu validate succeeds. Does
# NOT create the cluster, does NOT touch /etc/hosts.
uv run blueprint-bootstrap --phase 1 --check
```

`--check` runs only the pre-flight (host tools present, docker
daemon reachable, tofu init + validate succeed). If it fails,
fix the underlying issue before continuing.

## Install (one-shot when the pitfalls below are known)

```sh
uv run blueprint-bootstrap --phase 1
```

The bootstrap runs **4 preparation steps**, then prints the **6
user steps** the operator must run themselves. Re-running the
command after a partial failure resumes from the failed step.

| Step | Owner | What it does |
| ---- | ----- | ------------ |
| 1/4  | bootstrap | Check / install host prereqs (`docker`, `kubectl`, `kind`, `helm`, `tofu`, `openssl`) |
| 2/4  | bootstrap | Seed `infra/tofu/tofu.tfvars` from `tofu.tfvars.example` (only if missing) |
| 3/4  | bootstrap | Run `tofu init` (download providers) + `tofu validate` (syntax check) — **no `plan`, no `apply`** |
| 4/4  | bootstrap | Cache the Headlamp chart at `infra/helm-charts/headlamp-0.43.0.tgz` (no install) |
| 1/6  | user       | `tofu -chdir=infra/tofu plan` — read the plan carefully |
| 2/6  | user       | `tofu -chdir=infra/tofu apply -auto-approve` — **this is the only step that creates the cluster** |
| 3/6  | user       | `KUBECONFIG=$PWD/infra/tofu/kubeconfig kubectl get nodes` — expect 5 nodes Ready |
| 4/6  | user       | Install Headlamp: `helm upgrade --install headlamp …` (bootstrap prints the exact command) |
| 5/6  | user       | Discover the Headlamp URL (`NODE_PORT` + `NODE_IP` → `http://$NODE_IP:$NODE_PORT`) |
| 6/6  | user       | Mint a Headlamp login token: `kubectl create token headlamp -n headlamp` |

The split between `[bootstrap]` (Steps 1–4) and `[user]` (Steps
1–6) is the spec rule made visible in the terminal. The bootstrap
never runs `tofu apply`; per spec, infrastructure is run manually.

**Re-running is safe.** Steps 1–4 are idempotent: re-running the
bootstrap re-validates prereqs (no-op when present), re-runs
`tofu init` (no-op when providers are cached), re-runs `tofu
validate` (no-op when syntax is good), re-downloads the Headlamp
chart only if missing. Steps 1–6 are also idempotent: re-applying
tofu is a no-op when the cluster is already up; re-installing
Headlamp is a `helm upgrade --install` (no-op when release +
chart + values are unchanged).

The `--user` flag short-circuits the pipeline and only prints the
user-handoff block — useful as a cheat sheet after the prep is
done.

## Smoke tests

After Step 6/6 finishes, all five should pass without manual
intervention:

```sh
export KUBECONFIG=$PWD/infra/tofu/kubeconfig

# 1. The 5 kind containers are running.
docker ps --format '{{.Names}}\t{{.Status}}' | grep ^cicd-
# Expected: cicd-control-plane, cicd-worker, cicd-worker2,
# cicd-worker3, cicd-worker4 (kind 0.27+ drops the "kind-" prefix).

# 2. All 5 nodes Ready.
kubectl get nodes -o wide
# Expected: 5 rows, all STATUS=Ready, 1 control-plane + 4 workers.

# 3. Per-node hostPath mounts are reachable from inside each
#    container. The host source is infra/data/{nodeN,shared}/
#    (the data_root variable in infra/tofu/tofu.tfvars defaults
#    to "../data", which resolves to infra/data/). To verify the
#    mount round-trips, write from the host (needs sudo — see
#    the "common pitfalls" entry for why) and read from inside
#    the container:
echo "probe-$$" | sudo tee infra/data/node1/probe.txt > /dev/null
docker exec cicd-worker cat /var/local/node1/probe.txt
sudo rm -f infra/data/node1/probe.txt
# Expected: prints "probe-<pid>".

# 4. Headlamp is installed + has a NodePort.
kubectl -n headlamp get deploy,svc,pods
# Expected: 1/1 Running pod, Service of type NodePort.

# 5. The Headlamp login token round-trips through the API server.
kubectl create token headlamp -n headlamp
# Expected: prints a JWT (~700 chars). Paste it into the
# dashboard's "Use a token" login form.
```

**Note:** the `*.local.bruj0.net` wildcard cert is **not** part
of Phase 1 anymore. The GitLab chart's pre-install cfssl Job
mints it during Phase 2. Don't look for it under `infra/tls/`.

## URLs you can reach after install

| URL                                    | Login                                | What it is                       |
| -------------------------------------- | ------------------------------------ | -------------------------------- |
| `http://$NODE_IP:$NODE_PORT` (Headlamp) | `kubectl create token` output        | Dashboard for the kind cluster (no TLS yet — Headlamp is plain HTTP on the NodePort) |

`$NODE_IP` is the IP of any kind worker (output of
`kubectl get nodes -o jsonpath='{.items[0].status.addresses[0].address}'`).
`$NODE_PORT` is the NodePort Headlamp was assigned (output of
`kubectl -n headlamp get -o jsonpath='{.spec.ports[0].nodePort}' svc headlamp`).

The bootstrap prints the exact one-liner to discover the URL.
**Do not** point a browser at `*.local.bruj0.net` yet — those
hostnames don't resolve and there's no cert until Phase 2.

## Iteration loop

When a step fails:

1. **Read the failure** (`Phase 1 prep failed at step N/4: <error>`).
2. **Map to the component** — each prep step has exactly one source file:
   - Step 1/4 (prereqs)      → `bootstrap/prereq.py` (`PrereqTool` subclasses + `_PROBES`) or `bootstrap/VERSIONS.json` (for the package-by-OS map)
   - Step 2/4 (tfvars)       → `bootstrap/tofu.py` (`TofuRunner.seed_tfvars_if_missing`) or `infra/tofu/tofu.tfvars.example`
   - Step 3/4 (tofu)         → `bootstrap/tofu.py` (`TofuRunner.init` / `validate`) or `infra/tofu/*.tf` (syntax error)
   - Step 4/4 (chart cache)  → `bootstrap/helm_cache.py` (`HelmChartCache.fetch`) or `bootstrap/VERSIONS.json` (for the chart URL + version)
3. **Re-run** `uv run blueprint-bootstrap --phase 1`. Every step has its own idempotency probe.
4. **Append one line to "Common pitfalls" below** describing: `symptom → root cause → fix (file)` so the next run is one-shot.

For user steps 1–6 (tofu + kubectl + helm), the fix is usually
in `infra/tofu/*.tf` or in the printed command. The bootstrap
prints the exact command for each user step; copy-paste, don't
retype.

## Canonical (known-good) pinned versions

These are the versions that have been observed to install end-to-end
on a 5-node Phase-1 cluster. Bumping one of these is the most common
source of a new failure mode — check this list before chasing a deeper
cause.

| Tool        | Version  | Notes                                                              |
| ----------- | -------- | ------------------------------------------------------------------ |
| `docker`    | `>=24.0` | Daemon must be reachable (`docker info` succeeds).                  |
| `kubectl`   | `>=1.31` | Client only (server version is whatever the kind node image ships). |
| `kind`      | `0.27.0` (pinned) | Binary URL is pinned in `VERSIONS.json`; do **not** use the system `kind` from a package manager. |
| `helm`      | `>=3.16` | Used by the chart cache + by the user handoff (Headlamp install). |
| `tofu`      | `>=1.6`  | OpenTofu (not Terraform). `tofu init` + `tofu validate` are the only tofu commands the bootstrap runs. |
| `openssl`   | `>=3.0`  | Used by the prereq probe (no PKI is generated anymore). |
| `uv`        | `>=0.5`  | Python project manager; not in `VERSIONS.json` (managed by mise/asdf). |
| Kind node image | `kindest/node:v1.31.0` | Pinned in `VERSIONS.json` under `kubernetes.kindest_node_image`. |
| Headlamp chart  | `0.43.0` from `https://kubernetes-sigs.github.io/headlamp/` | Cached at `infra/helm-charts/headlamp-0.43.0.tgz`. |

If you need a newer version of any of these, bump the relevant
field in `VERSIONS.json` (or `kubernetes.kindest_node_image` for
the node image) and re-run the bootstrap. The chart cache will
re-download the new version automatically.

## Common pitfalls (frozen — append, don't rewrite)

Each entry below is one observation from the iteration loop, written
once and never re-edited. Future readers can correlate the symptom,
edit the right file, and move on.

- `bootstrap] Docker daemon unreachable. Start it (e.g. sudo systemctl start docker) and re-run.` → the docker daemon isn't running. On Linux: `sudo systemctl start docker` (or `sudo dockerd` in a tty for a foreground view). On macOS: launch Docker Desktop. The prereq probe is `docker info --format '{{.ServerVersion}}'`; if that fails, nothing else will.
- `tofu: command not found` → OpenTofu isn't on `$PATH`. The prereq installer will try to install it via the package manager (apt/dnf/pacman/brew). If your OS isn't supported, install it manually from https://opentofu.org and re-run with `--skip-install`.
- `kind: command not found` even though `kind` is installed → the binary is in `/usr/local/bin/kind` but `$PATH` doesn't include it. The bootstrap adds it to the current shell's PATH during install, but a fresh terminal won't have it. Re-run with the new PATH or `export PATH=$PATH:/usr/local/bin`.
- `tofu init` fails with `Error: Failed to install provider` → no network, or the provider mirror is down. The provider is downloaded from `registry.opentofu.org` by default. If you're behind a proxy, set `HTTPS_PROXY` before re-running.
- `helm: failed to download chart "headlamp"` → the chart URL in `VERSIONS.json` (`https://kubernetes-sigs.github.io/headlamp/`) is unreachable. Bump `helm_repositories.headlamp.chart_version` if a new version is available, or check `helm repo add` works on a different network.
- `cicd-control-plane  | Error: failed to create cluster: API server listen port conflict` (or anything from kind complaining about ports) → something else is binding the host's port 6443 (or the random one kind picks). `docker ps` to find the offender, then `kind delete clusters` or `docker rm -f` to clear the port. **Then `tofu state rm kind_cluster.cicd`** (per AGENTS.md rule #3) so the next `tofu apply` doesn't think the cluster exists.
- `error: You must be logged in to the server (Unauthorized)` from any `kubectl` command → the kubeconfig at `infra/tofu/kubeconfig` was written by an older `tofu apply` and the kind node cert rotated. Re-run `tofu -chdir=infra/tofu apply -auto-approve` (idempotent) to rewrite it.
- `Headlamp pod stuck in CrashLoopBackOff` with `ImagePullBackOff` in Events → the headlamp container image pull rate-limited. The chart is installed by you (not the bootstrap), so re-run the helm upgrade with `--set image.pullPolicy=IfNotPresent` and it'll use the local image cache.
- `docker exec` returns "No such container: kind-cicd-..." → kind 0.27+ no longer prefixes container names with `kind-`. The actual names are `cicd-control-plane`, `cicd-worker`, `cicd-worker2`, `cicd-worker3`, `cicd-worker4`. Look at `docker ps` to see the real names.
- `Permission denied` writing to `infra/data/nodeN/` from the host as a non-root user → the bind-mount targets are created by kind as `root:root 755` on the first `tofu apply`. The bootstrap can't `chown` (root-only) and won't try. Two options: (1) `sudo chown -R $USER:users infra/data/` once after the first `tofu apply`, or (2) write through `sudo tee ... > /dev/null` for individual files. Option 1 is one-and-done and the recommended fix; option 2 is fine for ad-hoc smoke tests.
- **CRITICAL: the only way to create or delete the cluster is `tofu`.** No `kind create cluster --name=cicd`, no `docker rm -f cicd-*`, no `kubectl delete namespace` to "tidy up". See AGENTS.md § 4 rule #3. The tofu state file at `infra/tofu/terraform.tfstate` is the source of truth; if `tofu state list` is non-empty when `docker ps | grep ^cicd-` is empty, the cluster is in a weird state — fix with `tofu state rm <orphan>` (per resource), not hand-deletion of state.
- **CRITICAL: the bootstrap is preparation-only.** It runs `tofu init` and `tofu validate` but **never** `tofu plan` or `tofu apply`. If you find yourself wanting to add a `--apply` flag to the bootstrap, stop — see AGENTS.md § 4 rule #1.
- The `infra/data/*` hostPath mounts persist across `tofu destroy` → that's by design. Delete them manually (`rm -rf infra/data/node{1..4}/* infra/data/shared/*`) only when you want a clean slate.

## Rules of thumb (apply when adding Phase-1 pieces)

When you add a new prereq tool, a new hostPath mount, or a new
kind node shape, ask **who is the consumer?** — the developer
running the bootstrap, or a workload inside the cluster?

- Developer (laptop) → pin in `bootstrap/VERSIONS.json` under
  `tools.<tool>`. The prereq installer will pick it up.
- Cluster workload (CI job, Runner pod) → add to the kind node
  config in `infra/tofu/cluster.tf` (extraMounts, portMappings,
  nodeLabels). The bootstrap doesn't need to know.

When the bootstrap prints user handoff commands, every command
should be **copy-pasteable as-is**, including the `export
KUBECONFIG=…` prefix. The user shouldn't have to fill in a
path. The `--user` flag exists so a developer can re-read the
handoff at any time without re-running the prep.

## When the install is green

All smoke tests pass with no manual intervention. Commit the
changes (any new "Common pitfalls" entries, any new prereq tools,
any new tofu values). Future Phase-1 installs on a fresh host
should converge to one-shot.

## How to undo

```sh
# Drop the cluster. The bootstrap doesn't run this — per spec,
# infrastructure is run manually.
tofu -chdir=infra/tofu destroy -auto-approve

# Wipe the per-checkout bootstrap state (the seeded tfvars, the
# downloaded providers, the cached charts). Safe to re-run from
# scratch after this.
rm -rf infra/tofu/.terraform infra/tofu/.terraform.lock.hcl
rm -rf infra/tofu/tofu.tfvars
rm -rf infra/helm-charts/headlamp-*.tgz
```

`infra/data/*` is on the host so it survives cluster destroy.
Delete it manually if you want a clean slate. After undo, a
fresh `uv run blueprint-bootstrap --phase 1` re-preps
everything, followed by your `tofu apply`.
