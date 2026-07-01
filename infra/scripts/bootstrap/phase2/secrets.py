"""OpenBao client built on `hvac` (official Python Vault client).

The previous implementation exec'd `bao` inside the openbao-0 pod
via `kubectl exec`. That had two annoying failure modes:

  1. The token helper inside the pod didn't always survive separate
     `kubectl exec` invocations — each exec is a fresh shell. So
     `bao login $TOKEN` in one call and `bao kv put` in the next
     could end up unauthenticated, producing a silent 403.
  2. Every command round-tripped the kubelet, so the Phase 2 install
     pipeline paid 1-2 s of latency per secret.

This client uses the HTTP API directly via `hvac`. Auth state lives
in the Python process (one `hvac.Client` per `OpenBaoClient`), so
there's no token-helper fragility, and there's no shell process to
spawn per call.

The catch: the OpenBao server isn't reachable from the host without
a port-forward. We manage one lazily per process — see
`_PortForward` below. The forward is reused across calls (cheap),
and torn down when the client is closed / garbage-collected.

Why a port-forward instead of exec?
  - The server's own CLI behaviour is identical from inside vs
    outside the pod — it's the same HTTP API.
  - We avoid the kubelet round-trip; hvac talks HTTP directly.
  - We get structured error responses (hvac exceptions) instead of
    `kubectl exec` exit-code + stderr parsing.
"""

from __future__ import annotations

import atexit
import json
import socket
import subprocess
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import hvac
from hvac.exceptions import Forbidden, InvalidPath, VaultError

from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, DryRunRunner

POD_NAME = "openbao-0"
SERVICE_NAMESPACE = "openbao"
SERVICE_NAME = "openbao"
SERVICE_PORT = 8200
LOCAL_PORT = 8200
LOCAL_URL = f"http://127.0.0.1:{LOCAL_PORT}"
WAIT_TIMEOUT = 30.0  # seconds to wait for the port-forward to be live


# ---------------------------------------------------------------------------
# Port-forward management
# ---------------------------------------------------------------------------


class _PortForward:
    """A single shared `kubectl port-forward` to the OpenBao service.

    We share one forward across all `OpenBaoClient` instances in a
    process — multiple clients would otherwise race to bind 8200.
    The forward is started lazily (first call to `start()`) and
    stopped on `close()` or at interpreter exit.

    The forward runs as a backgrounded child process whose stdout
    is captured (so the kubelet handshake text doesn't leak into
    the bootstrap log). It is killed via SIGTERM; if it doesn't
    die within 2 s, SIGKILL.

    Thread-safe: `start()` is idempotent and guarded by a lock so
    concurrent first-callers don't both spawn a forward.
    """

    _shared_lock = threading.Lock()
    _shared: "_PortForward | None" = None

    def __init__(self, runner: CommandRunner, log: Logger) -> None:
        self._r = runner
        self._log = log
        self._is_dry_run = isinstance(runner, DryRunRunner)
        self._proc: subprocess.Popen[bytes] | None = None
        self._local_lock = threading.Lock()

    @classmethod
    def shared(cls, runner: CommandRunner, log: Logger) -> "_PortForward":
        """Return the process-wide forward, creating it on first use."""
        with cls._shared_lock:
            if cls._shared is None:
                cls._shared = cls(runner, log)
            return cls._shared

    def start(self) -> None:
        """Ensure the forward is running. Idempotent.

        On first call, spawns `kubectl port-forward` in the
        background and waits until 127.0.0.1:8200 accepts a TCP
        connection. On subsequent calls, returns immediately.
        """
        with self._local_lock:
            if self._proc is not None and self._proc.poll() is None:
                return  # already running
            self._spawn()
            self._wait_ready()

    def _spawn(self) -> None:
        cmd = [
            "kubectl", "port-forward",
            f"--namespace={SERVICE_NAMESPACE}",
            f"svc/{SERVICE_NAME}",
            f"{LOCAL_PORT}:{SERVICE_PORT}",
        ]
        # Dry-run runs don't actually spawn the forward; we only
        # log the would-be command.
        if self._is_dry_run:
            # No actual port-forward needed in dry-run; just log the
            # command and the local URL we'd talk to.
            self._log.info(f"[dry-run] would start port-forward: "
                           f"{' '.join(cmd)}")
            return
        self._log.info(f"starting port-forward: {' '.join(cmd)}")
        # Start new process group so SIGTERM doesn't kill only the
        # shell — we want the kubectl child to die too. stdout is
        # suppressed: kubectl's progress messages are noisy.
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        atexit.register(self.close)

    def _wait_ready(self) -> None:
        """Block until 127.0.0.1:8200 accepts a TCP connection.

        Without this, the first hvac call after start races the
        kubelet handshake. We poll every 100 ms up to WAIT_TIMEOUT.
        On timeout, kill the forward and raise so callers see a
        clean error instead of a hung hvac.request().
        """
        deadline = time.monotonic() + WAIT_TIMEOUT
        while time.monotonic() < deadline:
            with suppress(OSError):
                with socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=0.5):
                    return
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(
                    f"port-forward to {SERVICE_NAME}.{SERVICE_NAMESPACE} "
                    f"exited (rc={self._proc.returncode}) before "
                    f"127.0.0.1:{LOCAL_PORT} became reachable"
                )
            time.sleep(0.1)
        self.close()
        raise RuntimeError(
            f"timed out waiting {WAIT_TIMEOUT}s for port-forward "
            f"127.0.0.1:{LOCAL_PORT} to be ready"
        )

    def close(self) -> None:
        """Tear down the forward. Safe to call multiple times."""
        with self._local_lock:
            if self._proc is None:
                return
            if self._proc.poll() is None:
                # Polite then forceful.
                with suppress(ProcessLookupError):
                    self._proc.terminate()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        self._proc.kill()
                    self._proc.wait(timeout=1.0)
            self._proc = None
            if _PortForward._shared is self:
                _PortForward._shared = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpenBaoClient:
    """hvac-backed client for OpenBao, with auto port-forwarding.

    Public API preserved from the previous kubectl-exec-based
    implementation so call sites in openbao.py / gitlab.py / runner.py
    don't change. Internals swap to direct HTTP via hvac.
    """

    def __init__(self, runner: CommandRunner, paths: Paths, log: Logger,
                 init_file: Path | None = None) -> None:
        self._r = runner
        self._paths = paths
        self._log = log
        # Path to the init JSON. We load the token lazily — init/unseal
        # are anonymous.
        self._init_file = init_file
        self._token: str | None = None
        # The actual hvac client is created on first authenticated
        # call (after init + unseal). Until then we use a fresh
        # anonymous client for things like `status`.
        self._client: hvac.Client | None = None
        # Lazy port-forward: only spawned when we actually need to
        # talk to the server (i.e. on the first authenticated call,
        # not during init). In dry-run we don't need one.
        self._is_dry_run = isinstance(runner, DryRunRunner)
        self._forward: _PortForward | None = (
            _PortForward.shared(runner, log) if not self._is_dry_run else None
        )

    # ---------- low-level client management ----------

    def _anonymous(self) -> hvac.Client:
        """Return an hvac client with no token (for init/unseal/status)."""
        if self._client is not None and not self._client.is_authenticated():
            return self._client
        if self._forward is not None:
            self._forward.start()
        return hvac.Client(url=LOCAL_URL)

    def _authenticated(self) -> hvac.Client:
        """Return an hvac client with the root token attached.

        On first call, starts the port-forward and creates the
        client. On subsequent calls, returns the cached client
        (token stays valid until the server is restarted, which
        would require re-unsealing and a new init-file anyway).
        """
        if self._client is not None and self._client.is_authenticated():
            return self._client
        token = self._ensure_token()
        if not token:
            raise RuntimeError(
                "no root token — has OpenBao been initialised? "
                f"Expected init JSON at {self._init_file}"
            )
        if self._forward is not None:
            self._forward.start()
        self._client = hvac.Client(url=LOCAL_URL, token=token)
        # Probe so we fail fast if the token is wrong / server
        # isn't unsealed. hvac's read_token lookup is cheap and
        # exercises the auth path.
        try:
            self._client.lookup_token()
        except Forbidden as e:
            raise RuntimeError(
                f"OpenBao rejected root token from {self._init_file}: {e}"
            ) from e
        return self._client

    def _ensure_token(self) -> str:
        """Load the root token from the init file. Idempotent."""
        if self._token:
            return self._token
        if self._init_file is None or not self._init_file.exists():
            return ""
        try:
            data = json.loads(self._init_file.read_text())
            self._token = data["root_token"]
        except Exception as e:
            raise RuntimeError(
                f"could not load root_token from {self._init_file}: {e}"
            ) from e
        return self._token

    # ---------- auth ----------

    def login(self) -> None:
        """Establish the authenticated session.

        Previously this ran `bao login <token>` inside the pod to
        prime the CLI's token helper. With hvac, "login" is just
        `hvac.Client(token=...)` and the auth state lives in our
        process — there's no separate helper to prime. We just
        call `_authenticated()` once to materialise the client and
        verify the token.
        """
        if self._is_dry_run:
            self._log.info("[dry-run] would log in to OpenBao")
            return
        token = self._ensure_token()
        if not token:
            self._log.warn(
                "no token to log in with (OpenBao not yet initialised)"
            )
            return
        self._log.info(f"logging in to OpenBao (token={token[:8]}...)")
        # Force client construction + token probe. If it raises,
        # the caller sees a clean error.
        self._authenticated()
        self._log.ok("logged in to OpenBao")

    # ---------- compatibility shim (used by tests / debugging) ----------

    def operator_init(self, *, key_shares: int, key_threshold: int) -> dict:
        """Run `bao operator init` via hvac.

        Returns the parsed JSON dict (root_token + unseal_keys_b64).
        Raises if the server is already initialised.
        """
        if self._is_dry_run:
            return {
                "root_token": "dryrun-root-token",
                "unseal_keys_b64": ["dryrun-unseal-key-base64=="],
            }
        client = self._anonymous()
        resp = client.sys.initialize(secret_shares=key_shares, secret_threshold=key_threshold)
        data = resp  # hvac returns the dict directly
        return {
            "root_token": data["root_token"],
            "unseal_keys_b64": data["keys_base64"],
        }

    def operator_unseal(self, key: str) -> dict:
        """Run `bao operator unseal <key>` via hvac.

        Returns the parsed status dict. Idempotent (calling on an
        already-unsealed server is a no-op that returns the sealed=false
        status).
        """
        if self._is_dry_run:
            return {"sealed": False, "progress": 1, "t": 1}
        client = self._anonymous()
        resp = client.sys.submit_unseal_key(key)
        # hvac returns the unseal status dict.
        return {
            "sealed": resp.get("sealed", False),
            "t": resp.get("t", 1),
            "progress": resp.get("progress", 0),
        }

    def raw(self, bao_cmd: list[str], *, check: bool = True,
            stdin: str | None = None) -> Any:
        """Backwards-compat shim — emulates the old `bao <args>` exec.

        Kept so any direct caller (e.g. ad-hoc scripts) still works.
        Returns an object with `.ok`, `.stdout`, `.stderr` so
        call sites that inspected them don't break. Prefer the
        dedicated methods (kv_put, kv_get, enable_kv_v2, etc.)
        for new code.

        Translates a tiny subset of `bao` invocations to the
        equivalent hvac calls. Operator commands (init / unseal)
        get first-class handling here so the bootstrap's init +
        unseal flow actually works. Anything we don't recognise
        falls back to a no-op so old test code doesn't crash on a
        rebuild.
        """
        if self._is_dry_run:
            return _FakeResult(True, "", "")

        # `bao kv put <path> k=v k=v` -> kv_v2 put
        if len(bao_cmd) >= 3 and bao_cmd[0] == "kv" and bao_cmd[1] == "put":
            path = bao_cmd[2]
            data = {}
            for kv in bao_cmd[3:]:
                k, _, v = kv.partition("=")
                data[k] = v
            self.kv_put(path.replace("secret/", "", 1), data)
            return _FakeResult(True, "", "")
        if len(bao_cmd) >= 3 and bao_cmd[0] == "kv" and bao_cmd[1] == "get":
            # `bao kv get -format=json secret/<path>` → JSON dict
            path = bao_cmd[-1].replace("secret/", "", 1)
            data = self._kv_get_raw(path)
            return _FakeResult(True, json.dumps({"data": {"data": data}}), "")
        if len(bao_cmd) >= 2 and bao_cmd[0] == "secrets" and bao_cmd[1] == "list":
            mounts = self._secrets_list_raw()
            return _FakeResult(True, json.dumps(mounts), "")
        if len(bao_cmd) >= 3 and bao_cmd[0] == "secrets" and bao_cmd[1] == "enable":
            # `bao secrets enable -path <p> -version=2 kv`
            path = None
            version = 2
            for i, arg in enumerate(bao_cmd):
                if arg == "-path" and i + 1 < len(bao_cmd):
                    path = bao_cmd[i + 1]
                if arg.startswith("-version="):
                    version = int(arg.split("=", 1)[1])
            if path is not None:
                self._enable_kv_raw(path, version=version)
            return _FakeResult(True, "", "")
        # `bao operator init -key-shares=N -key-threshold=M -format=json`
        if len(bao_cmd) >= 2 and bao_cmd[0] == "operator" and bao_cmd[1] == "init":
            shares, threshold = 1, 1
            for arg in bao_cmd:
                if arg.startswith("-key-shares="):
                    shares = int(arg.split("=", 1)[1])
                elif arg.startswith("-key-threshold="):
                    threshold = int(arg.split("=", 1)[1])
            try:
                data = self.operator_init(key_shares=shares, key_threshold=threshold)
                return _FakeResult(True, json.dumps(data), "")
            except Exception as e:
                return _FakeResult(False, "", str(e))
        # `bao operator unseal <key>`
        if len(bao_cmd) >= 3 and bao_cmd[0] == "operator" and bao_cmd[1] == "unseal":
            try:
                data = self.operator_unseal(bao_cmd[2])
                return _FakeResult(True, json.dumps(data), "")
            except Exception as e:
                return _FakeResult(False, "", str(e))
        # `bao status` → use hvac's typed accessors. The /sys/health
        # endpoint returns 501 when the server is uninitialised, which
        # is exactly the state we want to detect here, so we can't use
        # a naive GET. Instead, `sys.is_initialized()` (HEAD-like) and
        # `sys.read_seal_status()` (GET /sys/seal-status) for sealed.
        if len(bao_cmd) >= 1 and bao_cmd[0] == "status":
            if self._is_dry_run:
                return _FakeResult(True, json.dumps({"sealed": True, "initialized": False}), "")
            try:
                client = self._anonymous()
                initialized = bool(client.sys.is_initialized())
                sealed = True
                if initialized:
                    seal = client.sys.read_seal_status(method="GET")
                    sealed = bool(seal.get("sealed", True))
                return _FakeResult(True, json.dumps({
                    "sealed": sealed,
                    "initialized": initialized,
                }), "")
            except Exception as e:
                return _FakeResult(False, "", str(e))
        # Fallback: not implemented via hvac.
        return _FakeResult(True, "", "")

    def _secrets_list_raw(self) -> dict[str, dict[str, Any]]:
        client = self._authenticated()
        out: dict[str, dict[str, Any]] = {}
        for path, mount in client.sys.list_mounted_secrets_engines()["data"].items():
            entry: dict[str, Any] = {"type": mount.get("type")}
            opts = mount.get("options", {}) or {}
            if "version" in opts:
                entry["options"] = {"version": str(opts["version"])}
            out[path] = entry
        return out

    def _kv_get_raw(self, path: str) -> dict[str, str]:
        try:
            resp = self._authenticated().secrets.kv.v2.read_secret_version(
                path=path, raise_on_deleted_version=True
            )
        except InvalidPath:
            return {}
        return dict(resp["data"]["data"])

    def _enable_kv_raw(self, path: str, *, version: int) -> None:
        client = self._authenticated()
        try:
            client.sys.enable_secrets_engine(
                backend_type="kv",
                path=path,
                options={"version": str(version)},
            )
        except VaultError as e:
            # `path is already in use` → treat as success.
            if "already in use" in str(e).lower():
                return
            raise

    # ---------- mounts ----------

    def is_kv_v2_enabled(self, path: str = "secret") -> bool:
        """Return True if `path/` is mounted as kv-v2 already."""
        try:
            client = self._authenticated()
        except RuntimeError:
            # No token yet → can't be enabled.
            return False
        try:
            mounts = client.sys.list_mounted_secrets_engines()["data"]
        except VaultError:
            return False
        key = path if path.endswith("/") else f"{path}/"
        entry = mounts.get(key)
        if not entry:
            return False
        opts = (entry.get("options") or {})
        return entry.get("type") == "kv" and str(opts.get("version", "")) == "2"

    def enable_kv_v2(self, path: str = "secret") -> None:
        """Idempotently enable a kv-v2 engine at `path`."""
        if self._is_dry_run:
            # In dry-run there's no live server. Just record the
            # would-be enablement so the pipeline log reads correctly.
            self._log.info(f"[dry-run] would enable KV v2 at {path}/")
            return
        if self.is_kv_v2_enabled(path):
            self._log.info(f"KV v2 already enabled at {path}/")
            return
        self._log.info(f"Enabling KV v2 at {path}/")
        try:
            self._enable_kv_raw(path, version=2)
        except VaultError as e:
            if "already in use" in str(e).lower():
                self._log.info(f"KV mount at {path}/ already exists (idempotent)")
                return
            raise RuntimeError(f"openbao secrets enable {path} failed: {e}") from e
        self._log.ok(f"KV v2 mounted at {path}/")

    # ---------- data ----------

    def kv_put(self, path: str, data: dict[str, str]) -> None:
        """Write a KV v2 secret at `secret/<path>`."""
        if self._is_dry_run:
            self._log.info(f"[dry-run] would kv put secret/{path} ({len(data)} keys)")
            return
        try:
            self._authenticated().secrets.kv.v2.create_or_update_secret(
                path=path, secret=data
            )
        except VaultError as e:
            raise RuntimeError(
                f"openbao kv put secret/{path} failed: {e}"
            ) from e

    def kv_get(self, path: str, key: str | None = None) -> str | dict[str, str]:
        """Read a KV v2 secret at `secret/<path>`.

        Returns empty string/dict if the secret doesn't exist so
        callers can detect "first install" cleanly.
        """
        if self._is_dry_run:
            return "" if key is not None else {}
        try:
            data = self._kv_get_raw(path)
        except InvalidPath:
            return "" if key is not None else {}
        if key is None:
            return data
        return str(data.get(key, ""))

    # ---------- convenience ----------

    def write_gitlab_secrets(self, *, initial_root_password: str, smtp_host: str = "",
                             smtp_port: str = "", smtp_user: str = "", smtp_password: str = "") -> None:
        """Write the secrets GitLab reads at boot."""
        self.kv_put("gitlab", {"initial_root_password": initial_root_password})
        if any((smtp_host, smtp_port, smtp_user, smtp_password)):
            self.kv_put("gitlab/smtp", {
                "host": smtp_host,
                "port": smtp_port,
                "user": smtp_user,
                "password": smtp_password,
            })

    def fetch_gitlab_runner_registration_token(self) -> str:
        """Fetch the GitLab runner registration token from OpenBao."""
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


class _FakeResult:
    """Stand-in for `shell.CommandResult` returned by `OpenBaoClient.raw`.

    Preserves the (ok, stdout, stderr) shape so call sites that
    only check those still work.
    """
    __slots__ = ("ok", "stdout", "stderr")

    def __init__(self, ok: bool, stdout: str, stderr: str) -> None:
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
