"""GitLab Runner (Kubernetes executor).

The runner registers against the GitLab instance using a registration
token. The token is written into OpenBao by `gitlab.py` after the GitLab
chart finishes its post-install setup, so the runner installer must run
AFTER the gitlab installer.

Each install fetches the current registration token from OpenBao and
applies it via `--set runnerToken=...`. If you rotate the token in
GitLab's UI, re-run `--phase 2` to pick up the new one.
"""

from __future__ import annotations

from ..app_installer import HelmAppInstaller, HelmAppSpec, HelmChartCache, UserStep
from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner
from .secrets import OpenBaoClient


class GitLabRunnerInstaller(HelmAppInstaller):
    """GitLab Runner with Kubernetes executor."""

    NAMESPACE = "gitlab-runner"
    RELEASE = "gitlab-runner"
    REPO_KEY = "gitlab-runner"

    def __init__(self, runner: CommandRunner, paths: Paths, cache: HelmChartCache, log: Logger,
                 openbao: OpenBaoClient) -> None:
        super().__init__(
            runner, paths, cache, log,
            HelmAppSpec(
                repo_key=self.REPO_KEY,
                release=self.RELEASE,
                namespace=self.NAMESPACE,
                wait=False,  # Runner uses a Deployment, not StatefulSet; --wait is OK
                             # but slower. Skip it for the iteration loop.
                values_files=(
                    str(paths.phase2_refs_dir / "helm-values-runner.yaml"),
                ),
            ),
        )
        self._openbao = openbao

    # ---------- lifecycle ----------

    def install(self):
        """Install + bind the registration token from OpenBao.

        The token is fetched fresh on every install so re-runs after a
        manual token rotation in GitLab's UI just work.
        """
        token = self._openbao.fetch_gitlab_runner_registration_token()
        # Build the install command with --set runnerToken=...
        # We do this by mutating extra_set in a copy of the spec — the
        # parent's install() method rebuilds the command from spec.
        spec_with_token = HelmAppSpec(
            repo_key=self._spec.repo_key,
            release=self._spec.release,
            namespace=self._spec.namespace,
            wait=self._spec.wait,
            create_namespace=self._spec.create_namespace,
            extra_set=self._spec.extra_set + (("runnerToken", token),),
            values_files=self._spec.values_files,
        )
        # Temporarily swap spec, install, then restore.
        original = self._spec
        self._spec = spec_with_token
        try:
            result = super().install()
        finally:
            self._spec = original
        return result

    def user_handoff_steps(self) -> list[UserStep]:
        return [
            UserStep(
                title="Verify the runner shows up in GitLab's UI:",
                lines=(
                    "# In GitLab: Admin Area → CI/CD → Runners",
                    "# OR via API (after auth):",
                    f"export KUBECONFIG={self._paths.tofu_dir}/kubeconfig",
                    "kubectl exec -n gitlab deploy/gitlab-toolbox -- gitlab-rails runner 'puts Ci::Runner.all.map(&:description)'",
                ),
            ),
        ]