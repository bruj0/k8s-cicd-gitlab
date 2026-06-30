# Blueprint — local GitLab + Kubernetes CI/CD

This blueprint implements the end-to-end local DevOps workflow described in
`../spec.md` (and, by extension, `../devops-take-home.md`). It uses
OpenTofu, kind, the GitLab Helm chart, OpenBao, and the GitLab
chart's managed Envoy Gateway sub-chart. All artifacts live under
`blueprint/`.

## Layout

```
blueprint/
├── apps/             # Application repositories (one per workload)
│   ├── guestbook/    # Demo: classic k8s guestbook
│   ├── redis/        # Demo: redis master + slave
│   └── redis-slave/  # Demo: redis slave workload
├── data/             # Persistent storage that survives cluster destroy
│   ├── node1..5/     # One dir per kind node (extraMount hostPath)
│   └── shared/       # Shared extraMount on every node
├── docs/             # Per-phase runbooks + prereqs
│   ├── prereqs.md
│   └── phase-1.md
├── pyproject.toml    # uv project: installs blueprint-bootstrap + blueprint-secrets
├── uv.lock           # committed for reproducibility
└── infra/            # OpenTofu + provisioning scripts
    ├── helm-charts/  # Locally-cached helm charts (GitLab, Runner, OpenBao, Headlamp, ...)
    ├── scripts/      # Python helpers
    │   ├── bootstrap.py  # thin shim → delegates to bootstrap/ package
    │   └── bootstrap/    # class-based pipeline (SOLID), packaged via pyproject.toml
    │       ├── VERSIONS.json  # Pinned versions, single source of truth
    │       ├── cli.py          # click wrapper: blueprint-bootstrap entry point
    │       ├── secrets_cli.py  # click wrapper: blueprint-secrets (post-install helper)
    │       ├── app.py          # composition root
    │       └── phase2/         # Phase 2 installers (GitLab, Runner, OpenBao)
    ├── tofu/         # OpenTofu configuration (kind cluster)
    └── tls/          # Generated local CA + wildcard cert (gitignored)
```

## Phases

| Phase | Status   | What it delivers                                                            |
| ----- | -------- | --------------------------------------------------------------------------- |
| 1     | ✅ done  | Local 5-node kind cluster provisioned by OpenTofu, per-node + shared mounts |
| 2     | ✅ done  | GitLab (minimal) + Runner + OpenBao + chart-managed Envoy Gateway, reachable at `https://gitlab.local.bruj0.net` |
| 3     | pending  | Helm-deployed app, CI pipeline, secret-injection via OpenBao               |

See [docs/phase-1.md](docs/phase-1.md) for the Phase 1 runbook.

## Quick start (Phase 1)

Bootstrap **provisions** the working tree (prereqs, OpenTofu
providers, Headlamp chart cache) and **prints** the next commands.
It never runs `tofu apply` itself — per spec, OpenTofu is run by a
person.

```sh
cd blueprint

# Step 0a — One-time: install the bootstrap's Python deps into
# .venv/ (committed uv.lock means this is reproducible).
uv sync

# Step 0b — Run the prep pipeline (idempotent; safe to re-run).
uv run blueprint-bootstrap --phase 1
```

After Step 0, bootstrap is done and exits. The next six commands are
the ones **you** run. Each is idempotent.

```sh
# Step 1 — Inspect the plan. Read it carefully before applying.
tofu -chdir=infra/tofu plan

# Step 2 — Apply. This is what creates the 5-node kind cluster.
tofu -chdir=infra/tofu apply -auto-approve

# Step 3 — Verify the cluster is up (you should see 5 nodes Ready).
export KUBECONFIG=$PWD/infra/tofu/kubeconfig
kubectl get nodes -o wide

# Step 4 — Install the Headlamp dashboard (one-time, after the cluster is up).
helm upgrade --install headlamp \
  "$PWD/infra/helm-charts/headlamp-0.43.0.tgz" \
  --namespace headlamp --create-namespace --wait \
  --set service.type=NodePort

# Step 5 — Discover the Headlamp URL. The script prints http://NODE_IP:NODE_PORT.
NODE_PORT=$(kubectl get --namespace headlamp -o jsonpath="{.spec.ports[0].nodePort}" services headlamp)
NODE_IP=$(kubectl   get nodes     --namespace headlamp -o jsonpath="{.items[0].status.addresses[0].address}")
echo "http://$NODE_IP:$NODE_PORT"

# Step 6 — Mint a Headlamp login token. Paste it into the dashboard's
# "Use a token" login form.
kubectl create token headlamp --namespace headlamp
```

### Cheat sheet

If you only want the user-facing commands (no prep, no prereqs check),
print them anytime with:

```sh
uv run blueprint-bootstrap --user
```

### Other modes

| Flag            | Behaviour                                                 |
| --------------- | --------------------------------------------------------- |
| `--check`       | Print prereq status only. Do not install or provision.    |
| `--skip-install`| Run the full prep but assume prereqs are already present. |
| `--dry-run`     | Log every command without executing it.                   |
| `--user`        | Only print the user-handoff commands (Steps 1–6 above).   |

See [docs/prereqs.md](docs/prereqs.md) for host requirements and
[docs/phase-1.md](docs/phase-1.md) for the detailed Phase 1 runbook.

## Quick start (Phase 2)

After Phase 1 has you at `kubectl get nodes` showing 5 nodes Ready,
install the GitLab stack on top:

```sh
cd blueprint
export KUBECONFIG=$PWD/infra/tofu/kubeconfig

# Optional: pre-flight check (no installs, just verifies cluster + helm).
uv run blueprint-bootstrap --phase 2 --check

# Install everything (Gateway API CRDs, OpenBao, GitLab + Envoy, Runner).
uv run blueprint-bootstrap --phase 2
```

Phase 2 runs the 5-step pipeline end-to-end. Every step is idempotent —
**re-runs are safe**. If a step fails, fix the corresponding installer
or YAML reference under `infra/scripts/bootstrap/phase2/` and re-run.
The iteration loop (with a frozen list of known pitfalls) is documented
at
[`.agents/skills/provision-gitlab/SKILL.md`](.agents/skills/provision-gitlab/SKILL.md).

### What Phase 2 installs (and why)

The chart does the heavy lifting — the bootstrap just drives it:

  - **Gateway API CRDs** (upstream standard + 2 chart-shipped Envoy
    CRDs the GitLab chart needs) — installed by
    `phase2/gateway.py:GatewayCRDsInstaller` from
    `https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml`
    + `infra/scripts/bootstrap/phase2/references/gateway-api-crds/`.
  - **OpenBao** (KV v2 secret store) — chart installed by
    `phase2/openbao.py`, then `init` + `unseal` once. Init JSON
    persisted to `infra/secrets/openbao-init.json` (gitignored,
    `chmod 600`).
  - **GitLab CE** — chart installed by `phase2/gitlab.py`. The
    chart sub-installs **Envoy Gateway 1.7.1** as its
    `gateway-helm` sub-chart and mints a **self-signed wildcard
    cert** for `*.local.bruj0.net` via a pre-install cfssl Job.
    The bootstrap then sets the root password via `gitlab-rails
    runner` and captures the Runner registration token into
    OpenBao at `secret/gitlab/runner/registration_token`.
  - **GitLab Runner** — chart installed by `phase2/runner.py`,
    using the registration token from OpenBao. Registers
    against the in-cluster Service DNS
    (`gitlab-webservice-default.gitlab.svc:8181`), not the
    `*.local.bruj0.net` hostname (which doesn't resolve inside
    the cluster — see AGENTS.md rule).

### What you (the user) need to do post-install

Three one-time host-side steps, none of which the bootstrap can
do for you (they all live outside the cluster):

1. **Trust the local CA** (the chart's cfssl Job minted the
   wildcard for `*.local.bruj0.net`; export the CA and install
   it in the host trust store):

   ```sh
   kubectl -n gitlab get secret gitlab-wildcard-tls-ca \
     -o jsonpath='{.data.cfssl_ca}' | base64 -d > infra/tls/public/ca.crt
   sudo trust anchor infra/tls/public/ca.crt
   ```

2. **Map the wildcard to 127.0.0.1** (so the browser reaches
   Envoy on the kind node):

   ```sh
   echo "127.0.0.1 gitlab.local.bruj0.net registry.local.bruj0.net \
                kas.local.bruj0.net minio.local.bruj0.net \
                openbao.local.bruj0.net" | sudo tee -a /etc/hosts
   ```

3. **Read OpenBao secrets** (the `blueprint-secrets` CLI
   auto-port-forwards 127.0.0.1:8200, so no `kubectl port-forward`
   is needed):

   ```sh
   uv run blueprint-secrets read gitlab initial_root_password   # GitLab root pw
   uv run blueprint-secrets ui                                  # OpenBao UI
   ```

### URLs you can reach after install

All of these are `https://<hostname>/`, served by the chart's
Envoy Gateway sub-chart on the kind cluster, terminated with the
self-signed wildcard cert the chart minted in step 4. Trust the
CA first (above).

| URL                                  | Login                                              | What it is                                |
| ------------------------------------ | -------------------------------------------------- | ----------------------------------------- |
| `https://gitlab.local.bruj0.net`           | `root` / OpenBao secret                            | GitLab web UI + API                       |
| `https://registry.local.bruj0.net`         | `root` / OpenBao secret                            | GitLab Container Registry                 |
| `https://kas.local.bruj0.net`              | `root` / OpenBao secret                            | GitLab Agent Server (KAS)                 |
| `https://minio.local.bruj0.net`            | `root` / OpenBao secret                            | MinIO (LFS, artifacts, packages)          |
| `https://openbao.local.bruj0.net`          | root token in `infra/secrets/openbao-init.json`    | OpenBao UI                                |

The hostnames resolve on the **developer's machine** via
`/etc/hosts` (one-time, see step 2 above). Inside the cluster,
pods use Service DNS (`gitlab-webservice-default.gitlab.svc:8181`,
`openbao.openbao.svc:8200`, etc.) — `*.local.bruj0.net` does
**not** resolve in-cluster. See the matching rule in `AGENTS.md`.

## Conventions

- **Bootstrap prepares, never applies.** Per spec, the bootstrap
  application checks the system and provisions all the configuration
  so a person can run `tofu apply` themselves. There is no `--apply`
  flag on the bootstrap; the user runs OpenTofu manually.
- **No shell scripts for non-trivial logic** — the spec rules shell out for
  Python. `infra/scripts/bootstrap/` is a class-based package (SOLID)
  composed of single-responsibility classes wired together by
  `app.py`.
- **uv is the Python toolchain.** The bootstrap is a uv project
  (`pyproject.toml` + `uv.lock` committed, `.venv/` gitignored).
  The two installed entry points are `blueprint-bootstrap`
  (install CLI) and `blueprint-secrets` (post-install helper for
  reading OpenBao secrets + opening the UI). Don't reintroduce
  system-level `pip install` — the virtualenv is per-checkout and
  rebuildable from the lockfile.
- **Pinned versions live in one place.** `infra/scripts/bootstrap/VERSIONS.json`
  is the single source of truth for every tool version, helm chart
  version, and helm repository URL. No class hardcodes a version.
- **Helm charts are cached locally.** Charts are downloaded into
  `infra/helm-charts/` and installed from that path so installs work
  without re-fetching.
- **No plaintext secrets in git.** Phase 2 stores the OpenBao
  unseal keys + root token in `infra/secrets/openbao-init.json`
  (gitignored, mode 0600) and pushes the values that matter
  (GitLab initial root password, Runner registration token) into
  OpenBao at `secret/gitlab/...`. Read them back via
  `uv run blueprint-secrets read <path> <key>`.
- **TLS is local-CA now, Let's Encrypt later.** Public LE can't validate
  `*.local.bruj0.net`. The GitLab chart's pre-install cfssl Job
  mints the wildcard cert for us today; once public DNS is
  delegated, we swap to cert-manager-issued certs without
  changing anything about the bootstrap or the chart values.
- **Reproducibility first.** Every step is idempotent. Re-running
  `uv run blueprint-bootstrap --phase 1` is a no-op when nothing
  has changed.