"""OpenBao (KV v2 secret store) — install + initialise + unseal.

OpenBao is the secret backend for the rest of Phase 2. Per spec:
"Secrets must be stored in OpenBao and never stored in plain."

Lifecycle (each step is idempotent — safe to re-run):

    1. install()       helm install openbao via the cached chart
    2. wait_for_ready() block until the openbao-0 pod is Running+Ready
    3. init()           run `bao operator init` on first install;
                        persist the resulting JSON to
                        infra/secrets/openbao-init.json (gitignored, 0600)
    4. unseal()         run `bao operator unseal` if the server reports
                        sealed; needed every restart
    5. secrets.write_*  callers (gitlab installer) use OpenBaoClient to
                        put GitLab's initial root password + SMTP creds

The init/unseal pattern is what every OpenBao/HashiCorp Vault user
does. The unseal key is intentionally stored in
`infra/secrets/openbao-init.json` on the host because we need it on
every cluster restart. The file is gitignored and chmod 600.

If you'd rather not have a local on-disk copy, the unseal flow can
be replaced with a transit-auto-unseal backend — but that's out of
scope for Phase 2's iteration loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..app_installer import HelmAppInstaller, HelmAppSpec, HelmChartCache, UserStep
from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, CommandResult
from .secrets import OpenBaoClient


INIT_FILE_NAME = "openbao-init.json"
INIT_KEY_SHARES = 1
INIT_KEY_THRESHOLD = 1


@dataclass(frozen=True)
class OpenBaoInitOutput:
    """Subset of `bao operator init -format=json` that we persist."""

    unseal_keys_b64: tuple[str, ...]
    root_token: str

    @property
    def first_unseal_key(self) -> str:
        return self.unseal_keys_b64[0]


class OpenBaoInstaller(HelmAppInstaller):
    """OpenBao KV v2 server + init/unseal orchestration."""

    NAMESPACE = "openbao"
    RELEASE = "openbao"
    REPO_KEY = "openbao"

    def __init__(self, runner: CommandRunner, paths: Paths, cache: HelmChartCache, log: Logger) -> None:
        super().__init__(
            runner, paths, cache, log,
            HelmAppSpec(
                repo_key=self.REPO_KEY,
                release=self.RELEASE,
                namespace=self.NAMESPACE,
                wait=True,
                values_files=(
                    str(paths.phase2_refs_dir / "helm-values-openbao.yaml"),
                ),
            ),
        )
        # The init-file path doubles as the auth token source. The
        # shared OpenBaoClient (constructed once in app.py) also takes
        # this; we make sure of that here too.
        self._client = OpenBaoClient(runner, paths, log,
                                     init_file=self.init_file_path())

    def reload_client(self, client: OpenBaoClient) -> None:
        """Allow app.py to swap in the *shared* OpenBaoClient (so the
        GitLab and Runner installers see the same auth state)."""
        self._client = client

    # ---------- idempotency probes ----------

    def init_file_path(self) -> Path:
        """The path on disk where we persist the init JSON."""
        return self._paths.secrets_dir / INIT_FILE_NAME

    def is_initialised(self) -> bool:
        return self.init_file_path().exists()

    def is_sealed(self) -> bool:
        """Returns True if the OpenBao server is sealed (needs unseal).

        `bao status` returns exit code 2 when sealed (per OpenBao's
        documented convention). We must use `check=False` so the runner
        doesn't raise — `result.ok` is fine to read regardless.
        """
        result = self._client.raw(["status"], check=False)
        if not result.ok and not result.stdout.strip():
            return True  # pod unreachable or no output → assume sealed
        try:
            payload = json.loads(result.stdout)
        except Exception:
            return True
        return bool(payload.get("sealed", True))

    # ---------- lifecycle steps ----------

    def install(self) -> HelmAppInstaller.AppPrepResultLike:
        """Install the chart AND ensure the server is initialised + unsealed.

        Order: chart install → wait for pod `Running` (not Ready: OpenBao's
        readiness probe enforces unsealed-state) → init (first run) →
        unseal → wait until pod Ready.
        Returns the AppPrepResult from the chart install.
        """
        result = super().install()
        self._wait_for_pod_running()
        # Once the container is Running (not sealed-init barring the
        # kernel-level storage to begin), the OpenBao listener accepts
        # `bao operator init` over the unix socket / service. We init,
        # then unseal — each idempotent against re-runs.
        if not self.is_initialised():
            self._init()
        if self.is_sealed():
            self._unseal()
        # Finally block on Ready so downstream steps (Gateway applies
        # that query Gateway resources, etc.) have a green pod.
        self._wait_for_pod_ready()
        return result

    # ---------- internals ----------

    def _wait_for_pod_running(self, timeout_s: int = 180) -> None:
        """Block until `openbao-0` is at least `Running` (not necessarily `Ready`).

        OpenBao's ReadinessProbe fails until the server is initialised AND
        unsealed — but init/unseal happen in *this* installer right after
        the chart install. So we wait only for `Running` here; the
        subsequent `_init` / `_unseal` will take the pod from
        `Running-but-unsealed` → `Ready`.
        """
        self._log.info(f"Waiting for pod {self.RELEASE}-0 to be Running in {self.NAMESPACE} (timeout {timeout_s}s)")
        self._r.run([
            "kubectl", "wait", "--namespace", self.NAMESPACE,
            "--for=jsonpath={.status.phase}=Running",
            f"pod/{self.RELEASE}-0",
            f"--timeout={timeout_s}s",
        ])
        self._log.ok(f"Pod {self.RELEASE}-0 is Running")

    def _wait_for_pod_ready(self, timeout_s: int = 180) -> None:
        """Block until `openbao-0` is Running and Ready, or raise.

        Convenience used by tests / assertions. Normal init flow uses
        `_wait_for_pod_running` (which doesn't block on inited state)
        + `_init` + `_unseal`.
        """
        self._log.info(f"Waiting for pod {self.RELEASE}-0 in {self.NAMESPACE} (timeout {timeout_s}s)")
        self._r.run([
            "kubectl", "wait", "--namespace", self.NAMESPACE,
            "--for=condition=Ready",
            f"pod/{self.RELEASE}-0",
            f"--timeout={timeout_s}s",
        ])
        self._log.ok(f"Pod {self.RELEASE}-0 is Ready")

    def _init(self) -> OpenBaoInitOutput:
        """Run `bao operator init` and persist the result to disk.

        Idempotent: errors if already initialised. We check `is_initialised`
        before calling, so this method is only called on the first install.

        Dry-run: returns a synthetic init output so the rest of the pipeline
        can preview. The on-disk JSON is NOT written in dry-run.
        """
        from ..shell import DryRunRunner
        if isinstance(self._client._r, DryRunRunner):
            self._log.info("[dry-run] returning synthetic OpenBao init output")
            return OpenBaoInitOutput(
                unseal_keys_b64=("dryrun-unseal-key-base64==",),
                root_token="dryrun-root-token",
            )
        self._log.info(f"Initialising OpenBao (key-shares={INIT_KEY_SHARES}, threshold={INIT_KEY_THRESHOLD})")
        result = self._client.raw([
            "operator", "init",
            f"-key-shares={INIT_KEY_SHARES}",
            f"-key-threshold={INIT_KEY_THRESHOLD}",
            "-format=json",
        ])
        if not result.ok:
            raise RuntimeError(f"bao operator init failed: {result.stderr.strip()}")
        try:
            payload = json.loads(result.stdout)
        except Exception as e:
            raise RuntimeError(f"bao operator init returned non-JSON output: {e}\nstdout={result.stdout[:400]}")
        output = OpenBaoInitOutput(
            unseal_keys_b64=tuple(payload["unseal_keys_b64"]),
            root_token=payload["root_token"],
        )
        # Persist to disk, mode 0600.
        self._paths.ensure_secrets_dir()
        path = self.init_file_path()
        path.write_text(json.dumps({
            "unseal_keys_b64": list(output.unseal_keys_b64),
            "root_token": output.root_token,
        }, indent=2))
        path.chmod(0o600)
        self._log.ok(f"OpenBao initialised; init JSON written to {path}")
        return output

    def _unseal(self) -> None:
        """Run `bao operator unseal` with the persisted key, if sealed.

        Dry-run: skips the actual call so the pipeline can preview.
        """
        from ..shell import DryRunRunner
        if isinstance(self._client._r, DryRunRunner):
            self._log.info("[dry-run] skipping actual unseal")
            return
        if not self.is_initialised():
            raise RuntimeError("OpenBao is not initialised; call _init() first.")
        path = self.init_file_path()
        payload = json.loads(path.read_text())
        key = payload["unseal_keys_b64"][0]
        self._log.info("Unsealing OpenBao")
        self._client.raw(["operator", "unseal", key])
        self._log.ok("OpenBao unsealed")

    def user_handoff_steps(self) -> list[UserStep]:
        """After install, show how to read the init JSON + open the UI."""
        init_path = self.init_file_path()
        return [
            UserStep(
                title=f"OpenBao unseal key + root token live in {init_path} (chmod 600, gitignored).",
                lines=(
                    f"# Read them safely (never commit):",
                    f"cat {init_path} | jq .",
                    "",
                    f"# Set up your shell to talk to OpenBao:",
                    f"export VAO_ADDR=http://localhost:8200",
                    f"# (the UI is also exposed via the gateway; see httproute-openbao.yaml)",
                ),
            ),
        ]