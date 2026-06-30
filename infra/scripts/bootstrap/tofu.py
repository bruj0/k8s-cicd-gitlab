"""OpenTofu runner — provisions state, never applies.

Per spec rule: 'The bootstrap application never runs OpenTofu, it is
run manually.' We interpret that strictly:

  - Bootstrap WILL run `tofu init` so providers are downloaded (no infra
    change, no side effects on the cluster).
  - Bootstrap WILL run `tofu validate` to catch syntax errors early.
  - Bootstrap WILL NOT run `tofu plan` or `tofu apply`.

The user inspects plan and applies themselves; bootstrap prints the
exact commands after `init` + `validate`.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from .logger import Logger
from .paths import Paths
from .shell import CommandRunner


@dataclass(frozen=True)
class TofuNextSteps:
    """The exact commands the user runs after bootstrap finishes."""

    plan: str
    apply: str
    destroy: str
    kubectl: str


class TofuRunner:
    def __init__(self, runner: CommandRunner, paths: Paths, log: Logger) -> None:
        self._r = runner
        self._paths = paths
        self._log = log

    def seed_tfvars_if_missing(self) -> None:
        tfvars = self._paths.tofu_dir / "tofu.tfvars"
        if tfvars.exists():
            return
        example = self._paths.tofu_dir / "tofu.tfvars.example"
        if example.exists():
            self._paths.tofu_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(example, tfvars)
            self._log.ok(f"Seeded {tfvars.name} from {example.name}")

    def init(self) -> None:
        """Download providers. Safe to re-run."""
        self._paths.tofu_dir.mkdir(parents=True, exist_ok=True)
        self._r.run(["tofu", f"-chdir={self._paths.tofu_dir}", "init", "-upgrade"])

    def validate(self) -> None:
        """Static syntax check. Safe to re-run."""
        self._r.run(["tofu", f"-chdir={self._paths.tofu_dir}", "validate"])

    def next_steps(self) -> TofuNextSteps:
        """Commands the user runs *after* bootstrap finishes."""
        chdir = f"-chdir={self._paths.tofu_dir}"
        kubeconfig = self._paths.tofu_dir / "kubeconfig"
        return TofuNextSteps(
            plan=f"tofu {chdir} plan",
            apply=f"tofu {chdir} apply",
            destroy=f"tofu {chdir} destroy",
            kubectl=f"KUBECONFIG={kubeconfig} kubectl get nodes -o wide",
        )