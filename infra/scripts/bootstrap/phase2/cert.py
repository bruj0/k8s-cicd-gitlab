"""Wildcard TLS cert publisher (reuses Phase 1's self-signed CA).

Per spec, Phase 2 uses the local self-signed CA that Phase 1 already
generated. This class does NOT re-issue anything — it just republishes
the existing `infra/tls/private/_.local.bruj0.net.{crt,key}` as a
`kubernetes.io/tls` Secret in every namespace that serves a
`*.local.bruj0.net` host.

Idempotency: `kubectl create secret tls` errors if the secret exists.
We use `kubectl apply` with a YAML manifest built in-memory so re-runs
update the Secret rather than failing.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, CommandResult


# Phase 2 namespaces that serve the wildcard. Adding a new app?
# Just add its namespace here — the cert will follow.
TLS_NAMESPACES: tuple[str, ...] = (
    "gitlab",
    "openbao",
)


@dataclass(frozen=True)
class CertSecret:
    """Kubernetes Secret name + the cert/key bytes we wrote there."""

    namespace: str
    secret_name: str

    @property
    def labels(self) -> tuple[str, ...]:
        return (self.namespace, self.secret_name)


class WildcardCertInstaller:
    """Publish the Phase 1 wildcard cert into every Phase 2 namespace."""

    SECRET_NAME = "local-bruj0-net-tls"

    def __init__(self, runner: CommandRunner, paths: Paths, log: Logger) -> None:
        self._r = runner
        self._paths = paths
        self._log = log

    # ---------- idempotency probe ----------

    def is_published(self, namespace: str) -> bool:
        cmd = ["kubectl", "--namespace", namespace, "get", "secret", self.SECRET_NAME]
        result = self._r.run(cmd, check=False)
        return result.ok

    # ---------- main entrypoint ----------

    def publish(self) -> list[CertSecret]:
        """Ensure the TLS Secret exists in every TLS_NAMESPACE.

        Returns the list of secrets actually touched (idempotent — only
        namespaces where the secret was newly created or refreshed appear
        in the returned list).
        """
        cert_path = self._paths.tls_private / "_.local.bruj0.net.crt"
        key_path = self._paths.tls_private / "_.local.bruj0.net.key"
        if not cert_path.exists() or not key_path.exists():
            raise FileNotFoundError(
                f"Phase 1 wildcard cert not found at {cert_path}. "
                f"Run `python3 infra/scripts/bootstrap.py --phase 1` first to mint it."
            )
        cert_b64 = base64.b64encode(cert_path.read_bytes()).decode("ascii")
        key_b64 = base64.b64encode(key_path.read_bytes()).decode("ascii")

        # YAML manifest for `kubectl apply`. The Secret type is
        # kubernetes.io/tls — kubectl picks up tls.crt / tls.key from it.
        manifest = (
            "apiVersion: v1\n"
            "kind: Secret\n"
            "metadata:\n"
            f"  name: {self.SECRET_NAME}\n"
            "type: kubernetes.io/tls\n"
            "stringData:\n"
            "  tls.crt: |\n"
            + "\n".join(f"    {ln}" for ln in cert_path.read_text().splitlines())
            + "\n  tls.key: |\n"
            + "\n".join(f"    {ln}" for ln in key_path.read_text().splitlines())
            + "\n"
        )

        published: list[CertSecret] = []
        for ns in TLS_NAMESPACES:
            self._ensure_namespace(ns)
            self._r.run(["kubectl", "apply", "--namespace", ns, "-f", "-"], stdin=manifest)
            self._log.ok(f"TLS Secret {self.SECRET_NAME} published in namespace {ns}")
            published.append(CertSecret(namespace=ns, secret_name=self.SECRET_NAME))
        return published

    # ---------- internals ----------

    def _ensure_namespace(self, namespace: str) -> None:
        """Create the namespace if it doesn't exist (idempotent)."""
        cmd = ["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "yaml"]
        result: CommandResult = self._r.run(cmd)
        self._r.run(["kubectl", "apply", "-f", "-"], stdin=result.stdout)