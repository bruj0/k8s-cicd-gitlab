# Phase 1 — Provision the local kind cluster

This phase provisions a 5-node `kind` cluster via OpenTofu. It does **not**
install GitLab, OpenBao, Envoy, or any application — those land in
Phase 2. Keeping the phases scoped this way lets you verify Phase 1 in
isolation before moving on.

## What you get

- 1 control-plane node (`control-plane-1`, 4 GB)
- 3 worker nodes for GitLab services (`gitlab-1..3`, 4 GB each)
- 1 worker node for the GitLab Runner (`runner`, 8 GB)
- Per-node `extraMounts` from `blueprint/data/nodeN` → `/var/local/nodeN`
- Shared `extraMounts` from `blueprint/data/shared` → `/var/local/shared`
  on every node (incl. control-plane)
- Host ports `80` and `443` on the control-plane reserved for the
  Phase 2 Envoy Gateway entrypoint (forwarded to the cluster by kind).
- **TLS is NOT part of Phase 1** anymore — the GitLab chart's pre-install
  cfssl Job mints the `*.local.bruj0.net` wildcard during Phase 2.
  Phase 1 only creates the kind cluster.

## One-shot preparation

```sh
cd blueprint

# 0. One-time: install the bootstrap's Python deps into .venv/.
uv sync

# 1. Run the full preparation (installs prereqs, writes `tofu.tfvars`,
#    downloads OpenTofu providers, caches the Headlamp chart). Bootstrap
#    prints the next commands YOU run.
uv run blueprint-bootstrap --phase 1
```

The bootstrap **provisions configuration** (prereqs, `tofu.tfvars`,
downloaded providers, Headlamp chart cache) and **prints the exact
next commands YOU run** to actually apply the cluster. Per spec rule,
bootstrap never invokes `tofu apply` itself.

Re-running `uv run blueprint-bootstrap --phase 1` is idempotent — it
skips steps that already succeeded (e.g. the Headlamp chart isn't
re-downloaded if it's already on disk, `tofu init` is a no-op when
providers are present).

## Manual step-by-step

```sh
cd blueprint

# 1. Check prereqs without installing or provisioning anything
uv run blueprint-bootstrap --phase 1 --check

# 2. Run the full preparation (installs prereqs, writes tfvars,
#    downloads OpenTofu providers, caches the Headlamp chart).
#    Bootstrap prints the next commands YOU run.
uv run blueprint-bootstrap --phase 1

# 3. YOU inspect the plan, then YOU apply
tofu -chdir=infra/tofu plan
tofu -chdir=infra/tofu apply -auto-approve

# 4. YOU install Headlamp into the cluster
KUBECONFIG=$PWD/infra/tofu/kubeconfig \
  helm upgrade --install headlamp \
    $PWD/infra/helm-charts/headlamp-0.43.0.tgz \
    --namespace headlamp --create-namespace --wait --set service.type=NodePort
```

(Bootstrap prints the exact commands at the end of step 2 — copy them
verbatim.)

## Sanity checks

After `tofu apply` succeeds:

```sh
# 5 kind containers (1 control-plane + 4 workers)
docker ps --format '{{.Names}}' | grep ^kind-

# All nodes Ready, role labels visible
KUBECONFIG=$PWD/tofu/kubeconfig kubectl get nodes -o wide

# 3 gitlab-labelled nodes
KUBECONFIG=$PWD/tofu/kubeconfig kubectl get nodes -l node.kubernetes.io/role=gitlab

# Per-node hostPath mounts are reachable from inside each container
docker exec kind-cicd-control-plane  ls /var/local/shared
for n in node1 node2 node3 node4; do
  docker exec "kind-cicd-worker-$n" ls "/var/local/$n" || true
done
```

Expected outputs:

- 5 containers named `kind-cicd-control-plane`, `kind-cicd-worker`,
  `kind-cicd-worker2`, `kind-cicd-worker3`, `kind-cicd-worker4`
  (kind appends a numeric suffix only when duplicates exist).
- `kubectl get nodes` shows 5 rows, all `STATUS = Ready`.

(Note: the Phase 1 cert mint is gone. The `*.local.bruj0.net`
wildcard is created during Phase 2 by the GitLab chart's pre-install
cfssl Job — see `.agents/skills/provision-gitlab/SKILL.md`.)

## Tearing it down

```sh
tofu -chdir=blueprint/infra/tofu destroy -auto-approve
```

`data/*` is on the host so it survives cluster destroy. Delete it
manually if you want a clean slate:

```sh
rm -rf blueprint/data/node{1,2,3,4,5}/* blueprint/data/shared/*
```

## Trade-offs

- **Self-signed CA instead of Let's Encrypt.** Public LE cannot validate
  `*.local.bruj0.net` because the host has no public DNS. The
  GitLab chart's pre-install cfssl Job mints the wildcard during
  Phase 2; we swap to cert-manager with DNS-01 once the user
  delegates `local.bruj0.net` to a public resolver.
- **Side-by-side kubeconfig.** We write a kubeconfig next to the tofu
  state (`infra/tofu/kubeconfig`) instead of mutating
  `~/.kube/config`. Use `KUBECONFIG=... kubectl ...` or `--kubeconfig=...`.
  Easier to undo, easier to multi-cluster.
- **kind RAM/CPU are advisory.** The kind node config doesn't actually
  enforce resource limits inside the container. `node_shapes` is
  documentation + node labels, not cgroups. If a node really does OOM the
  host will show it.
- **Phase 1 does not bootstrap a `kubectl` context.** We don't want to
  silently modify the host's global config. `export KUBECONFIG=...` is
  one line; that's the deal.
- **state is local.** `terraform.tfstate` lives in
  `infra/tofu/terraform.tfstate` (gitignored). For a real CI pipeline
  you'd back this with an S3/GCS backend; for Phase 1 the reviewer can
  tear down and re-create on a laptop, which is the assignment's actual
  bar.