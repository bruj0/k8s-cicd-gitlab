# Secrets — How every credential in the blueprint is issued, stored, and restored

This document is the map of every secret the blueprint produces and
consumes. It exists so a new contributor can:

1. **Find a secret** they need (e.g. "what's the GitLab runner
   registration token called, and where do I read it from?").
2. **Add a secret** for a new installer ("I need to mint a JWT
   signing key; where do I persist it?").
3. **Debug a credential mismatch** on cluster recreate ("PG logs
   `password authentication failed for user "gitlab"` — is the
   stored password stale, or is the PG role itself gone?").

If you only need to *use* the stack day-to-day, the
[post-install helper](README.md#post-install-host-side-steps)
section in the README covers that. This doc is for people
**changing the bootstrap**.

---

## 1. Where secrets live in the stack

There are four places a secret can be:

| Where | Format | Persistent across `tofu destroy && apply`? | Owned by |
| --- | --- | --- | --- |
| **Host filesystem** (`infra/secrets/`) | plain text files + one YAML/JSON snapshot, mode 0600, gitignored | YES (host stays up; only `--destroy` wipes them) | the bootstrap |
| **Kubernetes Secret** in-cluster | base64 data field on an `Opaque`/`kubernetes.io/tls`/`kubernetes.io/dockerconfigjson` Secret | NO (re-created every install) | the chart, OR the bootstrap for chart-managed Secrets |
| **PostgreSQL** in CloudNativePG | role `password` column, SCRAM-SHA-256 verifier | YES if PG data is on the stable PV (CloudNativePG keeps it across cluster recreates) | `phase2/cloudnative_pg.py` for the bootstrap-minted roles, the chart for the chart-minted rails user |
| **OpenBao KV v2** | `secret/<mount>/<path>` keys | YES (OpenBao uses a stable PV for its PostgreSQL backend; the chart-bundled OpenBao uses the same CloudNativePG cluster) | whoever pushed it (the bootstrap, the chart, or a developer) |

The rule of thumb: **the bootstrap stores a single host-side
copy of every secret it minted** (because the K8s Secret's
data field is opaque to humans, and the PG role's verifier
isn't an extractable value). The exception is OpenBao — for
OpenBao we already have a stable backend (PG), so the
OpenBao-side secret is itself the persistent copy.

If you're adding a new chart-managed secret and can't find a
home for the host-side snapshot in this map, copy the
pattern from the existing snapshot in
`infra/secrets/gitlab-runtime-secrets.yaml`:

1. Find the existing K8s Secret the chart mints.
2. Add a step to `phase2/persistent_secrets.py:snapshot()`
   that dumps it to a host file at the end of every
   successful install.
3. Add a step to `phase2/persistent_secrets.py:restore()`
   that re-applies the host file to the cluster at the start
   of the next install.

---

## 2. The host-side snapshot files

All files live in `infra/secrets/` (gitignored). Mode 0600 on
every file. The directory is created by the first installer
that needs it (`paths.ensure_secrets_dir()`); `--destroy` is
the only path that wipes it.

| File | Format | Created by | Read by | Contents |
| --- | --- | --- | --- | --- |
| `openbao-init.json` | JSON | `phase2/openbao.py` on first install (init step) | `phase2/openbao.py` (unseal, login), `phase2/secrets.py` (`OpenBaoClient` root token load), `phase2/gitlab.py` (KV writes), `phase2/runner.py` (KV reads), `blueprint-secrets` CLI | `{ "root_token": "s.…", "unseal_keys": ["…"], "unseal_threshold": 1, … }` |
| `cnpg-role-passwords.json` | JSON | `phase2/cloudnative_pg.py` on every install (random password minted via `secrets.token_urlsafe(24)`; password is fresh each install but the persisted file lets `tofu destroy && apply` re-create the roles with the SAME passwords, so the chart's Secret refs that point at these passwords don't need to be edited) | `phase2/cloudnative_pg.py` on the next install (re-creates the `gitlab` + `openbao` PG roles with these exact passwords; recreates the `gitlabhq_production` + `openbao` databases if missing) | `{ "gitlab": "…", "openbao": "…" }` |
| `minio-root-user.txt` | text | `phase2/minio.py` | `blueprint-secrets` (informational; the Secret `minio` in the `minio` namespace is the canonical source) | MinIO `MINIO_ROOT_USER` |
| `minio-root-password.txt` | text | `phase2/minio.py` | as above | MinIO `MINIO_ROOT_PASSWORD` |
| `redis-password.txt` | text | `phase2/redis.py` | as above | Redis `redis-password` |
| `gitlab-runtime-secrets.yaml` | YAML | `phase2/persistent_secrets.py:snapshot()` (end of every successful install) | `phase2/persistent_secrets.py:restore()` (start of next install) | Multi-document YAML with the K8s data fields of every chart-managed Secret we want to preserve (see § 3) |

The first three files are the bootstrap's own state. The last
two are **snapshots of cluster state** — the bootstrap reads
from the cluster, writes to the host, and re-applies at the
next install. Their lifecycle is:

```
  install N (fresh)        install N+1 (recreate, no --destroy)
  ────────────────────     ───────────────────────────────────────
  helm install → mints     snapshotted runtime secrets restored
  random passwords →       from host → chart sees Secrets already
  snapshot writes them     exist → reuses passwords → on-disk
  to host at end           data still works
```

`--destroy` wipes everything in `infra/secrets/` because the
on-disk data (PG data on stable PV, OpenBao data on stable PV,
MinIO bucket contents, etc.) is **also** wiped when you blow
away `infra/data/shared/stable/`. Wiping one without the other
leaves a cluster that boots but doesn't authenticate against
its own data — exactly the failure mode the snapshots exist
to *prevent* on the no-destroy path.

---

## 3. What's in `gitlab-runtime-secrets.yaml`

This file is the snapshot of the chart-managed K8s Secrets
whose **data must match the on-disk data on the next install**.
Snapping just any secret would also work, but the file would
be 10× larger and most of it would be irrelevant (cert-manager
certs rotate, Envoy emits noise, the Runner's own credentials
are not used as PG/etc. backing state).

Concretely:

```yaml
# managed by phase2/persistent_secrets.py
---
apiVersion: v1
kind: Secret
metadata: { name: gitlab-postgresql-password, namespace: gitlab, … }
type: Opaque
data:
  postgres-password: <base64>
  postgresql-password: <base64>          # alias chart checks
  patroni-password: <base64>
  pgBouncerAdmin-password: <base64>
---
apiVersion: v1
kind: Secret
metadata: { name: gitlab-redis-password, … }
type: Opaque
data:
  redis-password: <base64>
---
apiVersion: v1
kind: Secret
metadata: { name: gitlab-minio-secret, … }
type: Opaque
data:
  accesskey: <base64>
  secretkey: <base64>
---
apiVersion: v1
kind: Secret
metadata: { name: gitlab-rails-secret, … }
type: Opaque
data:
  secrets.yml: <base64>                  # Rails secret_key_base, otp_key_base, db_key_base, …
---
apiVersion: v1
kind: Secret
metadata: { name: gitlab-gitaly-secret, … }
type: Opaque
data:
  token: <base64>
---
apiVersion: v1
kind: Secret
metadata: { name: gitlab-gitlab-kas-secret, … }
type: Opaque
data:
  kas_shared_secret: <base64>
  api_key: <base64>
---
# The chart-bundled OpenBao subchart's own root token + unseal
# material. Not the same as the bootstrap-installed OpenBao —
# they're independent deployments in different namespaces. This
# one is consumed by the GitLab rails app for Secrets Manager.
apiVersion: v1
kind: Secret
metadata: { name: gitlab-openbao-secret, … }
type: Opaque
data:
  token: <base64>
  unseal-key: <base64>                   # unseal key (the subchart ships a single key, threshold 1)
```

If you add a NEW chart or sub-chart that the blueprint
consumes via chart-managed Secrets, append it here. The
`SNAPSHOT_NAMES` frozenset in
`phase2/persistent_secrets.py` is the allow-list.

What is **deliberately not** snapshotted:

- `gitlab-wildcard-tls-{ca,key,cert}` — handled by
  `phase2/wildcard_certs.py`, which reuses its own on-disk
  cert (`infra/tls/wildcard/`).
- `openbao-0.{0,1,…}` chart-managed Secrets (the chart's
  `unseal-key` for the bootstrap-installed OpenBao) — those
  are managed by `phase2/openbao.py` reading
  `infra/secrets/openbao-init.json` directly.
- cert-manager bootstrap tokens — they're regenerated on every
  install.

---

## 4. What's in OpenBao at `secret/`

The bootstrap installs **two** OpenBao deployments:

  - **Bootstrap-installed OpenBao** (`openbao-0` in the
    `openbao` namespace, our own `openbao-0.10.1` chart) —
    the hand-off point for shared bootstrap state.
  - **Chart-bundled OpenBao** (`gitlab-openbao` Deployment in
    the `gitlab` namespace, sub-chart of GitLab 10.x) — the
    storage backend for GitLab **Secrets Manager**, consumed
    only by the GitLab rails app.

The bootstrap only writes to the **bootstrap-installed**
OpenBao. The chart-bundled one is opaque from the bootstrap's
POV — its tokens and unseal material flow through the chart
and land in `gitlab-runtime-secrets.yaml` as
`gitlab-openbao-secret`.

KV v2 is mounted at `secret/` on the bootstrap-installed
OpenBao. Keys written by the bootstrap:

| Path | Key | Written by | Read by |
| --- | --- | --- | --- |
| `secret/gitlab/initial_root_password` | `<password>` | `phase2/gitlab.py` (post-install: read the chart-minted password out of `gitlab-initial-root-password` K8s Secret, write here) | `blueprint-secrets read gitlab initial_root_password` |
| `secret/gitlab/runner/registration_token` | `<token>` | `phase2/gitlab.py` (post-install: shell into the webservice pod, run Rails to fetch `runners_registration_token`; write the value here) | `phase2/runner.py` (passes to helm via `--set runnerToken=…`) |
| `secret/gitlab/smtp/{host,port,user,password}` | `<value>` | `phase2/gitlab.py` (only if the user supplied `--smtp-*` flags) | n/a (informational; consumed by humans via `blueprint-secrets`) |

The KV paths are NOT secrets — they're bucket names in OpenBao.
The values inside them ARE secrets (and they're stored in
OpenBao's PostgreSQL backend, which uses SCRAM-SHA-256 — see
§ 5 for the password hashing story).

Reading a secret back uses `OpenBaoClient.kv_get(path, key)`
or the `blueprint-secrets` CLI:

```sh
uv run blueprint-secrets read gitlab initial_root_password   # prints the value
uv run blueprint-secrets read gitlab                         # dumps all keys at that path
uv run blueprint-secrets ui                                  # opens the OpenBao web UI in your browser
```

The CLI auto-port-forwards `127.0.0.1:8200` to the
`openbao/openbao:8200` Service on first use, so there's no
`kubectl port-forward` to remember.

---

## 5. Password hashing across the stack

Three different password stores appear in this blueprint, and
each uses a different hashing scheme:

| Where | Hash algorithm | Why |
| --- | --- | --- |
| OpenBao (`secret/<path>/<key>` values) | n/a — stored as-is. The bootstrap doesn't hash anything before pushing to OpenBao; OpenBao itself hashes only for its built-in userpass auth, which we don't use. | These are *secrets as data*, not credentials humans type. |
| Kubernetes Secret `data` field | base64 — not a hash, an encoding | K8s API is encoding-only; transport security is TLS to the API server. |
| CloudNativePG `pg_authid` roles | SCRAM-SHA-256 (`SCRAM-SHA-256$iter:4096$salt:base64$StoredKey:base64$ServerKey:base64`) | PG `password_encryption = scram-sha-256` (CNPG default). The `git` + `openbao` roles we mint via `DO $$ BEGIN IF NOT EXISTS … END $$` blocks use `CREATE ROLE … LOGIN PASSWORD '<plaintext>'`, which makes PG apply the SCRAM-SHA-256 derivation on the fly. |
| GitLab rails `secrets.yml` | n/a — `secret_key_base`, `otp_key_base`, `db_key_base`, `encrypted_settings_key_base` are random bytes used as inputs to Rails' symmetric encryption — they're stored plaintext in the K8s Secret, kept secret at-rest via the K8s RBAC boundary. | Designed to live in plaintext in trusted config. |

**Implication for "where do I read the actual password back":**

- PG roles: `kubectl -n postgresql exec postgresql-cnpg-1 -c postgres -- psql -U postgres -c "SELECT rolname, rolpassword FROM pg_authid WHERE rolname='gitlab'"`. The `rolpassword` column is the SCRAM verifier, not the plaintext. The plaintext is only in our `cnpg-role-passwords.json` snapshot.

- OpenBao: `blueprint-secrets read <path> <key>` — OpenBao just returns the value we wrote.

- K8s Secrets: `kubectl get secret <name> -n <ns> -o jsonpath='{.data.<key>}' | base64 -d`. The base64 is an encoding, not a hash.

- Rails `secrets.yml`: `kubectl get secret gitlab-rails-secret -n gitlab -o jsonpath='{.data.secrets\.yml}' | base64 -d` — multi-line YAML, base64-decoded.

---

## 6. Common workflows

### "I need the GitLab root password"

```sh
uv run blueprint-secrets read gitlab initial_root_password
```

Or open the UI:

```sh
uv run blueprint-secrets ui   # opens https://openbao.local.bruj0.net/ui
# Login: token from
uv run blueprint-secrets read _ root_token    # or $ cat infra/secrets/openbao-init.json | jq -r .root_token
# Navigate to secret/gitlab/initial_root_password
```

Or read the chart-managed K8s Secret directly (same value, no
OpenBao hop):

```sh
kubectl -n gitlab get secret gitlab-initial-root-password \
  -o jsonpath='{.data.password}' | base64 -d
```

### "I need to log into MinIO"

```sh
# Option A: from the cluster (kubectl exec into MinIO pod)
kubectl -n minio exec deploy/minio -- env | grep MINIO_ROOT

# Option B: from the host (assumes mc is installed; the chart ship includes mc inside the MinIO pod)
kubectl -n minio port-forward svc/minio 9000:9000 &
ALIAS_USER=$(cat infra/secrets/minio-root-user.txt)
ALIAS_PASS=$(cat infra/secrets/minio-root-password.txt)
mc alias set local http://localhost:9000 "$ALIAS_USER" "$ALIAS_PASS"
mc ls local/
```

### "I rotated the GitLab runner token in the UI"

The runner token comes from the GitLab rails app at
`secret_data.runners_registration_token` (or its equivalent
in the admin UI under `Admin → CI/CD → Runners → Registration
token`). When you rotate it there, re-run the bootstrap:

```sh
uv run blueprint-bootstrap --phase 2   # idempotent, picks up the fresh token from OpenBao
```

The bootstrap's post-install step on every install re-reads
the token from the rails app and writes it back to OpenBao, so
running `--phase 2` after a rotation re-syncs the OpenBao side.

### "PG authentication fails after a `tofu destroy && apply` (no `--destroy`)"

This is the canonical cross-cluster-recreate failure mode. Two
shapes:

- **The `gitlab` role's *password* doesn't match.** If the
  snapshot exists, the bootstrap re-applies the chart-managed
  Secret `gitlab-postgresql-password` so the chart still uses
  the same password. But on a fully torn-down cluster, the PG
  *role itself* is gone (PG data was wiped with the cluster).
  The bootstrap detects that and re-creates the role +
  databases with a fresh password from
  `cnpg-role-passwords.json`.

- **The role is missing entirely.** PG logs
  `FATAL: role "gitlab" does not exist`. Same fix — re-run
  `--phase 2`. The `cloudnative_pg._create_role_sql` block is
  idempotent (`IF NOT EXISTS`), so it brings the role back.

If you want a TRULY fresh install that wipes both secrets and
stable data, run `bootstrap --destroy` first.

### "I need to add a new installer with a secret"

Pattern (see `phase2/minio.py:snapshot_credentials()` for the
canonical example):

1. Bootstrap mints the secret (`secrets.token_urlsafe(...)` or
   `kubectl get secret ... -o jsonpath=… | base64 -d` to
   recover a chart-minted value).
2. Bootstrap writes to host (`path.write_text(value + "\n")` +
   `path.chmod(0o600)`).
3. Bootstrap re-applies to the cluster on the next install
   (`kubectl -n <ns> create secret generic ... --from-literal=...`).
4. If the secret should ALSO live in OpenBao (e.g. so a
   developer can read it via `blueprint-secrets`), call
   `OpenBaoClient.kv_put(...)`.
5. Update `git ignore` if the file is new (`infra/secrets/` is
   already gitignored, but new file names should be confirmed).

### "Where exactly does CNPG store my passwords?"

In the `pg_authid` system catalog, in the `rolpassword`
column. The CloudNativePG operator connects to the postgres
database using a secret (`superuser-secret` /
`application-secret` / etc.) and the password you provided via
`password.secret` is applied at boot via
`ALTER ROLE … WITH PASSWORD '…'`. The plaintext never leaves
the Operator pod; only the verifier is on disk in the
postgresql-cnpg-1 stable PV.

If you want to extract the verifier:

```sh
kubectl -n postgresql exec postgresql-cnpg-1 -c postgres -- \
  psql -U postgres -c "SELECT rolname, rolpassword FROM pg_authid WHERE rolname='gitlab'"
```

---

## 7. The `.gitignore` for `infra/secrets/`

Already excluded — nothing else should land in this folder.
If you find yourself adding a new file under `infra/secrets/`,
*also* add it to `.gitignore` as a defensive measure (the
existing patterns `infra/secrets/openbao-init.json`,
`infra/secrets/cnpg-role-passwords.json`, and the catch-all
`*.txt` / `*.json` pattern under `infra/secrets/` cover the
known files; new plaintext files with different extensions
need explicit entries).

Do not store any secret under any other location. The entire
rest of `infra/` is committed (including the
`phase2/references/` directory, which has *no* chart values
that include plaintext passwords — the chart-minted values
come from helm / kubectl, never embedded in our values
files).
