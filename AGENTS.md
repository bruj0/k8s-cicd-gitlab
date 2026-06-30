# AGENTS.md — Blueprint working guide

This document is the entry point for any AI agent or human teammate
working in this `blueprint/` tree. It explains what the blueprint is,
how the components fit together, and the non-obvious rules you must
follow when modifying anything here.

If you only have time to read one file: read this one. The other docs
(`README.md`, `docs/phase-1.md`, `docs/prereqs.md`) cover specific
phases.

---

## 1. What this blueprint is

A reproducible, **fully local** GitLab + Kubernetes CI/CD stack built
on top of the `devops-take-home.md` assignment. It currently delivers
**Phase 1 (cluster)** and **Phase 2 (GitLab stack)**:

- **Phase 1**: 5-node `kind` cluster provisioned by OpenTofu, with
  per-node and shared hostPath mounts, a self-signed wildcard cert for
  `*.local.bruj0.net`, and a Headlamp dashboard chart pre-cached for
  the user to install.
- **Phase 2**: GitLab CE + Runner + OpenBao installed end-to-end into
  the Phase-1 cluster via `uv run blueprint-bootstrap --phase 2`.
  The GitLab chart sub-installs Envoy Gateway as its managed ingress
  controller, mints a self-signed wildcard cert for `*.local.bruj0.net`
  via its pre-install Job (cfssl), and injects the cert into the
  Gateway listener — no separate Traefik chart, no separate
  cert-manager, no custom GatewayClass chart. Secrets bootstrap goes
  through OpenBao via the `hvac` Python client (auto-port-forwards
  127.0.0.1:8200). Iteration happens via
  `.agents/skills/provision-gitlab/SKILL.md`.

Phase 3 (Helm-deployed app, CI pipeline, secret-injection via OpenBao)
is planned but not implemented.

### What "local" means here

Everything runs on the developer's machine. There is no cloud account,
no managed Kubernetes, no public DNS for `*.local.bruj0.net`. The host
resolves `*.local.bruj0.net` to `127.0.0.1` via a `/etc/hosts` entry the
user maintains themselves (see `docs/prereqs.md`).

---

## 2. Source-of-truth inputs

These files define the contract. Touch them last; everything else
derives from them.

| File | Purpose |
| --- | --- |
| [`spec.md`](../spec.md) | The original assignment brief. Defines tools, rules, phase structure. |
| [`devops-take-home.md`](../devops-take-home.md) | The rubric: minimum bar + bar. Phase 1 must clear the floor. |
| [`README.md`](README.md) | Human-facing phase table + quick start. |
| [`docs/phase-1.md`](docs/phase-1.md) | Phase 1 runbook with manual step-by-step. |
| [`docs/prereqs.md`](docs/prereqs.md) | Supported OSes, hardware floor, ports, DNS. |
| `infra/scripts/bootstrap/VERSIONS.json` | **Sole source of truth** for every pinned version (tools, helm chart, helm repo URLs). Read it before adding any new tool or chart. |
| `.agents/skills/provision-gitlab/SKILL.md` | The Phase-2 iteration loop (run, observe, fix, repeat). Drives how an AI agent should use `bootstrap.py --phase 2`. |
| `infra/scripts/bootstrap/phase2/references/*.yaml` | The Phase-2 install-time configuration (helm values + Gateway API manifests). Single source of truth for what Phase-2 ships; every installer reads from here. |

If anything else disagrees with these, **the table wins**.

---

## 3. Layout (as it exists on disk)

```
blueprint/
├── AGENTS.md                            # this file
├── README.md
├── .agents/
│   └── skills/
│       └── provision-gitlab/
│           └── SKILL.md                 # Phase-2 iteration loop
├── .gitignore                           # tls/private, tofu state, *.tfvars, .terraform
├── apps/                                # repositories to host in GitLab (Phase 3)
│   ├── guestbook/                       # demo: classic k8s guestbook (Go app + helm chart)
│   ├── redis/                           # demo: redis master + loose k8s YAMLs + helm chart
│   └── redis-slave/                     # demo: redis slave workload + helm chart
├── data/                                # hostPath mounts for kind nodes (spec dirs)
│   ├── node1/ node2/ node3/ node4/ node5/   # one per kind worker (empty)
│   └── shared/                          # bind-mounted on every kind node (empty)
├── docs/
│   ├── phase-1.md
│   └── prereqs.md
└── infra/
    ├── data/                            # ← actively used hostPath source (see §8)
    │   ├── node1..node4/                # bound to kind workers 1..4
    │   ├── node5/                       # present but unused (see §8)
    │   └── shared/                      # bound to every kind node
    ├── helm-charts/                     # locally cached charts (Headlamp, GitLab, Runner, OpenBao, ...)
    ├── scripts/
    │   ├── bootstrap.py                 # thin shim → delegates to bootstrap/ package
    │   └── bootstrap/                   # class-based package (SOLID), packaged via pyproject.toml
    │   │       ├── __init__.py
    │   │       ├── __main__.py          # `python3 -m bootstrap`
    │   │       ├── cli.py               # click wrapper: `blueprint-bootstrap` entry point
    │   │       ├── secrets_cli.py       # click wrapper: `blueprint-secrets` entry point (post-install helper)
    │   │       ├── app.py               # composition root (BootstrapApp)
    │   │       ├── paths.py             # resolved filesystem paths (Paths dataclass)
    │   │       ├── logger.py            # Logger protocol + ConsoleLogger / NullLogger
    │   │       ├── shell.py             # CommandRunner protocol + SubprocessRunner / DryRunRunner
    │   │       ├── versions.py          # VERSIONS dict, load_versions(), tool_pin(), helm_repo()
    │   │       ├── VERSIONS.json        # pinned versions (read by versions.py)
    │   │       ├── os_detect.py         # OSFamily detection (arch/debian/rhel/darwin/other)
    │   │       ├── installer.py         # Installer Strategy (ArchInstaller / DebianInstaller / RhelInstaller / DarwinInstaller)
    │   │       ├── prereq.py            # PrereqTool ABC + Docker/Kubectl/Kind/Helm/Tofu + PrereqRegistry
    │   │       ├── tofu.py              # TofuRunner (init / validate / next_steps; NO apply)
    │   │       ├── helm_cache.py        # HelmChartCache (downloads <name>-<ver>.tgz to infra/helm-charts/)
    │   │       ├── app_installer.py     # HelmAppInstaller generic + HeadlampInstaller subclass + installer_for() factory
    │   │       └── phase2/              # Phase 2: install GitLab + Runner + OpenBao (chart manages Envoy + cert)
    │   │           ├── __init__.py      # re-exports Phase2Pipeline + installers
    │   │           ├── pipeline.py      # 5-step orchestrator (called from app.py:BootstrapApp)
    │   │           ├── catalog.py       # Phase2Installers dataclass (bundle of every installer)
    │   │           ├── gateway.py       # GatewayCRDsInstaller — upstream standard CRDs + chart-shipped Envoy CRDs
    │   │           ├── openbao.py       # OpenBaoInstaller — chart install + init/unseal
    │   │           ├── secrets.py       # OpenBaoClient — hvac-backed client + auto port-forward
    │   │           ├── gitlab.py        # GitlabInstaller — chart + set-root-password + capture runner token
    │   │           ├── runner.py        # GitLabRunnerInstaller — registers against in-cluster Service DNS
    │   │           └── references/      # install-time YAML (committed, see spec rule "templates must have their own files")
    │   │               ├── helm-values-openbao.yaml
    │   │               ├── helm-values-gitlab.yaml
    │   │               ├── helm-values-runner.yaml
    │   │               └── gateway-api-crds/   # 3 vendored CRDs (1 standard + 2 Envoy chart-shipped)
    ├── secrets/                         # gitignored: OpenBao init JSON (mode 0700, see phase2/openbao.py)
    ├── tofu/                            # OpenTofu configuration
    │   ├── providers.tf                 # kind ~> 0.11, helm ~> 3.0, local ~> 2.5, null ~> 3.2
    │   ├── variables.tf                 # cluster_name, kubernetes_version, node_shapes, kubeconfig_path, data_root, domain
    │   ├── locals.tf                    # resolves absolute paths + node shapes into kind-style node specs
    │   ├── cluster.tf                   # kind_cluster.cicd + kubeconfig rewrite + smoke test
    │   ├── outputs.tf                   # kubeconfig_path, ca_*, wildcard_*, phase_ready
    │   ├── tofu.tfvars.example          # committed; copy to tofu.tfvars for local overrides
    │   ├── tofu.tfvars                  # gitignored, real local overrides
    │   └── .terraform/, .terraform.lock.hcl  # generated
    └── tls/                             # generated PKI (gitignored)
        ├── private/                     # ca.crt, ca.key, _.<domain>.{crt,key,csr,cnf}
        └── public/                      # ca.crt (no keys)
```

---

## 4. The hard rules (from `spec.md`)

These rules are **non-negotiable**. If a change you want to make would
violate any of them, stop and ask.

1. **The bootstrap application never runs OpenTofu. It is run manually.**
   OpenTofu provisions *infrastructure* (the kind cluster itself), and
   that is always a manual step. In code terms:
   - `TofuRunner` exposes `init()`, `validate()`, `next_steps()`.
     It does **not** expose `apply()`.
   - `BootstrapApp.run()` stops after `tofu validate` and prints the
     commands the user runs.
   - If you add a code path that calls `tofu apply`, reject it.

2. **Phase 1 is preparation-only. Phase 2+ may install applications.**
   - **Phase 1 (cluster)**: the bootstrap is a *preparation* tool. It
     must not silently create infrastructure on the user's behalf. The
     `HelmAppInstaller` for Phase 1 (Headlamp) prints the `helm install`
     command for the user to run; it does not execute it.
   - **Phase 2+ (application stack)**: the bootstrap **may** call
     `helm install`, `kubectl apply`, init/unseal OpenBao, register the
     Runner, etc. directly. These are *applications*, not infrastructure,
     and the spec is fine with the bootstrap driving them end-to-end so
     iteration is fast.
   - The dividing line is **infrastructure vs. applications**. Anything
     that creates/alters the cluster itself (kind nodes, network, host
     mounts) stays manual. Anything that runs *on top of* an existing
     cluster (GitLab, Runner, OpenBao, app charts) may be driven by
     the bootstrap in Phase 2+. The chart handles Envoy + cert for
     us; we don't manage either directly.

### Other rules (less strict but still apply)

- **Shell scripts only if simple; otherwise Python following SOLID.**
  The bootstrap package is the SOLI D model. Don't create a new monolith
  — add a new small class to the package and wire it in `app.py`.
- **All versions pinned in `bootstrap/VERSIONS.json`.** No class
  hardcodes a version string. Bump versions in JSON, never in code.
- **Helm charts stored locally at `infra/helm-charts/`.** The bootstrap
  downloads them there; consumers install from that path.
- **Secrets stored in OpenBao, never in plain.** Phase 2 stores
  GitLab's initial root password and the Runner registration token
  in `infra/secrets/openbao-init.json` (gitignored, mode 0600) and
  the same values are pushed into OpenBao at
  `secret/gitlab/initial_root_password` and
  `secret/gitlab/runner/registration_token`. The bootstrap reads
  them back via the `hvac` Python client (`OpenBaoClient`,
  `bootstrap-secrets` CLI), which auto-port-forwards
  `127.0.0.1:8200` to the `openbao` Service on first use.
- **No hardcoded templates inside Python scripts.** All templates
  belong in their own files (a Jinja template, a yaml, etc.). The
  old Phase 1 PKI step generated an openssl cnf inline in
  `bootstrap/pki.py`; that file was removed when we switched to
  the GitLab chart's cfssl-based cert mint. Keep an eye out for
  new ad-hoc templating creeping into the bootstrap — extract
  to a file under `references/` instead.
- **Use `uv` for the Python toolchain.** The bootstrap is a uv
  project (`pyproject.toml` + `uv.lock` committed, `.venv/`
  gitignored). New entry points go under `[project.scripts]`;
  `uv sync` picks them up. The two installed entry points are
  `blueprint-bootstrap` (the install CLI) and `blueprint-secrets`
  (the post-install helper for reading OpenBao secrets + opening
  the UI). Don't reintroduce system-level `pip install` — the
  virtualenv is per-checkout and rebuildable from the lockfile.
- **Skill frontmatter must be single-line.** Any `.agents/skills/*/SKILL.md`
  `description:` field is **one quoted string**, not a folded multi-line
  scalar. Reason: skill loaders use YAML's compact-mapping parser; an
  indented continuation like `  Traefik (with Gateway API): ...` gets
  re-interpreted as a nested mapping and the whole frontmatter fails
  to parse. Quoting with `'…'` is mandatory when the description
  contains colons or runs over 80 chars.
- **`*.local.bruj0.net` is for humans, not for cluster traffic.**
  Any hostname like `gitlab.local.bruj0.net` exists in DNS only on
  the **developer's host** (via `/etc/hosts` + the local CA) so an
  end-user can open it in a browser. Cluster-resident workloads
  (the GitLab Runner pod, CI jobs, internal Travis callers, anything
  pod-side) MUST use the **in-cluster Service DNS** instead:
  `gitlab-webservice-default.gitlab.svc:8181`,
  `openbao.openbao.svc:8200`, etc. The reason: there is no CoreDNS
  rewrite for `*.local.bruj0.net` inside the cluster, so pods
  resolve those hostnames to `127.0.0.1` and break. This applies
  to every `gitlabUrl`/`apiUrl`/chart value that targets an
  external hostname.
- **Everything must be stored in `blueprint/`.** No stray files at the
  repo root or in `~`. The `apps/`, `infra/`, `data/`, `helm-charts/`
  layout is mandatory.

---

## 5. How the pieces fit together — Phase 1 data flow

```mermaid
flowchart TD
    U[User runs uv run blueprint-bootstrap]
    U --> A[BootstrapApp.run]

    subgraph Composition[Composition root - app.py]
        A --> P[PrereqTool Registry<br/>check + install missing prereqs]
        A --> T[TofuRunner<br/>init / validate ONLY - NO apply]
        A --> H[HelmChartCache<br/>pull tgz to infra/helm-charts/]
        A --> L[HelmAppInstaller<br/>generic + Headlamp subclass<br/>cache + print helm cmd]
    end

    P --> O[/usr/bin docker kubectl kind helm tofu/]
    T --> TF[(infra/tofu/.terraform/<br/>providers downloaded)]
    H --> HC[(infra/helm-charts/<br/>headlamp-0.43.0.tgz)]
    L --> STDOUT[stdout only<br/>no side effects]

    T --> N[Bootstrap prints next commands]
    N -->|tofu plan| USER2[User runs printed commands]
    USER2 -->|tofu apply| KIND[kind_cluster.cicd applies]
    KIND --> NODES[(5 kind nodes Ready)]

    classDef user fill:#1e3a8a,stroke:#3b82f6,color:#fff;
    classDef runner fill:#7c2d12,stroke:#f97316,color:#fff;
    classDef storage fill:#14532d,stroke:#22c55e,color:#fff;
    class U,USER2 user;
    class A,N,KIND runner;
    class O,TLS,TF,HC,STDOUT,NODES storage;
```

The user runs `uv run blueprint-bootstrap --phase 1` exactly
**once** (after a one-time `uv sync` to install the bootstrap's
deps). Then they execute the printed commands. Bootstrap is never
invoked again (re-running is a safe no-op for idempotency, but the
real work happens from the printed commands onward).

---

## 6. How to modify the blueprint

### Adding a new prereq tool

1. Edit `bootstrap/VERSIONS.json` — add a `<tool>` entry under `tools`
   with the same `package_by_family` map shape as the existing tools.
2. Edit `bootstrap/prereq.py`:
   - Add a `class <Tool>(PrereqTool)` with `name`, `candidates`, `pin_key`.
   - Register it in `PrereqRegistry.default()`'s `tools` list.
3. If the tool needs a non-standard `--version` flag, add it to the
   `_PROBES` dict in `prereq.py`.
4. Bump the package name in the appropriate OS family in
   `VERSIONS.json` (don't hardcode it elsewhere).

### Adding a new helm chart to be cached

1. Edit `bootstrap/VERSIONS.json` — add an entry under
   `helm_repositories` with `name`, `url`, `chart`, `chart_version`,
   and optional `values_overrides`.
2. Decide which file the installer lives in:
   - **Phase 1 installers (one-line, no post-install steps)** live in
     `bootstrap/app_installer.py`. The example in the old version of
     this section (`GitlabInstaller` reading a K8s Secret) was the
     pre-Phase-2 shape. Phase 1 installers inherit the base
     `HelmAppInstaller.install()` and only override `user_handoff_steps()`.
   - **Phase 2+ installers (need init, post-install secret capture,
     registration-token fetch, etc.)** live in
     `bootstrap/phase2/<component>.py` and get their own `install()`
     override. The composition root in `app.py:BootstrapApp.__init__`
     builds these directly via the relevant class, passing in any
     collaborators (e.g. `OpenBaoClient` for GitLab / Runner).
   - In both cases, add a branch to `installer_for()` in
     `app_installer.py` so `repo_key` resolves to the right class.
     Phase 2 also updates the `Phase2Installers` dataclass in
     `bootstrap/phase2/catalog.py`.

Example for a new Phase-2-style installer (Vault with auto-unseal):

```python
# in bootstrap/phase2/vault.py
class VaultInstaller(HelmAppInstaller):
    REPO_KEY = "vault"

    def __init__(self, runner, paths, cache, log, openbao):
        super().__init__(runner, paths, cache, log,
                         HelmAppSpec(repo_key=self.REPO_KEY,
                                     release="vault",
                                     namespace="vault",
                                     values_files=(str(paths.phase2_refs_dir
                                                       / "helm-values-vault.yaml"),)))
        self._openbao = openbao

    def install(self):
        result = super().install()
        # post-install: enable kubernetes auth method via bao CLI
        # (delegated to OpenBaoClient, etc.)
        return result

# in bootstrap/phase2/catalog.py:
@dataclass(frozen=True)
class Phase2Installers:
    gateway: GatewayCRDsInstaller
    openbao: OpenBaoInstaller
    gitlab: GitlabInstaller
    runner: GitLabRunnerInstaller
    vault: VaultInstaller                  # ← add

# in app.py:BootstrapApp.__init__:
self._phase2_installers = Phase2Installers(
    gateway=GatewayCRDsInstaller(...),
    openbao=OpenBaoInstaller(...),
    gitlab=GitlabInstaller(..., self._phase2_openbao_client),
    runner=GitLabRunnerInstaller(..., self._phase2_openbao_client),
    vault=VaultInstaller(..., self._phase2_openbao_client),
)
```

Wire the new step into `bootstrap/phase2/pipeline.py:_step_*` and
add a matching YAML reference under `references/`.

### User-facing access after Phase 2 completes

After `uv run blueprint-bootstrap --phase 2` finishes, the user
needs three things from the host to actually use the stack: (1)
TLS trust, (2) `/etc/hosts` entries for the wildcard domain, and
(3) a way to read OpenBao secrets. None of these are automated
because they all live outside the cluster.

1. **Trust the local CA** (the chart mints a self-signed wildcard
   for `*.local.bruj0.net` via its pre-install cfssl job; export
   the CA and install it in the host trust store):
   ```sh
   kubectl -n gitlab get secret gitlab-wildcard-tls-ca \
     -o jsonpath='{.data.cfssl_ca}' | base64 -d > infra/tls/public/ca.crt
   sudo trust anchor infra/tls/public/ca.crt
   ```

2. **Map `*.local.bruj0.net` to 127.0.0.1** (so the browser
   reaches the kind node port forwards). Add the entries once:
   ```sh
   echo "127.0.0.1 gitlab.local.bruj0.net registry.local.bruj0.net \
                kas.local.bruj0.net minio.local.bruj0.net" | sudo tee -a /etc/hosts
   ```

3. **Read OpenBao secrets** via the `blueprint-secrets` CLI
   (auto-port-forwards 127.0.0.1:8200, so no `kubectl port-forward`
   is needed):
   ```sh
   uv run blueprint-secrets read gitlab initial_root_password   # GitLab root pw
   uv run blueprint-secrets ui                                  # OpenBao UI
   ```

   The same access works from any Python session via `hvac.Client`
   — see `OpenBaoClient` in `bootstrap/phase2/secrets.py` for the
   pattern.

#### URLs the user can reach (all on `127.0.0.1`)

| URL                            | Login              | What it is                                      |
| ------------------------------ | ------------------ | ----------------------------------------------- |
| `https://gitlab.local.bruj0.net`     | `root` / OpenBao secret | GitLab UI (web, API, registry, KAS)        |
| `https://registry.local.bruj0.net`    | `root` / OpenBao secret | GitLab Container Registry                   |
| `https://kas.local.bruj0.net`        | `root` / OpenBao secret | GitLab Agent Server (KAS)                   |
| `https://minio.local.bruj0.net`      | `root` / OpenBao secret | MinIO (GitLab object storage, LFS, artifacts) |
| `https://openbao.local.bruj0.net`    | root token in `infra/secrets/openbao-init.json` | OpenBao UI (via Envoy Gateway)     |

The Envoy Gateway sub-chart is what makes all of the above
reachable through the `*.local.bruj0.net` hostnames. The
GitLab chart's own pre-install Job mints the wildcard cert
and injects it into the gateway listener — no Traefik, no
cert-manager, no custom GatewayClass chart.

If the user would rather hit the kind node IPs directly, the
`registry`/`kas`/`minio` Services are reachable on the
NodePort range that `kind` exposes (look at
`kubectl -n gitlab get svc -o wide`); the `gitlab.local.bruj0.net`
hostname is what makes TLS work without per-host
`--resolve` overrides.