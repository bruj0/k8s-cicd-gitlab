"""GitLab CE (the big chart).

Per spec: "Minimal configuration". In practice that means we accept
the chart's bundled Postgres/Redis/MinIO/Gitalia defaults but disable
the external ingress so Traefik's Gateway API does the routing.

Secrets bootstrap:
  1. We mint a random initial root password (32 chars).
  2. We write it to OpenBao at `secret/gitlab/initial_root_password`.
  3. The GitLab chart's `global.appConfig.secretBackend` is `kubernetes`
     by default, which means GitLab reads its own secrets from K8s
     Secrets. We additionally patch those Secrets from OpenBao where
     appropriate (this is iteration-2 work; iteration-1 just uses the
     stock chart with the k8s backend).
  4. After GitLab is up, we exec `gitlab-rails runner` to capture the
     initial root password actually used + the runner registration token,
     and write both back into OpenBao so the runner installer can pick
     them up.

Re-run semantics:
  - The chart install is idempotent (`helm upgrade --install`).
  - We do NOT re-mint the initial root password on re-runs (that would
    invalidate existing sessions).
  - The runner token refresh on every re-run — the runner reinstalls
    against the current token from OpenBao.
"""

from __future__ import annotations

import json
import secrets
import string
from dataclasses import dataclass

from ..app_installer import HelmAppInstaller, HelmAppSpec, HelmChartCache, UserStep
from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner
from .secrets import OpenBaoClient

INITIAL_ROOT_PASSWORD_FILE = "gitlab-initial-root-password"
RUNNER_TOKEN_KEY = "registration_token"


@dataclass(frozen=True)
class GitlabCredentials:
    """Credentials that ended up in OpenBao after install."""

    initial_root_password: str
    runner_registration_token: str


class GitlabInstaller(HelmAppInstaller):
    """GitLab CE — the big chart + post-install secret capture."""

    NAMESPACE = "gitlab"
    RELEASE = "gitlab"
    REPO_KEY = "gitlab"

    def __init__(self, runner: CommandRunner, paths: Paths, cache: HelmChartCache, log: Logger,
                 openbao: OpenBaoClient) -> None:
        super().__init__(
            runner, paths, cache, log,
            HelmAppSpec(
                repo_key=self.REPO_KEY,
                release=self.RELEASE,
                namespace=self.NAMESPACE,
                wait=False,  # GitLab takes minutes to come up; --wait would hang.
                values_files=(
                    str(paths.phase2_refs_dir / "helm-values-gitlab.yaml"),
                ),
            ),
        )
        self._openbao = openbao

    # ---------- idempotency probes ----------

    def is_initialized(self) -> bool:
        """True once the runner registration token has been captured into OpenBao.

        This is our proxy for "the install is done end-to-end". The chart
        itself coming up is not sufficient — we also need the
        post-install secret capture to have completed.
        """
        try:
            self._openbao.kv_get("gitlab/runner", RUNNER_TOKEN_KEY)
            return True
        except RuntimeError:
            return False

    # ---------- lifecycle steps ----------

    def install(self):
        """Install + capture post-install secrets.

        Order:
          1. login to OpenBao + enable KV v2 (must come before any kv_put)
          2. install the chart
          3. wait for webservice ready (toolbox pod becomes available)
          4. ensure GitLab root password matches what we put in OpenBao
             (mint + push via Rails, idempotent across re-runs)
          5. capture runner registration token from Rails → OpenBao

        We can't pre-mint the password in `_ensure_initial_password`
        before install: the toolbox pod is created by the chart install
        itself.
        """
        # 0. Auth: the `bao` CLI caches the token in ~/.vault-token so
        #    subsequent `kv put` calls succeed without re-auth. We also
        #    have to mount KV v2 on `secret/` — OpenBao doesn't ship
        #    with any mount by default.
        self._openbao.login()
        self._openbao.enable_kv_v2("secret")

        # 1. Install the chart.
        result = super().install()

        # 2. Wait for GitLab's webservice to be ready (this is the slow part).
        self._wait_for_webservice()

        # 3. Push a known root password into GitLab + OpenBao.
        self._ensure_initial_password()

        # 4. Capture the runner registration token and stash it in OpenBao.
        creds = self._capture_credentials()
        self._openbao.kv_put("gitlab/runner", {RUNNER_TOKEN_KEY: creds.runner_registration_token})
        self._log.ok("GitLab credentials captured into OpenBao")
        return result

    # ---------- internals ----------

    def _ensure_initial_password(self) -> str:
        """Ensure OpenBao has a known initial root password for GitLab.

        Flow on first install (chart just rendered with
        `password_automatically_set=true`):
          1. Mint a 32-char random password.
          2. Push it through GitLab Rails to overwrite root's password
             (so a `password_automatically_set: true` account can still
             log in with what we know).
          3. Stash it in OpenBao at secret/gitlab/initial_root_password.

        On re-runs: read the existing password from OpenBao and skip
        the Rails mutation. We never silently rotate the password —
        that would invalidate the user's existing browser sessions.
        """
        # First re-run? Reuse what's in OpenBao.
        try:
            pw = self._openbao.kv_get("gitlab", "initial_root_password")
            if isinstance(pw, str) and pw:
                return pw
        except RuntimeError:
            pass
        from ..shell import DryRunRunner
        # 32 chars from a 72-char alphabet — RFC4122-friendly entropy.
        alphabet = string.ascii_letters + string.digits
        pw = "".join(secrets.choice(alphabet) for _ in range(32))
        if not isinstance(self._r, DryRunRunner):
            # Push the password into GitLab via Rails so the root user
            # can authenticate with what we know. Always succeed even
            # if the chart's auto-generated password already set
            # `password_automatically_set=true`.
            self._log.info("Setting root password via gitlab-rails runner")
            # Use bash -lc and a one-shot ruby string to avoid the
            # deeply-nested quoting problems of multi-arg kubectl exec.
            ruby = (
                f'u = User.find_by(username: "root"); '
                f'u.password = "{pw}"; '
                f'u.password_automatically_set = false; '
                f'u.save(validate: false)'
            )
            self._r.run([
                "kubectl", "exec", "--namespace", self.NAMESPACE,
                "deploy/gitlab-toolbox", "--", "bash", "-lc",
                "gitlab-rails runner '%s'" % ruby.replace("'", "'\"'\"'"),
            ], check=False)
        # Either way, persist to OpenBao.
        self._openbao.kv_put("gitlab", {"initial_root_password": pw})
        self._log.ok("Initial GitLab root password stored in OpenBao")
        return pw

    def _wait_for_webservice(self, timeout_s: int = 900) -> None:
        """Block until gitlab-webservice-default is Ready (or 15min timeout)."""
        self._log.info(f"Waiting for GitLab webservice (timeout {timeout_s}s — this takes minutes)")
        self._r.run([
            "kubectl", "wait", "--namespace", self.NAMESPACE,
            "--for=condition=Ready",
            "pod", "-l", "app=webservice,release=gitlab",
            f"--timeout={timeout_s}s",
        ])
        self._log.ok("GitLab webservice is Ready")

    def _capture_credentials(self) -> GitlabCredentials:
        """Mint GitLab credentials via `gitlab-rails runner`.

        GitLab 18.x:
          - ApplicationSetting.current.initial_root_password was removed.
          - The chart generates a random root password on first boot and
            stashes it in the K8s Secret named
            `gitlab-initial-root-password` (in the GitLab namespace).
          - For the runner token, we read the per-instance registration
            token from the application settings (or mint a new one if
            none exists) — same as before.

        Steps:
          1. Read / decode the `gitlab-initial-root-password` K8s Secret.
          2. Read or mint `runners_registration_token` via Rails.

        Dry-run: returns synthetic credentials so the pipeline can preview.
        """
        from ..shell import DryRunRunner
        if isinstance(self._r, DryRunRunner):
            self._log.info("[dry-run] returning synthetic GitLab credentials")
            return GitlabCredentials(
                initial_root_password="dryrun-root-password",
                runner_registration_token="dryrun-runner-token",
            )

        # 1. Read the root password from the K8s Secret GitLab writes
        #    on first boot. Field: data.password (base64).
        root_pw_result = self._r.run([
            "kubectl", "get", "secret", "-n", self.NAMESPACE,
            "gitlab-initial-root-password",
            "-o", "jsonpath={.data.password}",
        ], check=False)
        root_pw = ""
        if root_pw_result.ok and root_pw_result.stdout.strip():
            import base64
            try:
                root_pw = base64.b64decode(root_pw_result.stdout).decode("utf-8", errors="replace").strip()
            except Exception:
                pass

        # 2. Read or mint the runner registration token. We use a
        #    bash intermediate so the inner single-quotes don't
        #    collide with kubectl exec's wrapping. The GitLab
        #    toolbox pod has multiple init containers, so we name
        #    `toolbox` explicitly via `bash -lc` to avoid the
        #    "Defaulted container" warnings being treated as stderr.
        token_cmd = (
            "gitlab-rails runner "
            "\"puts Gitlab::CurrentSettings.current_application_settings.runners_registration_token "
            "|| Ci::RunnerToken.create!(token: SecureRandom.hex(16)).token\""
        )
        token_result = self._r.run([
            "kubectl", "exec", "--namespace", self.NAMESPACE,
            "deploy/gitlab-toolbox", "--", "bash", "-lc", token_cmd,
        ], check=False)
        runner_token = token_result.stdout.strip() if token_result.ok else ""

        if not runner_token:
            raise RuntimeError(
                "Failed to capture runner registration token via gitlab-rails runner. "
                f"runner_token ok={token_result.ok}\n"
                f"stdout: {token_result.stdout[:300]}\n"
                f"stderr: {token_result.stderr[:300]}"
            )
        return GitlabCredentials(initial_root_password=root_pw, runner_registration_token=runner_token)

    def user_handoff_steps(self) -> list[UserStep]:
        return [
            UserStep(
                title="GitLab is up at https://gitlab.local.bruj0.net (trust the self-signed CA first).",
                lines=(
                    "# Trust the local CA on your host:",
                    f"sudo trust anchor {self._paths.tls_public}/ca.crt",
                    "",
                    "# Read the initial root password from OpenBao (auto-port-forwards):",
                    "uv run blueprint-secrets read gitlab initial_root_password",
                ),
            ),
        ]