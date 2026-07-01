"""CLI entry point for `blueprint-bootstrap` (the script declared in
`pyproject.toml`).

Wraps the existing argparse-based `BootstrapApp.from_argv()` so the
`uv run blueprint-bootstrap …` invocation has the same UX as the
legacy `python3 infra/scripts/bootstrap.py …`.

Why click instead of just argparse?
  - Better --help formatting out of the box.
  - Subcommands are easier to add later (we currently have one
    default command that runs the bootstrap, but we may add
    `bootstrap-bootstrap lint`, `… smoke`, `… clean` etc.).
  - Plays well with `uv run <script>` (entry-points declared in
    pyproject get installed into `.venv/bin/` by `uv sync`).

Run modes (preserved from the legacy CLI):

  uv run blueprint-bootstrap                  # Phase 1 prep
  uv run blueprint-bootstrap --phase 2         # install GitLab stack
  uv run blueprint-bootstrap --phase 2 --dry-run
  uv run blueprint-bootstrap --user            # Phase 1 handoff cheat sheet
  uv run blueprint-bootstrap --check           # prereq check only
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from .app import BootstrapApp


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Bootstrap for the local GitLab + OpenBao kind-cluster blueprint. "
        "Phase 1 prepares the working tree (prereqs, tfvars, helm chart "
        "cache) and prints the next commands. Phase 2 installs GitLab + "
        "Runner + OpenBao end-to-end. Per spec, Phase 1 bootstrap never "
        "runs OpenTofu."
    ),
)
@click.option(
    "--phase",
    type=click.IntRange(1, 2),
    default=1,
    show_default=True,
    help="Which phase to run. 1 = prepare kind cluster (default). "
         "2 = install GitLab stack end-to-end.",
)
@click.option(
    "--domain",
    default="local.bruj0.net",
    show_default=True,
    help="Base DNS domain for the cluster.",
)
@click.option(
    "--check",
    is_flag=True,
    help="Only check prereqs; do not install or provision.",
)
@click.option(
    "--skip-install",
    is_flag=True,
    help="Assume prereqs are present; only check.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Log every command without executing it.",
)
@click.option(
    "--user",
    is_flag=True,
    help=(
        "Only print the Phase-1 user-handoff block: the commands YOU "
        "must run after bootstrap finishes (tofu plan / apply, kubectl "
        "verify, helm install, discover Headlamp URL, mint login "
        "token). Useful as a cheat sheet. (Phase 1 only.)"
    ),
)
@click.option(
    "--port-forward",
    "port_forward_target",
    default=None,
    metavar="TARGET",
    help=(
        "Forward a local port to a cluster service (e.g. 'gitlab' "
        "forwards 127.0.0.1:8443 → chart-managed Envoy Gateway "
        ":443). Blocks until interrupted. The kind cluster does not "
        "expose 80/443 externally (no LoadBalancer IPs in kind), "
        "so this is how the browser reaches GitLab at "
        "https://gitlab.local.bruj0.net:8443."
    ),
)
@click.option(
    "--destroy",
    "destroy",
    is_flag=True,
    help=(
        "Wipe EVERYTHING the bootstrap manages: run `tofu destroy` "
        "on infra/tofu to remove the kind cluster, then delete the "
        "host-side tree under infra/data/shared/ (stable hostPath "
        "PVC backing dirs for CNPG / Redis / MinIO / OpenBao / "
        "Gitaly + chart-managed *.preserved-* leftovers), "
        "infra/tls/wildcard/ (self-signed CA + cert), "
        "infra/secrets/openbao-init.json (root token + unseal key), "
        "and infra/secrets/gitlab-runtime-secrets.yaml (chart-managed "
        "passwords). Use this when you want a true from-scratch reset; "
        "the next `bootstrap --phase 2` will recreate everything as if "
        "for the first time. Requires `--yes` to actually run "
        "(otherwise just prints what it would do). Pair with "
        "`--preserve-data` (inverse: skip the data wipe) for users "
        "who want the cluster gone but the host-side stateful dirs "
        "left intact (the legacy 2026-06 contract)."
    ),
)
@click.option(
    "--preserve-data",
    "preserve_data",
    is_flag=True,
    help=(
        "Inverse of the default destroy contract: `bootstrap "
        "--destroy --preserve-data` runs `tofu destroy` with "
        "`var.preserve_stateful_data=true`, which keeps the host-side "
        "infra/data/shared/stable/* dirs (and chart-managed PVC "
        "leftovers via the `mv` teardown script) intact. Useful when "
        "you want to recreate the cluster but reuse the on-disk PG / "
        "Redis / MinIO / OpenBao / Gitaly data — note this requires "
        "the chart-managed Secrets snapshot to also be present "
        "(infra/secrets/gitlab-runtime-secrets.yaml) so the chart "
        "install picks up the same credentials as the on-disk data, "
        "else PG logs `password authentication failed`. Mirror of "
        "the var on the tofu side (`tofu apply "
        "-var=preserve_stateful_data=true`); the two flags must "
        "agree — pass the matching tofu var when re-applying the "
        "cluster."
    ),
)
@click.option(
    "--yes",
    "confirm_yes",
    is_flag=True,
    help="Skip the interactive confirmation prompt for --destroy.",
)
def main(
    phase: int,
    domain: str,
    check: bool,
    skip_install: bool,
    dry_run: bool,
    user: bool,
    port_forward_target: str | None,
    destroy: bool,
    preserve_data: bool,
    confirm_yes: bool,
) -> int:
    """Run the bootstrap."""
    if destroy:
        return _run_destroy(confirm_yes, dry_run, preserve_data)
    # Translate click args into the argv shape BootstrapApp.from_argv
    # expects. Keeping BootstrapApp unchanged means we don't have to
    # touch the entire arg-parsing surface in one shot — the click
    # wrapper is a thin shim.
    if port_forward_target is not None:
        # Short-circuit: skip the bootstrap entirely and just keep
        # the port-forward alive in the foreground.
        return _run_port_forward(port_forward_target)
    argv: list[str] = []
    if phase != 1:
        argv += ["--phase", str(phase)]
    if domain != "local.bruj0.net":
        argv += ["--domain", domain]
    if check:
        argv += ["--check"]
    if skip_install:
        argv += ["--skip-install"]
    if dry_run:
        argv += ["--dry-run"]
    if user:
        argv += ["--user"]
    # BootstrapApp looks for the bootstrap/ package relative to the
    # first positional arg-less invocation, so we pass the package
    # dir explicitly.
    bootstrap_dir = Path(__file__).resolve().parent
    app = BootstrapApp.from_argv(bootstrap_dir, argv)
    return app.run()


def _run_port_forward(target: str) -> int:
    """Block forever, forwarding a host port to a cluster service.

    Supported targets (the only one the blueprint currently needs is
    `gitlab`):
        gitlab   127.0.0.1:8443 → svc/envoy-<gateway>-<hash> :https-443
                 on the chart-managed Envoy Gateway that fronts
                 GitLab webservice + registry + kas + minio. We use
                 host port 8443 (not 443) because kind's
                 `extraPortMappings` already reserves 443 on the
                 control-plane container for future use; binding
                 443 from a port-forward would conflict.

                 We forward to the gateway Service's named port
                 `https-443` (whose targetPort is 10443, the port
                 the Envoy proxy actually binds on). Forwarding
                 to port 443 would not resolve because the
                 Service has no service port 443 — only the named
                 port `https-443` whose port field is 443.

                 IMPORTANT: the Gateway has multiple listeners,
                 one per hostname (gitlab.local.bruj0.net,
                 registry.local.bruj0.net, kas.local.bruj0.net,
                 minio.local.bruj0.net). All four are TLS-terminated
                 by Envoy. To get the right cert back, the client
                 must send SNI. For browsers that just means
                 visiting `https://gitlab.local.bruj0.net:8443/...`
                 (the SNI hostname is derived from the URL host).
                 For `curl`, you need `--resolve <host>:8443:127.0.0.1`
                 so the SNI matches the listener hostname.

    For everything else (OpenBao UI, k9s) the existing
    `bootstrap.secrets_cli ui` and `k9s` commands do the right
    thing already — they're auto-managed by their respective
    helpers.
    """
    import os
    import signal
    import subprocess

    if target == "gitlab":
        local_port = 8443
        # Discover the Envoy Gateway Service — the chart picks a
        # hash suffix that changes on every install/upgrade, so
        # we must look it up rather than hard-code it.
        svc_query = subprocess.run(
            [
                "kubectl", "-n", "gitlab",
                "get", "svc",
                "-l", "gateway.envoyproxy.io/owning-gateway-name=gitlab-gw",
                "-o", "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True, text=True,
        )
        svc = svc_query.stdout.strip()
        if not svc:
            print(
                "ERROR: GitLab Envoy Gateway service not found. "
                "Run `bootstrap --phase 2` first.",
                file=sys.stderr,
            )
            return 1
        # Use the named port `https-443` rather than the numeric
        # 443 — the Service has no port "443", only a named port
        # whose `port:` field is 443 and `targetPort:` is 10443.
        cmd = [
            "kubectl", "-n", "gitlab",
            "port-forward",
            "--address", "127.0.0.1",
            f"svc/{svc}",
            f"{local_port}:https-443",
        ]
        print(
            f"Forwarding 127.0.0.1:{local_port} → {svc}.gitlab.svc:https-443\n"
            f"Visit: https://gitlab.local.bruj0.net:{local_port}/\n"
            f"  (the URL host drives the SNI hostname that picks the\n"
            f"   right Gateway listener + cert.)\n"
            f"For curl: curl -k --resolve gitlab.local.bruj0.net:{local_port}:127.0.0.1 \\\n"
            f"          https://gitlab.local.bruj0.net:{local_port}/users/sign_in\n"
            f"(Press Ctrl+C to stop.)\n"
        )
    else:
        print(
            f"ERROR: unknown --port-forward target {target!r}. "
            f"Supported: gitlab",
            file=sys.stderr,
        )
        return 2

    # Replace this process with `kubectl port-forward` so Ctrl+C
    # cleanly stops the forward (instead of the parent shell).
    try:
        os.execvp(cmd[0], cmd)
    except KeyboardInterrupt:
        return 0


def _run_destroy(confirm_yes: bool, dry_run: bool, preserve_data: bool = False) -> int:
    """Wipe the entire blueprint state: cluster + (optional) data + secrets.

    Stages (each prints a clear `[destroy] <stage>` line):

      1. `tofu destroy` on infra/tofu (kind cluster, port-forward
         notes, kubeconfig file). Leaves host-side `infra/data/`
         and `infra/secrets/` untouched — those are owned by the
         bootstrap, not by tofu. When `--preserve-data` is passed,
         we pass `-var=preserve_stateful_data=true` to tofu so the
         cluster's `null_resource.wipe_data` destroy provisioner
         becomes a no-op (and the bind-mount `propagation` is set
         to default HostToContainer instead of Bidirectional,
         keeping the host-side data source intact).
      2. `infra/data/shared/stable/*` — hostPath PV backing dirs
         for CloudNativePG, Redis, MinIO, OpenBao + Gitaly state.
         SKIPPED when `--preserve-data`.
      3. `infra/data/shared/*` (everything NOT under stable/) —
         leftover PV dirs from previous helm chart installs. Today
         (2026-07+) local-path's default teardown script is `rm
         -rf`, so leftovers rarely exist; the `*.preserved-*`
         glob is a no-op most of the time but covers any
         hand-flip cases. SKIPPED when `--preserve-data`.
      4. `infra/tls/wildcard/*` — the self-signed CA + cert. The
         next install regenerates these (10-year validity, so this
         only matters if the cluster has been compromised).
      5. `infra/secrets/openbao-init.json` — OpenBao's root token
         + unseal key. The next install re-initialises OpenBao.
      6. `infra/secrets/cnpg-role-passwords.json` — the
         CloudNativePG GitLab + OpenBao database role passwords.
         The next install regenerates stable SHA-256-derived
         passwords.
      7. `infra/secrets/redis-password.txt` +
         `infra/secrets/minio-root-user.txt` +
         `infra/secrets/minio-root-password.txt` — the chart-
         external Redis + MinIO credentials.
      8. `infra/secrets/gitlab-runtime-secrets.yaml` — the chart's
         snapshot of Rails/Gitaly/KAS passwords. PG/Redis/MinIO
         are no longer in this snapshot (they live in their own
         files). The next install re-mints fresh ones.

    Idempotent: each stage silently no-ops if its target doesn't
    exist. Safe to re-run after a partial failure.

    Args:
        confirm_yes: skip the interactive y/N prompt (for scripts).
        dry_run: print what would be removed without removing.
        preserve_data: keep host-side infra/data/shared/* intact
            (skips stages 2-3 in this list). Mirrors the tofu
            `var.preserve_stateful_data = true`; the two MUST
            agree when re-applying.
    """
    blueprint_root = Path(__file__).resolve().parent.parent.parent.parent
    infra = blueprint_root / "infra"
    tofu_dir = infra / "tofu"
    data_shared = infra / "data" / "shared"
    stable_dir = data_shared / "stable"
    secrets_dir = infra / "secrets"
    tls_wildcard = infra / "tls" / "wildcard"

    stages: list[tuple[str, str, "Callable[[], None]"]] = [
        (
            f"tofu destroy (kind cluster + kubeconfig + smoke test; "
            f"preserve_data={'true' if preserve_data else 'false'})",
            str(tofu_dir),
            lambda: _run_tofu_destroy(tofu_dir, dry_run, preserve_data),
        ),
        (
            "stable service data (cnpg/redis/minio/openbao/gitaly)",
            str(stable_dir),
            lambda: _rm(stable_dir, dry_run, glob="*"),
        ),
        (
            "leftover PV dirs from prior helm installs (*.preserved-*)",
            str(data_shared),
            lambda: _rm(data_shared, dry_run,
                        glob="*.preserved-*", keep=["stable"]),
        ),
        (
            "self-signed wildcard TLS cert + CA",
            str(tls_wildcard),
            lambda: _rm(tls_wildcard, dry_run, glob="*"),
        ),
        (
            "OpenBao root token + unseal key",
            str(secrets_dir / "openbao-init.json"),
            lambda: _rm(secrets_dir / "openbao-init.json", dry_run),
        ),
        (
            "CloudNativePG role passwords (git / openbao DB users)",
            str(secrets_dir / "cnpg-role-passwords.json"),
            lambda: _rm(secrets_dir / "cnpg-role-passwords.json", dry_run),
        ),
        (
            "Redis master password (auto-generated by bitnami chart)",
            str(secrets_dir / "redis-password.txt"),
            lambda: _rm(secrets_dir / "redis-password.txt", dry_run),
        ),
        (
            "MinIO root user + password (auto-generated by minio chart)",
            str(secrets_dir / "minio-root-user.txt"),
            lambda: _rm(secrets_dir / "minio-root-user.txt", dry_run),
        ),
        (
            "MinIO root password",
            str(secrets_dir / "minio-root-password.txt"),
            lambda: _rm(secrets_dir / "minio-root-password.txt", dry_run),
        ),
        (
            "GitLab chart-managed Secrets snapshot",
            str(secrets_dir / "gitlab-runtime-secrets.yaml"),
            lambda: _rm(secrets_dir / "gitlab-runtime-secrets.yaml", dry_run),
        ),
    ]

    if preserve_data:
        # Skip the host-side data stages (2 + 3). The list is
        # built once above; we mutate the lambdas in place so
        # subsequent calls in the loop see the same callable
        # objects (Python closures-by-reference). The host-side
        # tofu destroy already short-circuits via the var, so
        # the only two stages we need to skip here are the
        # bootstrap-side rm of stable/ + data_shared.
        def _noop() -> None:
            return
        for idx in (1, 2):
            stages[idx] = (stages[idx][0] + " [SKIPPED: --preserve-data]", stages[idx][1], _noop)

    if not confirm_yes and not dry_run:
        if preserve_data:
            print(
                "This will permanently remove:\n"
                f"  - 1 kind cluster (via tofu destroy, "
                f"preserve_stateful_data=true)\n"
                f"  - {tls_wildcard}\n"
                f"  - {secrets_dir / 'openbao-init.json'}\n"
                f"  - {secrets_dir / 'cnpg-role-passwords.json'}\n"
                f"  - {secrets_dir / 'redis-password.txt'}\n"
                f"  - {secrets_dir / 'minio-root-user.txt'}\n"
                f"  - {secrets_dir / 'minio-root-password.txt'}\n"
                f"  - {secrets_dir / 'gitlab-runtime-secrets.yaml'}\n"
                f"\n"
                f"PRESERVED (intentionally, --preserve-data):\n"
                f"  - {stable_dir}     (CNPG/Redis/MinIO/OpenBao/Gitaly state)\n"
                f"  - {data_shared}*.preserved-*  (chart-managed leftovers)\n"
            )
        else:
            print(
                "This will permanently remove:\n"
                f"  - 1 kind cluster (via tofu destroy)\n"
                f"  - {stable_dir}\n"
                f"  - {data_shared} (orphaned *.preserved-* dirs only)\n"
                f"  - {tls_wildcard}\n"
                f"  - {secrets_dir / 'openbao-init.json'}\n"
                f"  - {secrets_dir / 'cnpg-role-passwords.json'}\n"
                f"  - {secrets_dir / 'redis-password.txt'}\n"
                f"  - {secrets_dir / 'minio-root-user.txt'}\n"
                f"  - {secrets_dir / 'minio-root-password.txt'}\n"
                f"  - {secrets_dir / 'gitlab-runtime-secrets.yaml'}\n"
            )
        try:
            answer = input("Proceed? [y/N] ")
        except EOFError:
            answer = "n"
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 0

    print()
    for name, path, action in stages:
        marker = "[dry-run] " if dry_run else ""
        print(f"{marker}[destroy] {name} ({path})")
        try:
            action()
        except Exception as exc:
            print(f"  ERROR: {exc}")
            return 1
    print()
    print("[destroy] Done. Next step: `bootstrap --phase 2` will rebuild "
          "the cluster from scratch.")
    return 0


def _run_tofu_destroy(
    tofu_dir: Path, dry_run: bool, preserve_data: bool = False,
) -> None:
    """Run `tofu destroy` in infra/tofu. No-op if no state exists.

    When `preserve_data` is true, pass
    `-var=preserve_stateful_data=true` so the
    `null_resource.wipe_data` destroy provisioner (which sweeps
    infra/data/shared/* by default) becomes a no-op and the
    `extra_mounts.propagation` field defaults to HostToContainer
    (instead of Bidirectional). The host-side data is then left
    intact across `tofu destroy`. The user MUST pass the same
    var to the next `tofu apply` to keep the contract consistent
    (otherwise the cluster would apply with Bidirectional + a
    wipe_data hook that re-reads the now-stale value).
    """
    import subprocess
    tfstate = tofu_dir / "terraform.tfstate"
    if not tfstate.exists() and not dry_run:
        print(f"  (no state at {tfstate}; skipping)")
        return
    cmd = ["tofu", "destroy", "-auto-approve"]
    if preserve_data:
        cmd.extend(["-var=preserve_stateful_data=true"])
    if dry_run:
        cmd = ["tofu", "plan", "-destroy"]
        if preserve_data:
            cmd.extend(["-var=preserve_stateful_data=true"])
    result = subprocess.run(
        cmd, cwd=str(tofu_dir),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"tofu destroy failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    # Print a brief summary so the user sees something happened.
    tail = "\n".join((result.stdout or "").splitlines()[-5:])
    if tail:
        print(f"  {tail.replace(chr(10), chr(10) + '  ')}")


def _rm(path: Path, dry_run: bool, glob: str = "*", keep: list[str] | None = None) -> None:
    """Recursively delete path matching glob, optionally keeping some names.

    No-op if the target doesn't exist. Used by --destroy to wipe
    specific host-side trees without touching siblings.
    """
    import shutil
    if not path.exists():
        print(f"  (does not exist; skipping)")
        return
    if path.is_file():
        if dry_run:
            print(f"  (dry-run: would delete {path})")
        else:
            path.unlink()
            print(f"  deleted {path}")
        return
    keep_set = set(keep or [])
    children = list(path.glob(glob)) if glob != "*" else list(path.iterdir())
    if not children:
        print(f"  (empty; skipping)")
        return
    for child in children:
        if child.name in keep_set:
            continue
        if dry_run:
            print(f"  (dry-run: would delete {child})")
        elif child.is_dir():
            # PV dirs are owned by the pod's runtime UID (e.g. openbao=100,
            # postgres=1001) which our bootstrap user can't rmtree by
            # default — and the inner files/dirs (mode 0700) are owned by
            # the same UID, so neither chmod nor rm work from this user
            # (sudo typically requires a password).
            #
            # Fall back to a one-shot privileged container that bind-
            # mounts the path and rm's it for us. We try Docker first
            # (most common), then Podman (rootless containers are fine
            # for this since we only need to mutate the bind mount on
            # the host). If neither is available, the user has to
            # delete the dir manually with `sudo rm -rf`.
            import subprocess
            chmod = subprocess.run(
                ["chmod", "-R", "a+rwX", str(child)],
                capture_output=True, text=True,
            )
            if chmod.returncode == 0:
                shutil.rmtree(child)
                print(f"  deleted {child}/")
                return
            # chmod failed (likely EPERM on inner files we can't even
            # read). Try a privileged container.
            deleted = _rm_via_privileged_container(child)
            if deleted:
                print(f"  deleted {child}/ (via privileged container)")
            else:
                raise RuntimeError(
                    f"could not delete {child}: chmod failed and no "
                    f"container runtime is available. Run `sudo rm -rf "
                    f"{child}` manually."
                )
        else:
            child.unlink()
            print(f"  deleted {child}")


def _rm_via_privileged_container(path: Path) -> bool:
    """Try to delete `path` via a one-shot privileged container.

    Used as a fallback when host-side chmod/rm fails because the
    tree is owned by a different UID (typical for kind PV dirs:
    openbao runs as UID 100, postgres as 1001, etc.). The container
    bind-mounts the parent dir read-write, then `rm -rf` the child.

    Returns True on success, False if no usable container runtime
    was found (in which case the caller surfaces a "run sudo rm -rf
    manually" message).
    """
    import subprocess
    parent = path.parent.resolve()
    target = path.name

    # Try docker first.
    for runtime in ("docker", "podman"):
        probe = subprocess.run(
            ["which", runtime], capture_output=True, text=True,
        )
        if probe.returncode != 0:
            continue
        run = subprocess.run(
            [runtime, "run", "--rm",
             "-v", f"{parent}:/mnt:rw",
             "alpine:latest",
             "sh", "-c", f"chmod -R a+rwX /mnt && rm -rf /mnt/{target}"],
            capture_output=True, text=True,
        )
        if run.returncode == 0:
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())