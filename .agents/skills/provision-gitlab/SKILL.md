---
name: provision-gitlab
description: Install Phase 2 of the blueprint — GitLab, Runner, OpenBao, and
  Traefik (with Gateway API). Use when the user says "phase 2", "install
  gitlab", "provision gitlab", or asks to set up the GitLab/CI portion of
  the blueprint. Drives the iteration loop: run `bootstrap.py --phase 2`,
  observe, fix the installer or YAML references, re-run until smoke tests
  pass.
---

# Provision GitLab (Phase 2)

This skill installs the rest of the blueprint on top of a working
Phase-1 kind cluster. Every step lives in `bootstrap.py --phase 2`; the
Python source for each installer is under
`infra/scripts/bootstrap/phase2/`, and the install-time configuration
(YAML values, Gateway + HTTPRoute manifests) is under
`infra/scripts/bootstrap/phase2/references/`.

The skill owns the **iteration loop**. It does not own the install
logic — that's `bootstrap.py`. The loop is: run, observe, fix, repeat
until smoke tests pass cleanly.

## Pre-flight

```sh
cd blueprint
export KUBECONFIG=$PWD/infra/tofu/kubeconfig

# Phase 1 must already be done: 5-node kind cluster up, kubectl reachable.
python3 infra/scripts/bootstrap.py --phase 2 --check
```

`--check` runs only the pre-flight step (cluster reachable, helm
installed). It does NOT install anything. If it fails, fix the
underlying issue (cluster down, helm not on PATH) before continuing.

## Install

```sh
python3 infra/scripts/bootstrap.py --phase 2
```

This runs the 7-step pipeline:

| Step | What it does |
| ---- | ------------ |
| 1/7  | Pre-flight (cluster + helm reachable) |
| 2/7  | Publish the Phase-1 wildcard TLS cert into `gitlab` + `openbao` namespaces |
| 3/7  | Install Traefik with the Gateway API provider enabled |
| 4/7  | Install + initialise + unseal OpenBao |
| 5/7  | Apply Gateway + HTTPRoute manifests (Traefik routes `*.local.bruj0.net`) |
| 6/7  | Install GitLab + capture initial root password + runner token into OpenBao |
| 7/7  | Install GitLab Runner (registration token from OpenBao) |

Every step has an idempotency probe. **Re-running is safe** — installs
that are already at the target version are no-ops; OpenBao init runs
exactly once; certs are reconciled, not duplicated.

## Smoke tests

After install completes:

```sh
# 1. Trust the local CA on the host (so curl doesn't reject the cert).
sudo trust anchor infra/tls/public/ca.crt

# 2. GitLab web UI responds.
curl -ksf https://gitlab.local.bruj0.net/-/health | jq .

# 3. GitLab pods are Running.
kubectl -n gitlab get pods

# 4. OpenBao pods are Running.
kubectl -n openbao get pods

# 5. Traefik pods are Running.
kubectl -n traefik get pods

# 6. GitLab Runner pods are Running.
kubectl -n gitlab-runner get pods

# 7. The runner is registered.
kubectl -n gitlab exec deploy/gitlab-toolbox -- \
  gitlab-rails runner 'puts Ci::Runner.all.map { |r| "#{r.description} (#{r.active})" }'
```

## Iteration loop

The whole point of this skill is the loop. After each run:

1. **Read the failure.** If a step failed, the bootstrap prints
   `Phase 2 install failed: <error>` and exits non-zero. Don't read
   past that line — start there.
2. **Identify the component.** Each step maps to exactly one installer
   in `phase2/`. The step header tells you which:
   - Step 3 = Traefik → edit `phase2/traefik.py` or `references/helm-values-traefik.yaml`
   - Step 4 = OpenBao → edit `phase2/openbao.py`, `phase2/secrets.py`, or `references/helm-values-openbao.yaml`
   - Step 5 = Gateway → edit `references/gateway.yaml` or `references/httproute-*.yaml`
   - Step 6 = GitLab → edit `phase2/gitlab.py` or `references/helm-values-gitlab.yaml`
   - Step 7 = Runner → edit `phase2/runner.py` or `references/helm-values-runner.yaml`
3. **Fix and re-run.** Every step is idempotent — `bootstrap.py --phase 2`
   is the only command you need. Don't try to undo previous installs;
   let the idempotency probes handle it.
4. **Capture the lesson.** When a fix is general enough that a fresh
   Phase-2 install would also need it, add a one-line entry under
   "Common pitfalls" below. That's how this skill converges to one-shot.

## Common pitfalls

<!-- One line per pitfall: symptom → fix. Append during the iteration loop. -->

<!-- Template:
- `<error message>` → `<one-line fix>` and which file to edit.
-->

## When the install is green

All 7 smoke tests pass cleanly with no manual intervention. Commit the
changes — `VERSIONS.json`, the installer files, the YAML references,
and any "Common pitfalls" entries. The skill now reproduces the install
one-shot for any fresh Phase-1 cluster.

## How to undo

```sh
# Remove every Phase-2 chart release (OpenBao init JSON stays on disk).
helm uninstall -n traefik traefik
helm uninstall -n openbao openbao
helm uninstall -n gitlab gitlab
helm uninstall -n gitlab-runner gitlab-runner

# Wipe the gateway / httproutes / namespaces.
kubectl delete -f infra/scripts/bootstrap/phase2/references/

# Drop the secret-bootstrap state.
rm -rf infra/secrets/

# Phase 1 cluster stays up.
```

After `bootstrap.py --phase 2` again, you'll get a fresh install.