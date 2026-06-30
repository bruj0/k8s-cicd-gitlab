"""Generic helm-app installer — bootstrap caches, user installs.

Per spec rule: 'The bootstrap application checks the system and
provisions all the configuration so a person can run it.' We interpret
that as: bootstrap prepares everything (chart on disk, exact `helm
upgrade --install` command, follow-up commands the user needs) but does
NOT run helm or kubectl itself.

The user (or the .gitlab-ci.yml in Phase 3) executes the printed commands.

This module exposes one generic abstraction (`HelmAppInstaller`) plus
per-app subclasses. The generic class is driven by a declarative
`HelmAppSpec` so that future phases can add a `GitlabInstaller`,
`TraefikInstaller`, etc. without copy-pasting the chart-cache + helm
command-assembly logic.

Public surface (in order of likely reuse):

    HelmAppSpec        declarative per-app config (repo_key, namespace, ...)
    UserStep           one entry in the user handoff (title + command lines)
    AppPrepResult      everything app.py needs to print a handoff for one app
    HelmAppInstaller   the worker — caches the chart, builds the install command
    installer_for()    factory that reads VERSIONS.json and returns the right installer
    HeadlampInstaller  Headlamp-specific subclass (overrides user_handoff_steps)
"""

from __future__ import annotations

from dataclasses import dataclass

from .helm_cache import CachedChart, HelmChartCache
from .logger import Logger
from .paths import Paths
from .shell import CommandRunner
from .versions import helm_repo


# ---------- declarative types ----------


@dataclass(frozen=True)
class HelmAppSpec:
    """Everything `HelmAppInstaller` needs to know about one app.

    `repo_key` looks up the chart URL, name, version, and `values_overrides`
    in `VERSIONS.json` under `helm_repositories`. Everything else is app-local.
    """

    repo_key: str
    release: str
    namespace: str
    wait: bool = True
    create_namespace: bool = True
    # Extra `helm --set foo=bar` overrides that are NOT in VERSIONS.json
    # (e.g. service.type=NodePort for Headlamp). These are appended after
    # the ones declared in VERSIONS so callers can override without editing JSON.
    extra_set: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class UserStep:
    """One entry in the user-handoff block.

    `title` is the one-liner printed above the command (`Step 5/6  ...`).
    `lines` is the list of shell lines the user runs verbatim, joined with
    backslash-continuations when displayed.
    """

    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class AppPrepResult:
    """Everything the composition root needs to print a handoff for one app."""

    chart_path: str
    namespace: str
    release: str
    helm_command: str           # the exact `helm upgrade --install` command
    extra_user_steps: tuple[UserStep, ...] = ()   # follow-up commands (URL, token, ...)


# ---------- the worker ----------


class HelmAppInstaller:
    """Caches a chart and assembles the install command the user runs.

    Does NOT call `helm install` itself — that would violate the spec
    rule that bootstrap never runs the deployment tools.
    """

    def __init__(
        self,
        runner: CommandRunner,
        paths: Paths,
        cache: HelmChartCache,
        log: Logger,
        spec: HelmAppSpec,
    ) -> None:
        self._r = runner
        self._paths = paths
        self._cache = cache
        self._log = log
        self._spec = spec

    @property
    def spec(self) -> HelmAppSpec:
        return self._spec

    @property
    def repo_key(self) -> str:
        return self._spec.repo_key

    # ---------- idempotency probe ----------

    def is_already_cached(self) -> bool:
        cfg = helm_repo(self._spec.repo_key)
        target = self._paths.helm_charts_dir / f"{cfg['chart']}-{cfg['chart_version']}.tgz"
        return target.exists()

    # ---------- main entrypoint ----------

    def prepare(self) -> AppPrepResult:
        """Cache the chart and return the install command for the user.

        Side effects: downloads the chart (if not cached) via HelmChartCache,
        and logs the cached path + the helm command. The user runs the command
        themselves; bootstrap never does.
        """
        chart = self._cache.ensure(self._spec.repo_key)
        return self._build_result(chart)

    def fake_prepare(self) -> AppPrepResult:
        """Return an AppPrepResult WITHOUT doing any I/O.

        Used by `bootstrap --user` (cheat-sheet mode) where we want to show
        the handoff commands without paying for the chart download. The
        chart path is constructed from `paths.helm_charts_dir` exactly the
        way `HelmChartCache` would, so it is byte-identical to a real run
        once the chart is downloaded.
        """
        cfg = helm_repo(self._spec.repo_key)
        fake_path = self._paths.helm_charts_dir / f"{cfg['chart']}-{cfg['chart_version']}.tgz"
        fake_chart = CachedChart(name=cfg["chart"], version=cfg["chart_version"], path=fake_path)
        return self._build_result(fake_chart)

    # ---------- extension hooks (override in subclasses) ----------

    def user_handoff_steps(self) -> list[UserStep]:
        """Override to inject app-specific follow-up commands.

        The default is empty — generic helm apps don't need anything beyond
        the install command. Headlamp overrides this to add the URL-discovery
        and token-mint steps. A future GitlabInstaller would override it
        with the URL + initial-root-password steps.
        """
        return []

    # ---------- internals ----------

    def _build_result(self, chart: CachedChart) -> AppPrepResult:
        cfg = helm_repo(self._spec.repo_key)
        set_args: list[str] = []
        for k, v in _flatten(cfg.get("values_overrides", {})):
            set_args += ["--set", f"{k}={v}"]
        for k, v in self._spec.extra_set:
            set_args += ["--set", f"{k}={v}"]

        cmd_parts: list[str] = [
            "helm", "upgrade", "--install", self._spec.release, f"'{chart.path}'",
        ]
        if self._spec.create_namespace:
            cmd_parts += ["--namespace", self._spec.namespace, "--create-namespace"]
        else:
            cmd_parts += ["--namespace", self._spec.namespace]
        if self._spec.wait:
            cmd_parts.append("--wait")
        cmd_parts += set_args
        cmd = " ".join(cmd_parts)

        self._log.ok(f"{self._spec.release} chart cached at: {chart.path}")
        self._log.info(f"To install {self._spec.release} into the cluster, run manually:")
        self._log.info(f"  KUBECONFIG={self._paths.tofu_dir}/kubeconfig \\")
        self._log.info(f"  {cmd}")
        return AppPrepResult(
            chart_path=str(chart.path),
            namespace=self._spec.namespace,
            release=self._spec.release,
            helm_command=cmd,
            extra_user_steps=tuple(self.user_handoff_steps()),
        )


# ---------- factory ----------


def installer_for(
    repo_key: str,
    runner: CommandRunner,
    paths: Paths,
    cache: HelmChartCache,
    log: Logger,
) -> HelmAppInstaller:
    """Build the right installer for a repo_key.

    Today only Headlamp is supported. Future keys (e.g. `gitlab`,
    `traefik`) are matched here — callers (`app.py`) never special-case
    the app name.
    """
    if repo_key == "headlamp":
        return HeadlampInstaller(runner, paths, cache, log)
    # Fallback: generic installer with sensible defaults. Subclasses can
    # add more cases above this line as they ship.
    return HelmAppInstaller(
        runner, paths, cache, log,
        HelmAppSpec(
            repo_key=repo_key,
            release=helm_repo(repo_key)["chart"],
            namespace=helm_repo(repo_key)["chart"],
        ),
    )


# ---------- Headlamp subclass ----------


class HeadlampInstaller(HelmAppInstaller):
    """Headlamp-specific behaviour: NodePort service + URL/token follow-ups.

    Subclasses `HelmAppInstaller` so existing call sites
    (`self.headlamp.prepare()` in `app.py`) keep working unchanged.
    """

    NAMESPACE = "headlamp"
    RELEASE = "headlamp"
    REPO_KEY = "headlamp"

    def __init__(self, runner: CommandRunner, paths: Paths, cache: HelmChartCache, log: Logger) -> None:
        super().__init__(
            runner, paths, cache, log,
            HelmAppSpec(
                repo_key=self.REPO_KEY,
                release=self.RELEASE,
                namespace=self.NAMESPACE,
                # Note: service.type=NodePort is already declared in
                # VERSIONS.json under `helm_repositories.headlamp.values_overrides`,
                # so we do NOT duplicate it here via `extra_set`. That field is
                # reserved for runtime/programmatic overrides not in JSON.
            ),
        )

    def user_handoff_steps(self) -> list[UserStep]:
        """URL discovery + token mint — the two extra steps the README documents."""
        kubeconfig_export = f"export KUBECONFIG={self._paths.tofu_dir}/kubeconfig"
        return [
            UserStep(
                title=(
                    f"Discover the {self.RELEASE} URL "
                    f"(prints http://$NODE_IP:$NODE_PORT on the host):"
                ),
                lines=(
                    kubeconfig_export,
                    "NODE_PORT=$(kubectl get --namespace headlamp "
                    "-o jsonpath=\"{.spec.ports[0].nodePort}\" services headlamp)",
                    "NODE_IP=$(kubectl   get nodes     --namespace headlamp "
                    "-o jsonpath=\"{.items[0].status.addresses[0].address}\")",
                    "echo \"http://$NODE_IP:$NODE_PORT\"",
                ),
            ),
            UserStep(
                title=(
                    f"Mint a {self.RELEASE} login token "
                    f"(paste it into the dashboard's token login form):"
                ),
                lines=(
                    f"{kubeconfig_export} \\",
                    "kubectl create token headlamp --namespace headlamp",
                ),
            ),
        ]


# ---------- helpers ----------


def _flatten(d: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested dicts into dot-paths so helm --set accepts them."""
    out: list[tuple[str, str]] = []
    for k, v in d.items():
        path = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        if isinstance(v, dict):
            out += _flatten(v, path)
        else:
            out.append((path, str(v)))
    return out