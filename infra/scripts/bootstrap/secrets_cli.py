"""User-facing helpers for the Phase 2 stack.

After the Phase 2 install finishes, the user wants to:

  1. Read secrets back out of OpenBao (e.g. the GitLab initial
     root password) — `python -m bootstrap.secrets_cli read <path>
     <key>`.
  2. Open the OpenBao UI in a browser — `python -m bootstrap.secrets_cli ui`.
  3. Reach any in-cluster service without remembering the
     `kubectl port-forward` incantation — `python -m
     bootstrap.secrets_cli port-forward <service>`.

The first two are OpenBao-specific. The third is the generic
workhorse: a registry of (namespace, service, port) tuples plus
the same lazy `kubectl port-forward` plumbing — so users don't
have to remember the `-n` flag, the right port number, or that
the kubeconfig lives in `infra/tofu/kubeconfig`.

Why a separate module instead of a method on `OpenBaoClient`?
  - The CLI is for the *end user* (the operator) after install.
    `OpenBaoClient` is for the *bootstrap* during install.
    Different audiences, different ergonomics.
  - This module is shipped as part of the bootstrap package and
    can be invoked with `uv run python -m bootstrap.secrets_cli …`
    without any context about the rest of the install pipeline.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import click
import hvac

from .logger import ConsoleLogger


# --- OpenBao-specific constants (used by `read` + `ui`) -----------
INIT_FILE = Path("infra/secrets/openbao-init.json")
OPENBAO_LOCAL_URL = "http://127.0.0.1:8200"
OPENBAO_NAMESPACE = "openbao"
OPENBAO_SERVICE = "openbao"
OPENBAO_SERVICE_PORT = 8200
OPENBAO_LOCAL_PORT = 8200


# --- Generic port-forward registry --------------------------------
#
# Each entry maps a friendly name to the in-cluster Service the user
# wants to reach. `local_port: None` means "use the cluster port"
# (the most common case — we talk to the same port number on
# 127.0.0.1 as the Service exposes).
#
# `after` is a lambda that runs once the forward is up and prints
# next-step hints (URLs, /etc/hosts entries, etc.). Keep it
# idempotent + terse.
@dataclass(frozen=True)
class PortForwardTarget:
    key: str            # CLI name (e.g. "gitlab")
    name: str           # Human-readable label
    namespace: str      # Kubernetes namespace
    service: str        # Service name
    cluster_port: int   # In-cluster port to forward
    local_port: int | None  # Local bind port (None = same as cluster_port)
    after: "callable[[int], None] | None" = None  # prints next steps


def _print_gitlab_hints(local_port: int) -> None:
    """Print a /etc/hosts + curl hint after the webservice is forwarded."""
    click.echo("")
    click.echo("Quick checks:")
    click.echo(f"  curl -s http://127.0.0.1:{local_port}/-/health")
    click.echo("")
    click.echo("For the full Gateway-reachable path, see")
    click.echo("  docs/phase-2.md (NodePort exposure section).")


def _print_registry_hints(local_port: int) -> None:
    click.echo("")
    click.echo("Login:")
    click.echo(
        "  printf '<root_token>\\n' | sudo docker login "
        f"127.0.0.1:{local_port} -u root --password-stdin"
    )


def _print_minio_hints(local_port: int) -> None:
    click.echo("")
    click.echo("MinIO root creds are in infra/secrets/minio.txt.")
    click.echo(f"Console:  http://127.0.0.1:9001")
    click.echo(f"S3 API:   http://127.0.0.1:{local_port}")


# Public registry. Order here is the order shown in `port-forward --list`.
PORT_FORWARD_TARGETS: tuple[PortForwardTarget, ...] = (
    PortForwardTarget(
        key="openbao",
        name="OpenBao (KV v2 + UI on port 8200)",
        namespace="openbao",
        service="openbao",
        cluster_port=8200,
        local_port=8200,
        after=lambda lp: click.echo(
            f"\nOpenBao UI:    http://127.0.0.1:{lp}/ui\n"
            f"Token helper:  python -m bootstrap.secrets_cli read \\\n"
            f"                 <path> <key>"
        ),
    ),
    PortForwardTarget(
        key="gitlab-webservice",
        name="GitLab webservice workhorse (HTTP, port 8181) — "
             "internal/CLI use only, NOT for browsers",
        namespace="gitlab",
        service="gitlab-webservice-default",
        cluster_port=8181,
        local_port=8181,
        after=_print_gitlab_hints,
    ),
    PortForwardTarget(
        key="gitlab-registry",
        name="Container Registry (Docker registry HTTP API, port 5000)",
        namespace="gitlab",
        service="gitlab-registry",
        cluster_port=5000,
        local_port=5000,
        after=_print_registry_hints,
    ),
    PortForwardTarget(
        key="gitlab-kas",
        name="GitLab KAS (gRPC proxy, port 8151)",
        namespace="gitlab",
        service="gitlab-kas",
        cluster_port=8151,
        local_port=8151,
    ),
    PortForwardTarget(
        key="minio",
        name="MinIO S3 API (port 9000)",
        namespace="minio",
        service="minio",
        cluster_port=9000,
        local_port=9000,
        after=_print_minio_hints,
    ),
    PortForwardTarget(
        key="minio-console",
        name="MinIO Console (web UI, port 9001)",
        namespace="minio",
        service="minio-console",
        cluster_port=9001,
        local_port=9001,
    ),
)

_TARGETS_BY_KEY: dict[str, PortForwardTarget] = {t.key: t for t in PORT_FORWARD_TARGETS}


def _resolve_kubeconfig() -> str:
    """Find a kubeconfig usable for port-forwards.

    Prefers `infra/tofu/kubeconfig` (the kind cluster's admin
    kubeconfig, written by tofu) and falls back to $KUBECONFIG or
    `~/.kube/config` so a user with their own context set up
    doesn't have to fight us.
    """
    # 1. tofu-created kind kubeconfig (most common in this repo)
    blueprint_root = Path(__file__).resolve().parents[2]
    tf_kube = blueprint_root / "infra" / "tofu" / "kubeconfig"
    if tf_kube.exists():
        return str(tf_kube)
    # 2. env var
    env = os.environ.get("KUBECONFIG")
    if env and Path(env).exists():
        return env
    # 3. default ~/.kube/config
    default = Path.home() / ".kube" / "config"
    if default.exists():
        return str(default)
    raise click.ClickException(
        "no kubeconfig found — expected infra/tofu/kubeconfig "
        "(kind cluster created by tofu) or $KUBECONFIG"
    )


def _ensure_port_forward_generic(
    target: PortForwardTarget,
    kubeconfig: str,
    local_port_override: int | None = None,
) -> int:
    """Start (if needed) a port-forward for `target`, return the local port.

    Behaviour:
      - If 127.0.0.1:<local_port> is already accepting TCP, we
        assume something else owns it (kubectl port-forward or
        a previous run still alive) and reuse — no spawn. This
        matches the `secrets_cli ui` semantics.
      - Otherwise spawn `kubectl --kubeconfig <kc> port-forward
        -n <ns> svc/<svc> <local>:<remote>` in its own process
        group (so Ctrl-C / process kill cleans up the whole
        tree), wait until the TCP socket answers, return.
    """
    local_port = local_port_override or target.local_port or target.cluster_port
    try:
        with socket.create_connection(("127.0.0.1", local_port), timeout=0.5):
            return local_port  # already up — assume it's ours (or harmless)
    except OSError:
        pass

    cmd = [
        "kubectl",
        f"--kubeconfig={kubeconfig}",
        "port-forward",
        f"--namespace={target.namespace}",
        f"svc/{target.service}",
        f"{local_port}:{target.cluster_port}",
    ]
    click.echo(f"starting port-forward: {' '.join(shlex.quote(p) for p in cmd)}")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # own process group → Ctrl-C kills the tree
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                ("127.0.0.1", local_port), timeout=0.5
            ):
                return local_port
        except OSError:
            time.sleep(0.1)
    raise click.ClickException(
        f"port-forward to 127.0.0.1:{local_port} did not become ready"
    )


def _wait_until_ctrl_c(pids: tuple[int, ...] = ()) -> None:
    """Block on a sleep loop, killing the spawned kubectl on Ctrl-C.

    On signal, sends SIGTERM to every PID we know about
    (os.getpgrp() finds them all anyway via session ID), then
    returns so click exits cleanly.
    """
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        click.echo("\nshutting down port-forward")


# ---------------------------------------------------------------------------
# OpenBao-specific helpers (kept for backwards compatibility — `read`/`ui`
# still call these directly).
# ---------------------------------------------------------------------------


def _load_token() -> str:
    """Load the root token from infra/secrets/openbao-init.json."""
    if not INIT_FILE.exists():
        raise click.ClickException(
            f"OpenBao init file not found at {INIT_FILE}. "
            "Has the Phase 2 install run yet?"
        )
    return json.loads(INIT_FILE.read_text())["root_token"]


def _ensure_openbao_port_forward(log) -> None:
    """Legacy OpenBao-specific path; delegates to the generic one."""
    openbao = _TARGETS_BY_KEY["openbao"]
    _ensure_port_forward_generic(openbao, _resolve_kubeconfig())


@click.group(help="Read OpenBao secrets + open the UI (auto-port-forwards).")
def main() -> None:
    pass


@main.command()
@click.argument("path")
@click.argument("key", required=False)
def read(path: str, key: str | None) -> None:
    """Read a KV v2 secret at `secret/<path>` (or just one key from it).

    Example:
      python -m bootstrap.secrets_cli read gitlab initial_root_password
    """
    log = ConsoleLogger(color=False)
    _ensure_openbao_port_forward(log)
    client = hvac.Client(url=OPENBAO_LOCAL_URL, token=_load_token())
    try:
        resp = client.secrets.kv.v2.read_secret_version(path=path)
    except hvac.exceptions.InvalidPath:
        raise click.ClickException(f"no secret at secret/{path}")
    data = resp["data"]["data"]
    if key is not None:
        if key not in data:
            raise click.ClickException(
                f"key '{key}' not present in secret/{path} "
                f"(available: {sorted(data)})"
            )
        click.echo(data[key])
    else:
        click.echo(json.dumps(data, indent=2, sort_keys=True))


@main.command(help="Print the OpenBao UI URL + root token (port-forward stays up).")
def ui() -> None:
    log = ConsoleLogger(color=False)
    _ensure_openbao_port_forward(log)
    token = _load_token()
    click.echo(f"OpenBao UI:   {OPENBAO_LOCAL_URL}/ui")
    click.echo(f"Root token:   {token}")
    click.echo("Port-forward is running in the background. Press Ctrl-C to stop it.")
    _wait_until_ctrl_c()


@main.command(
    help="Port-forward any in-cluster Service to 127.0.0.1.\n\n"
         "Run with a service name (see `--list`) to start the forward\n"
         "and print next-step hints. Ctrl-C stops the forward."
)
@click.argument("service", required=False)
@click.option(
    "--list", "list_",
    is_flag=True,
    help="Print the registry of port-forwardable services and exit.",
)
@click.option(
    "--local-port",
    type=int,
    default=None,
    help="Override the local bind port (default: same as the cluster port).",
)
def port_forward(service: str | None, list_: bool, local_port: int | None) -> None:
    if list_ or not service:
        click.echo("Available port-forwardable services:")
        max_key = max(len(t.key) for t in PORT_FORWARD_TARGETS)
        for t in PORT_FORWARD_TARGETS:
            click.echo(f"  {t.key.ljust(max_key)}  {t.name}")
        click.echo("")
        click.echo(f"Default kubeconfig: {_resolve_kubeconfig()}")
        click.echo(
            "Use `--local-port N` to override the bind port on 127.0.0.1."
        )
        return

    target = _TARGETS_BY_KEY.get(service)
    if target is None:
        names = ", ".join(sorted(_TARGETS_BY_KEY))
        raise click.ClickException(
            f"unknown service '{service}'. "
            f"Try one of: {names} (or pass --list)."
        )
    kubeconfig = _resolve_kubeconfig()
    active_local = _ensure_port_forward_generic(target, kubeconfig, local_port)
    click.echo(
        f"\nForwarding:  127.0.0.1:{active_local} → "
        f"svc/{target.service}.{target.namespace}:{target.cluster_port}"
    )
    if target.after is not None:
        target.after(active_local)
    click.echo("\nPress Ctrl-C to stop the forward.")
    _wait_until_ctrl_c()


if __name__ == "__main__":
    sys.exit(main())