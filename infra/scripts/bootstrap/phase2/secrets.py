"""OpenBao client (wraps `bao` CLI via `kubectl exec`).

We deliberately don't pull in a Python OpenBao SDK — that would add
another runtime dep and a separate auth flow. The official `bao` CLI
is what users run anyway. We exec into the openbao-0 pod and run
`bao` commands there.

Why exec into the pod?
  - It uses the server's own auth path, so we don't have to manage a
    Service / port-forward for Phase 2.
  - The pod has the right TLS context for talking to its own server.
  - It's idempotent: the CLI returns the same output regardless of
    caller.

Trade-off: this couples to the openbao-0 StatefulSet name. If the
chart's naming convention changes, update `POD_NAME` here.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, CommandResult

POD_NAME = "openbao-0"


class OpenBaoClient:
    """`kubectl exec` wrapper around the `bao` CLI."""

    def __init__(self, runner: CommandRunner, paths: Paths, log: Logger,
                 init_file: Path | None = None) -> None:
        self._r = runner
        self._paths = paths
        self._log = log
        # Path to the init JSON. We load the token lazily — first
        # authenticated command triggers it. Init/unseal are anonymous.
        self._init_file = init_file
        self._token: str | None = None

    # ---------- auth ----------

    def _ensure_token(self) -> str:
        """Load the root token from the init file. Idempotent.

        Falls back to empty string (anonymous mode) when the file
        doesn't exist yet — useful for `bao operator init` / `status`
        before any client has logged in. Authenticated commands must
        trigger this *after* init has run.
        """
        if self._token:
            return self._token
        if self._init_file is None or not self._init_file.exists():
            return ""
        # Lazy-import json to keep module import side-effect free.
        import json as _json
        try:
            data = _json.loads(self._init_file.read_text())
            self._token = data["root_token"]
        except Exception as e:
            raise RuntimeError(
                f"could not load root_token from {self._init_file}: {e}"
            )
        return self._token

    def login(self) -> None:
        """Set the OpenBao token inside the openbao-0 pod.

        Runs `bao login -` inside the pod, with the token on stdin.
        The CLI caches it in `~/.vault-token` so subsequent
        `bao kv put`, `bao secrets enable`, etc. succeed without
        re-auth.

        Why one-time login vs per-call `VAO_TOKEN=` env? Because we
        exec via `kubectl exec ... --` (no env injection possible from
        the host side). The CLI's persistent token file is what makes
        repeated calls work without re-auth.
        """
        token = self._ensure_token()
        if not token:
            return  # Not yet initialised; nothing to log in with.
        self.raw(["login", "-"], check=False, stdin=token + "\n")

    # ---------- low-level ----------

    def raw(self, bao_cmd: list[str], *, check: bool = True,
            stdin: str | None = None) -> CommandResult:
        """Run a `bao` subcommand inside the openbao-0 pod.

        Example: `client.raw(["status"])` runs `bao status` inside the pod.
        When `stdin` is passed, `-i` is added to `kubectl exec` so the
        stdin payload reaches the remote `bao` process — without it,
        stdin is silently dropped.
        """
        cmd = ["kubectl", "exec", "--namespace", "openbao"]
        if stdin is not None:
            cmd.append("-i")
        cmd += [POD_NAME, "--", "bao", *bao_cmd]
        return self._r.run(cmd, check=check, stdin=stdin)

    # ---------- mounts ----------

    def is_kv_v2_enabled(self, path: str = "secret") -> bool:
        """Return True if `secret/<path>` is mounted as kv-v2 already.

        Used by callers to avoid re-running `secrets enable` (which
        errors on subsequent calls). `bao secrets list -format=json`
        returns a dict of mounted engines: `{ "secret/": {...} }`.
        """
        result = self.raw(["secrets", "list", "-format=json"], check=False)
        if not result.ok or not result.stdout.strip():
            return False
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        # The mount path key has a trailing slash, e.g. "secret/".
        key = path if path.endswith("/") else f"{path}/"
        entry = payload.get(key)
        if not entry:
            return False
        return entry.get("type") == "kv" and str(entry.get("options", {}).get("version", "")) == "2"

    def enable_kv_v2(self, path: str = "secret") -> None:
        """Idempotently enable a kv-v2 engine at `path` (default: `secret`).

        Required before any `kv put path/<sub>` call. Without it,
        OpenBao returns `404 no handler for path "path/..."`.
        Idempotent: errors are silently ignored if the mount already
        exists (the call returns a `path is already in use` error).
        """
        if self.is_kv_v2_enabled(path):
            self._log.info(f"KV v2 already enabled at {path}/")
            return
        self._log.info(f"Enabling KV v2 at {path}/")
        result = self.raw(
            ["secrets", "enable", "-path", path, "-version=2", "kv"], check=False
        )
        if not result.ok:
            stderr = result.stderr.strip()
            if "already in use" in stderr.lower():
                self._log.info(f"KV mount at {path}/ already exists (idempotent)")
                return
            raise RuntimeError(f"bao secrets enable {path} failed: {stderr}")
        self._log.ok(f"KV v2 mounted at {path}/")

    def login(self) -> None:
        """No-op stub kept here for backward compat with callers that
        imported the old API. The real login() lives near the top of
        this class."""
        return None

    # ---------- data ----------

    def kv_put(self, path: str, data: dict[str, str]) -> None:
        """`bao kv put secret/<path> k=v ...` — writes a KV v2 secret.

        `data` becomes multiple key=value pairs in the same secret. We
        pass them as repeated `k=v` arguments so the user doesn't have to
        maintain a JSON file on the host.

        Requires the caller to have called `login()` first (so the
        server accepts writes).
        """
        args: list[str] = ["kv", "put", f"secret/{path}"]
        for k, v in data.items():
            args += [f"{k}={v}"]
        result = self.raw(args)
        if not result.ok:
            raise RuntimeError(
                f"bao kv put secret/{path} failed: {result.stderr.strip()}"
            )

    def kv_get(self, path: str, key: str | None = None) -> str | dict[str, str]:
        """`bao kv get secret/<path>` — reads a KV v2 secret.

        With `key`, returns just that field's value. Without, returns
        the whole secret as a dict.

        Returns empty string/dict if the secret doesn't exist (this is
        how callers detect "first install" — they catch the result and
        fall through to a write path). Raises RuntimeError only on
        unexpected errors (kubectl exec failed, etc.).
        """
        cmd = ["kv", "get", "-format=json", f"secret/{path}"]
        result = self.raw(cmd)
        # Dry-run returns ok=True with empty stdout. Real runs return
        # ok=False with a non-zero rc + stderr like "No value found at ..."
        # Treat both as "no secret yet" so the caller can mint + write.
        if not result.ok or not result.stdout.strip():
            return "" if key is not None else {}
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "" if key is not None else {}
        data = payload.get("data", {}).get("data", {})
        if key is None:
            return data
        return str(data.get(key, ""))

    # ---------- convenience ----------

    def write_gitlab_secrets(self, *, initial_root_password: str, smtp_host: str = "",
                             smtp_port: str = "", smtp_user: str = "", smtp_password: str = "") -> None:
        """Write the secrets GitLab reads at boot.

        Called by the GitlabInstaller right before installing the chart
        so the values exist when GitLab's pre-install Job queries them.
        """
        # Path: secret/gitlab
        self.kv_put("gitlab", {"initial_root_password": initial_root_password})
        # Path: secret/gitlab/smtp (only if any SMTP value is non-empty)
        if any((smtp_host, smtp_port, smtp_user, smtp_password)):
            self.kv_put("gitlab/smtp", {
                "host": smtp_host,
                "port": smtp_port,
                "user": smtp_user,
                "password": smtp_password,
            })

    def fetch_gitlab_runner_registration_token(self) -> str:
        """Fetch the GitLab runner registration token from OpenBao.

        The token is written by `gitlab.py` after the chart install
        completes (via `gitlab-rails runner` inside the GitLab pod). If
        it's not there yet, raise — the install pipeline ordering must
        guarantee it exists.
        """
        try:
            token = self.kv_get("gitlab/runner", "registration_token")
        except RuntimeError as e:
            raise RuntimeError(
                "GitLab runner registration token not in OpenBao at "
                "secret/gitlab/runner/registration_token. The GitLab "
                "installer should have written it after the chart finished "
                "initialising. Re-run --phase 2 to retry."
            ) from e
        return str(token)