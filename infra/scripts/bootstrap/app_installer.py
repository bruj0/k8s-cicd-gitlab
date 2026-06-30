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
    # Absolute paths to helm values files, applied in order. Phase 2 uses
    # this so install-time config lives in committed YAML references.
    values_files: tuple[str, ...] = ()


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

    def is_already_deployed(self) -> bool:
        """Probe whether the release is already deployed in the cluster.

        Default implementation calls `helm list` filtered by namespace +
        release. Subclasses can override with a cheaper probe (e.g. an
        existing `kubectl get deployment`) if they have one.

        Used by Phase 2's pipeline for idempotency: if a release is
        already deployed, we skip the `helm upgrade --install` but still
        re-cache the chart and re-emit the install command (so re-runs
        are safe and report what they did).
        """
        cmd = ["helm", "list", "--namespace", self._spec.namespace,
               "--filter", self._spec.release,
               "--output", "json"]
        try:
            result = self._r.run(cmd, check=True)
        except Exception:
            return False
        if not result.stdout.strip():
            return False
        # helm list --output json returns "[]" or "[{...}]"
        try:
            import json as _json
            rows = _json.loads(result.stdout)
        except Exception:
            return False
        return any(row.get("name") == self._spec.release for row in rows)

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

    def install(self) -> AppPrepResult:
        """Cache the chart, then ACTUALLY install it via `helm upgrade --install`.

        This is the Phase 2 entrypoint. Phase 1's `bootstrap.py` does NOT
        call this — it sticks to `prepare()` and prints the command.

        Idempotent: `helm upgrade --install` is a no-op when the release
        is at the same version. If you want to skip work entirely on
        re-runs, use `is_already_deployed()` first.
        """
        result = self.prepare()
        # Ensure the repo is registered locally (idempotent: helm errors if
        # the repo doesn't exist). The chart is cached at infra/helm-charts/<name>.tgz
        # — the helm call uses that local path, not the repo, but the repo
        # must be added once for `helm pull` to work the first time.
        # Vendored charts (no `url`) skip this.
        cfg = helm_repo(self._spec.repo_key)
        if not self._is_vendored(cfg):
            self._ensure_repo(cfg["name"], cfg["url"])

        # helm reads the cached .tgz directly when we pass an absolute path,
        # so we don't need network access on subsequent runs.
        argv = self._argv_for_install(result)
        self._r.run(argv)
        self._log.ok(f"Installed {self._spec.release} into namespace {self._spec.namespace}")
        return result

    def _is_vendored(self, cfg: dict) -> bool:
        """True if the chart is sourced from a local path (no helm repo)."""
        return cfg.get("_source") == "vendored"

    def user_handoff_steps(self) -> list[UserStep]:
        """Default: no extra user steps after install.

        Override in subclasses that need URL discovery / token mint /
        CA export etc. (e.g. HeadlampInstaller, OpenBaoInstaller).
        """
        return []

    def _ensure_repo(self, repo_name: str, repo_url: str) -> None:
        """Add the helm repo if not already present (idempotent)."""
        list_result = self._r.run(["helm", "repo", "list", "--output", "json"], check=False)
        if list_result.ok and repo_name in list_result.stdout:
            return
        self._r.run(["helm", "repo", "add", repo_name, repo_url])
        self._r.run(["helm", "repo", "update", repo_name])

    def _build_result(self, chart: CachedChart) -> AppPrepResult:
        """Build the AppPrepResult for a chart.

        Used by prepare() (real chart) and fake_prepare() (no I/O cheat).
        Returns the AppPrepResult with both the human-readable cmd string
        (for `--user` mode and logs) and the argv list embedded so
        install() can re-derive the same command without re-deriving
        set_args etc.
        """
        cfg = helm_repo(self._spec.repo_key)
        set_args: list[str] = []
        for k, v in _flatten(cfg.get("values_overrides", {})):
            set_args += ["--set", f"{k}={v}"]
        for k, v in self._spec.extra_set:
            set_args += ["--set", f"{k}={v}"]

        # argv form: what install() will pass to subprocess.run.
        argv: list[str] = [
            "helm", "upgrade", "--install", self._spec.release, str(chart.path),
        ]
        for vf in self._spec.values_files:
            argv += ["--values", vf]
        if self._spec.create_namespace:
            argv += ["--namespace", self._spec.namespace, "--create-namespace"]
        else:
            argv += ["--namespace", self._spec.namespace]
        if self._spec.wait:
            argv.append("--wait")
        argv += set_args

        # Human-readable form: same argv but quoted for shell echo.
        cmd = " ".join(argv)

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

    def _argv_for_install(self, result: AppPrepResult) -> list[str]:
        """Build the argv list for `helm upgrade --install` from an AppPrepResult.

        This mirrors _build_result's cmd construction but with values_files
        expanded into --values flags. We split the call so install() can
        reuse the same command-construction logic as prepare().
        """
        cfg = helm_repo(self._spec.repo_key)
        set_args: list[str] = []
        for k, v in _flatten(cfg.get("values_overrides", {})):
            set_args += ["--set", f"{k}={v}"]
        for k, v in self._spec.extra_set:
            set_args += ["--set", f"{k}={v}"]

        argv: list[str] = [
            "helm", "upgrade", "--install", self._spec.release, str(result.chart_path),
        ]
        for vf in self._spec.values_files:
            argv += ["--values", vf]
        if self._spec.create_namespace:
            argv += ["--namespace", self._spec.namespace, "--create-namespace"]
        else:
            argv += ["--namespace", self._spec.namespace]
        if self._spec.wait:
            argv.append("--wait")
        argv += set_args
        return argv


# ---------- factory ----------


def installer_for(
    repo_key: str,
    runner: CommandRunner,
    paths: Paths,
    cache: HelmChartCache,
    log: Logger,
) -> HelmAppInstaller:
    """Build the right installer for a repo_key.

    Each branch picks the subclass that has the right overrides for that
    app (URL discovery, secret bootstrap, registration-token fetch, etc.).
    Callers (`app.py`, `phase2/pipeline.py`) never special-case the app
    name; the factory is the single place where repo_key → class lives.
    """
    if repo_key == "headlamp":
        return HeadlampInstaller(runner, paths, cache, log)
    if repo_key == "traefik":
        return TraefikInstaller(runner, paths, cache, log)
    if repo_key == "openbao":
        return OpenBaoInstaller(runner, paths, cache, log)
    if repo_key == "gitlab":
        return GitlabInstaller(runner, paths, cache, log)
    if repo_key == "gitlab-runner":
        return GitLabRunnerInstaller(runner, paths, cache, log)
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