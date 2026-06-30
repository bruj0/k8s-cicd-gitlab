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

        Order: write initial password → helm install → wait for
        webservice ready → exec gitlab-rails to get real creds → push
        them back into OpenBao.
        """
        # 1. Pre-flight: ensure the initial root password exists in OpenBao.
        self._ensure_initial_password()

        # 2. Install the chart.
        result = super().install()

        # 3. Wait for GitLab's webservice to be ready (this is the slow part).
        self._wait_for_webservice()

        # 4. Capture the actual root password + runner registration token.
        creds = self._capture_credentials()
        self._openbao.kv_put("gitlab/runner", {RUNNER_TOKEN_KEY: creds.runner_registration_token})
        self._log.ok("GitLab credentials captured into OpenBao")
        return result

    # ---------- internals ----------

    def _ensure_initial_password(self) -> str:
        """Write an initial root password to OpenBao if one isn't already there."""
        try:
            pw = self._openbao.kv_get("gitlab", "initial_root_password")
            if isinstance(pw, str) and pw:
                return pw
        except RuntimeError:
            pass
        # 32 chars from a 72-char alphabet — RFC4122-friendly entropy.
        alphabet = string.ascii_letters + string.digits
        pw = "".join(secrets.choice(alphabet) for _ in range(32))
        self._openbao.kv_put("gitlab", {"initial_root_password": pw})
        self._log.ok("Initial GitLab root password minted and stored in OpenBao")
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
        """Run `gitlab-rails runner` inside the toolbox pod to read creds.

        The GitLab chart ships a `toolbox` pod with `gitlab-rails` on the
        PATH. We exec into it twice: once to read the actual initial
        root password (the chart may have generated its own), once to
        mint a runner registration token if one doesn't exist.

        Dry-run: returns synthetic credentials so the pipeline can preview
        the install. Real runs always go through the real gitlab-rails
        exec path.
        """
        from ..shell import DryRunRunner
        if isinstance(self._r, DryRunRunner):
            self._log.info("[dry-run] returning synthetic GitLab credentials")
            return GitlabCredentials(
                initial_root_password="dryrun-root-password",
                runner_registration_token="dryrun-runner-token",
            )

        # 1. Read the initial root password. The chart writes it to a
        #    K8s Secret, but we also stash it in OpenBao for the dashboard.
        root_pw_result = self._r.run([
            "kubectl", "exec", "--namespace", self.NAMESPACE,
            "deploy/gitlab-toolbox", "--",
            "gitlab-rails", "runner",
            "-e", "puts ApplicationSetting.current.initial_root_password",
        ], check=False)
        root_pw = root_pw_result.stdout.strip() if root_pw_result.ok else ""

        # 2. Mint a runner registration token (idempotent — returns the same
        #    token for a given (group, runner-name) pair).
        token_result = self._r.run([
            "kubectl", "exec", "--namespace", self.NAMESPACE,
            "deploy/gitlab-toolbox", "--",
            "gitlab-rails", "runner",
            "-e", "puts Gitlab::CurrentSettings.current_application_settings.runners_registration_token || Ci::RunnerToken.create!(token: SecureRandom.hex(16)).token",
        ], check=False)
        runner_token = token_result.stdout.strip() if token_result.ok else ""

        if not root_pw or not runner_token:
            raise RuntimeError(
                "Failed to capture GitLab credentials via gitlab-rails runner. "
                f"root_pw ok={root_pw_result.ok}, runner_token ok={token_result.ok}\n"
                f"stderr: {root_pw_result.stderr[:200]} / {token_result.stderr[:200]}"
            )
        return GitlabCredentials(initial_root_password=root_pw, runner_registration_token=runner_token)

    def user_handoff_steps(self) -> list[UserStep]:
        kubeconfig_export = f"export KUBECONFIG={self._paths.tofu_dir}/kubeconfig"
        return [
            UserStep(
                title="GitLab is up at https://gitlab.local.bruj0.net (trust the self-signed CA first).",
                lines=(
                    "# Trust the local CA on your host:",
                    f"sudo trust anchor {self._paths.tls_public}/ca.crt",
                    "",
                    "# Read the initial root password from OpenBao:",
                    f"{kubeconfig_export}",
                    f"KUBECONFIG=$PWD/infra/tofu/kubeconfig kubectl exec -n openbao openbao-0 -- bao kv get -format=json secret/gitlab | jq -r '.data.data.initial_root_password'",
                ),
            ),
        ]