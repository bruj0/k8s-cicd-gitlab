# Blueprint — local GitLab + Kubernetes CI/CD

This blueprint implements the end-to-end local DevOps workflow described in
`../spec.md` (and, by extension, `../devops-take-home.md`). It uses
OpenTofu, kind, the GitLab Helm chart, OpenBao, and Traefik with Gateway
API support. All artifacts live under `blueprint/`.

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
└── infra/            # OpenTofu + provisioning scripts
    ├── helm-charts/  # Locally-cached helm charts (Headlamp, ...)
    ├── scripts/      # Python helpers
    │   ├── pki.py            # Local CA + wildcard cert (openssl wrapper)
    │   └── bootstrap/        # Class-based prep pipeline (SOLID)
    │       ├── VERSIONS.json # Pinned versions, single source of truth
    │       └── app.py        # Composition root
    ├── tofu/         # OpenTofu configuration (kind cluster)
    └── tls/          # Generated local CA + wildcard cert (gitignored)
```

## Phases

| Phase | Status   | What it delivers                                                            |
| ----- | -------- | --------------------------------------------------------------------------- |
| 1     | ✅ done  | Local 5-node kind cluster provisioned by OpenTofu, per-node + shared mounts |
| 2     | ✅ done  | GitLab (minimal) + Runner + OpenBao + Traefik w/ Gateway API, reachable at `https://gitlab.local.bruj0.net` |
| 3     | pending  | Helm-deployed app, CI pipeline, secret-injection via OpenBao               |

See [docs/phase-1.md](docs/phase-1.md) for the Phase 1 runbook.

## Quick start (Phase 1)

Bootstrap **provisions** the working tree (prereqs, PKI, OpenTofu
providers, Headlamp chart cache) and **prints** the next commands.
It never runs `tofu apply` itself — per spec, OpenTofu is run by a
person.

```sh
cd blueprint

# Step 0 — Run the prep pipeline (idempotent; safe to re-run).
python3 infra/scripts/bootstrap.py --phase 1
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
python3 infra/scripts/bootstrap.py --user
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
python3 infra/scripts/bootstrap.py --phase 2 --check

# Install everything (Traefik, OpenBao, GitLab, Runner, Gateway + HTTPRoutes).
python3 infra/scripts/bootstrap.py --phase 2

# Trust the local CA so curl / your browser accept the self-signed cert.
sudo trust anchor infra/tls/public/ca.crt

# Open GitLab: https://gitlab.local.bruj0.net  (login: root)
# Open OpenBao: https://openbao.local.bruj0.net  (initial root token in
#   infra/secrets/openbao-init.json, gitignored, chmod 600)
```

Phase 2 runs the 7-step pipeline end-to-end. Every step is idempotent —
**re-runs are safe**. If a step fails, fix the corresponding installer
or YAML reference under `infra/scripts/bootstrap/phase2/` and re-run.
The iteration loop is documented at
[`.agents/skills/provision-gitlab/SKILL.md`](.agents/skills/provision-gitlab/SKILL.md).

## Conventions

- **Bootstrap prepares, never applies.** Per spec, the bootstrap
  application checks the system and provisions all the configuration
  so a person can run `tofu apply` themselves. There is no `--apply`
  flag on the bootstrap; the user runs OpenTofu manually.
- **No shell scripts for non-trivial logic** — the spec rules shell out for
  Python. `infra/scripts/bootstrap/` is a class-based package (SOLID)
  composed of single-responsibility classes wired together by
  `app.py`. `infra/scripts/pki.py` is the only remaining monolith (it
  predates the package and only runs `openssl`).
- **Pinned versions live in one place.** `infra/scripts/bootstrap/VERSIONS.json`
  is the single source of truth for every tool version, helm chart
  version, and helm repository URL. No class hardcodes a version.
- **Helm charts are cached locally.** Charts are downloaded into
  `infra/helm-charts/` and installed from that path so installs work
  without re-fetching.
- **No plaintext secrets in git.** Phase 1's CA private key is the only
  artifact that qualifies, and it lives under `infra/tls/private/`
  (gitignored). Phase 2 moves all secrets into OpenBao.
- **TLS is local-CA now, Let's Encrypt later.** Public LE can't validate
  `*.local.bruj0.net`. Phase 2 swaps the issuer once public DNS is
  delegated. The wrapper (Traefik, IngressRoute, etc.) never changes.
- **Reproducibility first.** Every step is idempotent. Re-running
  `bootstrap.py --phase 1` is a no-op when nothing has changed.