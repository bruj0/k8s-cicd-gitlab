"""PKI runner — delegates to the legacy scripts/pki.py module.

Kept as a class so future refactors can replace the subprocess call with
an in-process call to a Python cryptography-based signer without touching
the orchestrator.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .paths import Paths
from .shell import CommandRunner


class PkiRunner:
    def __init__(self, runner: CommandRunner, paths: Paths) -> None:
        self._r = runner
        self._paths = paths

    def ensure(self, domain: str) -> None:
        self._paths.tls_private.mkdir(parents=True, exist_ok=True)
        self._paths.tls_public.mkdir(parents=True, exist_ok=True)
        # Idempotent: pki.py rewrites the same files every run. The output
        # is the same content, so any consumer that re-reads the files
        # (Traefik, cert-manager) sees a stable cert.
        self._r.run([
            sys.executable,
            str(self._paths.script_dir / "pki.py"),
            "--domain", domain,
            "--private-dir", str(self._paths.tls_private),
            "--public-dir", str(self._paths.tls_public),
        ])