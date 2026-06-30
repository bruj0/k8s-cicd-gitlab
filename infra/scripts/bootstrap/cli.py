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
def main(
    phase: int,
    domain: str,
    check: bool,
    skip_install: bool,
    dry_run: bool,
    user: bool,
) -> int:
    """Run the bootstrap."""
    # Translate click args into the argv shape BootstrapApp.from_argv
    # expects. Keeping BootstrapApp unchanged means we don't have to
    # touch the entire arg-parsing surface in one shot — the click
    # wrapper is a thin shim.
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


if __name__ == "__main__":
    sys.exit(main())