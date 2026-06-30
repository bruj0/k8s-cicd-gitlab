"""User-facing OpenBao helper.

After the Phase 2 install finishes, the user wants to:

  1. Read secrets back out of OpenBao (e.g. the GitLab initial
     root password) — `python -m bootstrap.secrets_cli read <path>
     <key>`.
  2. Open the OpenBao UI in a browser — `python -m bootstrap.secrets_cli ui`.

Both commands auto-port-forward 127.0.0.1:8200 via the same
mechanism `OpenBaoClient` uses internally. There's no need to
remember the kubectl port-forward dance.

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
import socket
import subprocess
import sys
import time
from pathlib import Path

import click
import hvac

from .logger import ConsoleLogger


# Same constants as OpenBaoClient — duplicated here to keep this
# module importable without dragging in the Phase 2 bootstrap graph.
INIT_FILE = Path("infra/secrets/openbao-init.json")
LOCAL_URL = "http://127.0.0.1:8200"
SERVICE_NAMESPACE = "openbao"
SERVICE_NAME = "openbao"
SERVICE_PORT = 8200
LOCAL_PORT = 8200


def _load_token() -> str:
    """Load the root token from infra/secrets/openbao-init.json."""
    if not INIT_FILE.exists():
        raise click.ClickException(
            f"OpenBao init file not found at {INIT_FILE}. "
            "Has the Phase 2 install run yet?"
        )
    return json.loads(INIT_FILE.read_text())["root_token"]


def _ensure_port_forward(log) -> None:
    """Make sure 127.0.0.1:8200 is reachable.

    Spawns `kubectl port-forward` in the background and waits for
    the TCP socket to be live. Idempotent: if 8200 is already
    accepting connections (e.g. user has their own forward), we
    skip the spawn.
    """
    try:
        with socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=0.5):
            return  # already reachable
    except OSError:
        pass
    cmd = [
        "kubectl", "port-forward",
        f"--namespace={SERVICE_NAMESPACE}",
        f"svc/{SERVICE_NAME}",
        f"{LOCAL_PORT}:{SERVICE_PORT}",
    ]
    click.echo(f"starting port-forward: {' '.join(cmd)}")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Wait up to 10s for the kubelet handshake.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise click.ClickException(
        f"port-forward to 127.0.0.1:{LOCAL_PORT} did not become ready"
    )


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
    _ensure_port_forward(log)
    client = hvac.Client(url=LOCAL_URL, token=_load_token())
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
    _ensure_port_forward(log)
    token = _load_token()
    click.echo(f"OpenBao UI:   http://127.0.0.1:{LOCAL_PORT}/ui")
    click.echo(f"Root token:   {token}")
    click.echo("Port-forward is running in the background. Press Ctrl-C to stop it.")
    try:
        # Keep the process alive so the port-forward stays up.
        # The user can stop with Ctrl-C, which kills us (and the
        # port-forward child thanks to start_new_session=True +
        # process group cleanup).
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        click.echo("shutting down port-forward")


if __name__ == "__main__":
    sys.exit(main())