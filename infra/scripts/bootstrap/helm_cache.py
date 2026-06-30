"""Local helm chart cache (idempotent).

Why: when kind or Phase 2 needs an offline-ish install path, we want the
chart on disk under `infra/helm-charts/cache/<name>-<version>.tgz`. If
the requested chart version matches what's on disk, we skip the
download. Otherwise we run `helm repo add` + `helm pull`.

This class owns the disk format; `HeadlampInstaller` and Phase 2
charts ask it for a path, never for a URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .logger import Logger
from .paths import Paths
from .shell import CommandRunner
from .versions import helm_repo


_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class CachedChart:
    name: str
    version: str
    path: Path

    @property
    def filename(self) -> str:
        return self.path.name


class HelmChartCache:
    """Pulls helm charts into `infra/helm-charts/cache/` and remembers them."""

    def __init__(self, runner: CommandRunner, paths: Paths, log: Logger) -> None:
        self._r = runner
        self._paths = paths
        self._log = log
        paths.ensure_dirs()

    def _filename(self, name: str, version: str) -> str:
        if not _SAFE_NAME.match(name):
            raise ValueError(f"Unsafe chart name: {name!r}")
        return f"{name}-{version}.tgz"

    def ensure(self, repo_key: str) -> CachedChart:
        """Return a local path to <name>-<version>.tgz, downloading only if needed."""
        cfg = helm_repo(repo_key)
        name = cfg["chart"]
        version = cfg["chart_version"]
        repo_url = cfg["url"]
        repo_name = cfg["name"]
        target = self._paths.helm_charts_dir / self._filename(name, version)

        if target.exists():
            self._log.ok(f"chart cached: {target.name}")
            return CachedChart(name, version, target)

        # Ensure the repo is registered. `helm repo add` is idempotent.
        self._r.run(["helm", "repo", "add", repo_name, repo_url])
        self._r.run(["helm", "repo", "update", repo_name])
        self._log.info(f"Pulling chart {name}@{version} from {repo_url}")
        # --untar=false ensures we keep the .tgz. --destination is the cache dir.
        self._r.run([
            "helm", "pull", repo_name + "/" + name,
            "--version", version,
            "--destination", str(self._paths.helm_charts_dir),
        ])
        if not target.exists():
            # `helm pull` writes <filename>.tgz; some chart versions use a different
            # suffix. Fall back: take the newest .tgz in the charts dir.
            candidates = sorted(self._paths.helm_charts_dir.glob(f"{name}-*.tgz"), key=lambda p: p.stat().st_mtime)
            if not candidates:
                raise RuntimeError(f"helm pull succeeded but {name} tgz not found in {self._paths.helm_charts_dir}")
            return CachedChart(name, version, candidates[-1])
        return CachedChart(name, version, target)