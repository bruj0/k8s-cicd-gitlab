"""Composition root for the bootstrap package.

Phases:
  1. Bootstrap checks the host for required tools, provisions the working
     tree (tfvars, PKI, helm chart cache), and PRINTS the next commands.
     It never invokes `tofu apply`. (See `spec.md` rule: "The bootstrap
     application checks the system and provisions all the configuration
     so a person can run it.")

  2. Bootstrap ACTUALLY installs Phase 2 (Traefik + OpenBao + GitLab +
     Runner + Gateway API manifests). The spec rule from Phase 1 was
     scoped to OpenTofu; helm/kubectl ARE applied by the bootstrap so
     iteration can test results. Every step is idempotent so re-runs are
     safe.

Run vs read at a glance:

    [bootstrap]  checks host prereqs, installs missing tools, mints PKI,
                 seeds tfvars, runs `tofu init` + `tofu validate`,
                 caches the Headlamp chart, then STOPS (Phase 1).
    [user]       inspects `tofu plan`, runs `tofu apply`, runs
                 `kubectl get nodes`, runs `helm install` for Headlamp
                 (Phase 1's user handoff).
    [bootstrap]  installs Traefik + OpenBao + GitLab + Runner + Gateway
                 in order, with idempotency probes + retry-safe steps
                 (Phase 2).

Every line printed by this app is prefixed `[bootstrap]` or `[user]`
so the boundary between the two is unambiguous in the terminal.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from .app_installer import HeadlampInstaller
from .helm_cache import HelmChartCache
from .installer import installer_for
from .logger import ConsoleLogger, Logger
from .os_detect import detect_os, is_supported
from .paths import Paths
from .phase2 import (
    GatewayApplier, OpenBaoClient, Phase2Installers, Phase2Pipeline,
    WildcardCertInstaller,
)
from .pki import PkiRunner
from .prereq import PrereqRegistry
from .shell import CommandRunner, DryRunRunner, SubprocessRunner
from .tofu import TofuRunner
from .versions import load_versions

# Visual tags. Keeping them as module constants means future phases can
# reuse them without re-typing the string literal everywhere.
TAG_BOOTSTRAP = "[bootstrap]"
TAG_USER = "[user]"


@dataclass(frozen=True)
class CliArgs:
    phase: int
    domain: str
    check: bool
    skip_install: bool
    dry_run: bool
    user: bool


def parse_args(argv: list[str] | None = None) -> CliArgs:
    ap = argparse.ArgumentParser(
        description=(
            "Blueprint bootstrap. Phase 1 prepares the working tree (prereqs, "
            "PKI, tfvars, helm chart cache) and prints the next commands. "
            "Phase 2 installs GitLab + Runner + OpenBao + Traefik end-to-end. "
            "Per spec, Phase 1 bootstrap never runs OpenTofu."
        )
    )
    ap.add_argument(
        "--phase", type=int, default=1,
        help="Which phase to run. 1 = prepare kind cluster (default). "
             "2 = install GitLab stack end-to-end.",
    )
    ap.add_argument("--domain", default="local.bruj0.net", help="Base DNS domain for the cluster.")
    ap.add_argument("--check", action="store_true", help="Only check prereqs; do not install or provision.")
    ap.add_argument("--skip-install", action="store_true", help="Assume prereqs are present; only check.")
    ap.add_argument("--dry-run", action="store_true", help="Log every command without executing it.")
    ap.add_argument(
        "--user",
        action="store_true",
        help=(
            "Only print the Phase-1 user-handoff block: the commands YOU "
            "must run after bootstrap finishes (tofu plan / apply, kubectl "
            "verify, helm install, discover Headlamp URL, mint login "
            "token). Useful as a cheat sheet. (Phase 1 only.)"
        ),
    )
    ns = ap.parse_args(argv)
    return CliArgs(**vars(ns))


class BootstrapApp:
    """Wires every helper class and runs the Phase 1 preparation pipeline.

    Lifecycle (printed in order to the terminal):

        [bootstrap]  Step 1/4  Install missing host prereqs
        [bootstrap]  Step 2/4  Mint local CA + wildcard cert
        [bootstrap]  Step 3/4  `tofu init` + `tofu validate` (no apply)
        [bootstrap]  Step 4/4  Cache Headlamp chart (no install)
        [user]       Step 1/6  Inspect `tofu plan` (read it carefully)
        [user]       Step 2/6  Run `tofu apply` (this provisions the cluster)
        [user]       Step 3/6  Verify 5 nodes are Ready with `kubectl get nodes`
        [user]       Step 4/6  Install Headlamp via `helm upgrade --install`
        [user]       Step 5/6  Discover the Headlamp URL (NODE_PORT + NODE_IP)
        [user]       Step 6/6  Mint a Headlamp login token with `kubectl create token`

    The split between Step 1-4 (the app does it) and Step 1-6 (you do it)
    is the spec rule made visible. Re-running is safe and idempotent.

    The `--user` flag short-circuits the pipeline and only prints the
    user-handoff block — useful as a cheat sheet after the prep is done.

    Phase 2 mode (`--phase 2`) wires a different pipeline (`Phase2Pipeline`)
    that ACTUALLY installs the GitLab stack. Every step has its own
    idempotency probe, so re-runs are safe and converge on a working
    install after iteration.
    """

    def __init__(self, paths: Paths, args: CliArgs, log: Logger, runner: CommandRunner) -> None:
        self.paths = paths
        self.args = args
        self.log = log
        self.runner = runner

        # Phase 1 collaborators.
        self.prereqs = PrereqRegistry.default(runner)
        self.pki = PkiRunner(runner, paths)
        self.tofu = TofuRunner(runner, paths, log)
        self.chart_cache = HelmChartCache(runner, paths, log)
        self.headlamp = HeadlampInstaller(runner, paths, self.chart_cache, log)

        # Phase 2 collaborators. We construct them eagerly so a misconfig
        # (e.g. missing references YAML) surfaces at boot, not at install.
        # Note: we build the Phase 2 installers directly rather than via
        # `installer_for()` because GitLab + Runner need an OpenBaoClient
        # injected, which `installer_for()` doesn't know about.
        from .phase2.gitlab import GitlabInstaller
        from .phase2.openbao import OpenBaoInstaller
        from .phase2.runner import GitLabRunnerInstaller
        from .phase2.traefik import TraefikInstaller

        self._phase2_openbao_client = OpenBaoClient(runner, paths, log)
        self._phase2_cert = WildcardCertInstaller(runner, paths, log)
        self._phase2_gateway = GatewayApplier(runner, paths, log)
        self._phase2_installers = Phase2Installers(
            cert=self._phase2_cert,
            traefik=TraefikInstaller(runner, paths, self.chart_cache, log),
            openbao=OpenBaoInstaller(runner, paths, self.chart_cache, log),
            gitlab=GitlabInstaller(runner, paths, self.chart_cache, log, self._phase2_openbao_client),
            runner=GitLabRunnerInstaller(runner, paths, self.chart_cache, log, self._phase2_openbao_client),
        )
        self.phase2 = Phase2Pipeline(
            paths=paths, runner=runner, log=log,
            installers=self._phase2_installers,
            gateway=self._phase2_gateway,
            cert=self._phase2_cert,
        )

    @classmethod
    def from_argv(cls, bootstrap_dir, argv: list[str] | None = None) -> "BootstrapApp":
        args = parse_args(argv)
        load_versions(bootstrap_dir / "VERSIONS.json")
        paths = Paths.from_bootstrap_dir(bootstrap_dir)
        log = ConsoleLogger() if not args.dry_run else ConsoleLogger(color=False)
        runner: CommandRunner = DryRunRunner(log) if args.dry_run else SubprocessRunner(log)
        return cls(paths, args, log, runner)

    # ---------- logging helpers (prefix every line with the actor tag) ----------

    def _app(self, msg: str) -> None:
        self.log.info(f"{TAG_BOOTSTRAP} {msg}")

    def _app_err(self, msg: str) -> None:
        self.log.err(f"{TAG_BOOTSTRAP} {msg}")

    def _you(self, msg: str) -> None:
        self.log.info(f"{TAG_USER} {msg}")

    @staticmethod
    def _describe_mode(args: CliArgs) -> str:
        """Human-readable summary of which flags the user passed.

        Order matters: the most specific combination wins. `--user` overrides
        everything else (it's a print-only mode), then `--check` and
        `--skip-install` are mutually compatible but the former implies the
        latter. `dry-run` is reported last because it composes with all of
        the above.
        """
        if args.user:
            base = "user cheat sheet only (--user)"
        elif args.check:
            base = "check-only (--check)"
        elif args.skip_install:
            base = "prep without installing prereqs (--skip-install)"
        else:
            base = "full prep"
        return f"{base}, dry-run" if args.dry_run else base

    def _banner(self, title: str) -> None:
        self.log.info("")
        self.log.info("=" * 72)
        self.log.info(f"  {title}")
        self.log.info("=" * 72)

    # ---------- main pipeline ----------

    def run(self) -> int:
        args = self.args

        if args.phase == 1:
            return self._run_phase1()
        if args.phase == 2:
            return self._run_phase2()
        self._app_err(f"Phase {args.phase} is not implemented yet.")
        return 2

    def _run_phase1(self) -> int:
        """The original Phase 1 preparation pipeline (pre-check + print handoff)."""
        args = self.args
        family, distro = detect_os()

        self._banner("Phase 1 bootstrap — preparation only (no `tofu apply`)")
        self._app(f"Blueprint root: {self.paths.blueprint_dir}")
        self._app(f"OS family:      {family}{f' ({distro})' if distro else ''}")
        mode = self._describe_mode(args)
        self._app(f"Mode:           {mode}")

        if args.user:
            self.log.info("")
            self._app("--user: skipping all prep, only printing the handoff commands.")
        else:
            self._app("This app will:")
            self._app("    1. check / install host prereqs (docker, kubectl, kind, helm, tofu, openssl)")
            self._app("    2. mint a local CA + wildcard cert under infra/tls/")
            self._app("    3. run `tofu init` + `tofu validate` (NOT `tofu apply`)")
            self._app("    4. cache the Headlamp chart under infra/helm-charts/")
            self._app("    5. print the commands YOU then run manually")
        self.log.info("")

        if not is_supported(family) and not (args.check or args.skip_install) and not args.user:
            self._app_err(f"Unsupported OS family: {family!r}. Install tools manually and re-run with --skip-install.")
            return 1

        if args.user:
            self._banner("[user] Handoff cheat sheet (no prep was performed)")
            self._print_user_handoff_only()
            return 0

        # Step 0: prereq report (always)
        self._banner("[bootstrap] Prereq report")
        report = self.prereqs.report()
        self._print_report(report)

        if args.check or args.skip_install:
            ok = self.prereqs.all_ok(report) and self.prereqs.daemon_ok()
            self.log.info("")
            self._app(("All prereqs present." if ok else "Some prereqs missing — install before continuing."))
            return 0 if ok else 1

        # Step 1: install any missing prereqs
        self._banner("[bootstrap] Step 1/4  Install any missing host prereqs")
        installer = installer_for(family, self.runner)
        report = self.prereqs.ensure_all(installer)
        self._print_report(report)
        if not self.prereqs.daemon_ok():
            self._app_err("Docker daemon unreachable. Start it (e.g. `sudo systemctl start docker`) and re-run.")
            return 1

        # Step 2: PKI
        self._banner("[bootstrap] Step 2/4  Mint local CA + wildcard cert")
        self.pki.ensure(args.domain)

        # Step 3: tofu init + validate (downloads providers, checks syntax).
        # Spec rule: bootstrap never runs `tofu apply`.
        self._banner("[bootstrap] Step 3/4  Initialise + validate OpenTofu (no apply)")
        self.tofu.seed_tfvars_if_missing()
        self.tofu.init()
        self.tofu.validate()

        # Step 4: cache the Headlamp chart (no install; user runs helm).
        self._banner("[bootstrap] Step 4/4  Cache Headlamp helm chart (no install)")
        headlamp = self.headlamp.prepare()

        # Step 5: hand off to the user.
        self._print_user_handoff(headlamp)
        return 0

    def _run_phase2(self) -> int:
        """Phase 2: install GitLab stack end-to-end.

        Different lifecycle from Phase 1: there's no user handoff, just a
        final "Phase 2 install complete" with URLs and next commands.
        Every step has an idempotency probe, so re-runs are safe.
        """
        args = self.args
        family, distro = detect_os()

        self._banner("Phase 2 bootstrap — install GitLab + Runner + OpenBao + Traefik")
        self._app(f"Blueprint root: {self.paths.blueprint_dir}")
        self._app(f"OS family:      {family}{f' ({distro})' if distro else ''}")
        mode = self._describe_mode(args)
        self._app(f"Mode:           {mode}")

        if args.check:
            self._app("--check: pre-flight only (no installs, no chart downloads)")
        else:
            self._app("This will:")
            self._app("    1. pre-flight (cluster reachable, helm installed)")
            self._app("    2. publish the Phase-1 wildcard TLS cert into gitlab+openbao namespaces")
            self._app("    3. install Traefik with Gateway API CRDs")
            self._app("    4. install + initialise + unseal OpenBao")
            self._app("    5. apply Gateway + HTTPRoute manifests")
            self._app("    6. install GitLab (min config) + capture initial creds into OpenBao")
            self._app("    7. install GitLab Runner (registers against gitlab.local.bruj0.net)")
        self.log.info("")

        if args.user:
            self._app_err("--user is Phase-1-only; ignored in Phase 2.")
            return 2

        if args.check:
            # Pre-flight only — defer to the pipeline's pre-flight step.
            try:
                self.phase2._step_preflight()
                self._app("Pre-flight OK.")
                return 0
            except Exception as e:
                self._app_err(f"Pre-flight failed: {e}")
                return 1

        return self.phase2.run()

    def _print_user_handoff_only(self) -> None:
        """Cheat-sheet variant: prints the handoff using canned commands.

        Used when the user invokes `--user` without running prep first. We
        cannot call `self.headlamp.prepare()` here (that downloads a chart),
        so we use `fake_prepare()` which synthesises the same `AppPrepResult`
        without doing I/O.
        """
        fake = self.headlamp.fake_prepare()
        self._print_user_handoff(fake)

    def _print_user_handoff(self, headlamp) -> None:
        steps = self.tofu.next_steps()
        kubeconfig_export = f"export KUBECONFIG={self.paths.tofu_dir}/kubeconfig"
        self.log.info("")
        self.log.info("=" * 72)
        self.log.info("  [user]  Bootstrap finished. The cluster does NOT exist yet.")
        self.log.info("          Run the commands below in order. Each is idempotent.")
        self.log.info("=" * 72)
        self._you("Step 1/6  Inspect the plan (read carefully before applying):")
        self.log.info(f"  $ {steps.plan}")
        self.log.info("")
        self._you("Step 2/6  Apply (this is what creates the kind cluster):")
        self.log.info(f"  $ {steps.apply}")
        self.log.info("")
        self._you("Step 3/6  Verify the cluster is up (you should see 5 nodes Ready):")
        self.log.info(f"  $ {steps.kubectl}")
        self.log.info("")
        self._you("Step 4/6  Install Headlamp into the cluster:")
        self.log.info(f"  $ {kubeconfig_export} \\")
        self.log.info(f"      {headlamp.helm_command}")
        self.log.info("")
        self._you("Step 5/6  Discover the Headlamp URL (run on the host, prints http://NODE_IP:NODE_PORT):")
        self.log.info(f"  $ {kubeconfig_export}")
        self.log.info( "    NODE_PORT=$(kubectl get --namespace headlamp -o jsonpath=\"{.spec.ports[0].nodePort}\" services headlamp)")
        self.log.info( "    NODE_IP=$(kubectl   get nodes     --namespace headlamp -o jsonpath=\"{.items[0].status.addresses[0].address}\")")
        self.log.info( "    echo \"http://$NODE_IP:$NODE_PORT\"")
        self.log.info("")
        self._you("Step 6/6  Mint a Headlamp login token (paste it into the dashboard's token login form):")
        self.log.info(f"  $ {kubeconfig_export} \\")
        self.log.info( "    kubectl create token headlamp --namespace headlamp")
        self.log.info("")
        self.log.info("=" * 72)
        self._app("Done. You are now the operator.")

    def _print_report(self, report) -> None:
        width = max(len(r.name) for r in report)
        for r in report:
            if r.ok:
                self.log.ok(f"  {TAG_BOOTSTRAP} {r.name:<{width}}  OK   {r.version or ''}")
            else:
                self.log.err(f"  {TAG_BOOTSTRAP} {r.name:<{width}}  MISSING")
        if not self.prereqs.daemon_ok():
            self.log.err(f"  {TAG_BOOTSTRAP} {'docker_daemon':<{width}}  DOWN")
        else:
            self.log.ok(f"  {TAG_BOOTSTRAP} {'docker_daemon':<{width}}  OK")