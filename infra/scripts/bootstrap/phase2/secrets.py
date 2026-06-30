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

    def __init__(self, runner: CommandRunner, paths: Paths, log: Logger) -> None:
        self._r = runner
        self._paths = paths
        self._log = log
        # Token is loaded lazily from the init file. We don't require it
        # at construction because `bao operator init` and `bao operator
        # unseal` are unauthenticated operations.
        self._token: str | None = None

    # ---------- low-level ----------

    def raw(self, bao_cmd: list[str], *, check: bool = True) -> CommandResult:
        """Run a `bao` subcommand inside the openbao-0 pod.

        Example: `client.raw(["status"])` runs `bao status` inside the pod.
        """
        cmd = ["kubectl", "exec", "--namespace", "openbao", POD_NAME, "--", "bao", *bao_cmd]
        return self._r.run(cmd, check=check)

    def kv_put(self, path: str, data: dict[str, str]) -> None:
        """`bao kv put secret/<path> k=v ...` — writes a KV v2 secret.

        `data` becomes multiple key=value pairs in the same secret. We
        pass them as repeated `k=v` arguments so the user doesn't have to
        maintain a JSON file on the host.
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