"""Self-signed wildcard cert minting for the GitLab Gateway listeners.

The chart's `templates/shared-secrets/self-signed-cert-job.yml` pre-install
Job normally mints a wildcard cert for `*.global.hosts.domain` via
`registry.gitlab.com/gitlab-org/build/cng/cfssl-self-sign` and stores it in
Secret `gitlab-wildcard-tls` (plus `gitlab-wildcard-tls-ca`).

That Job is gated by `include "gitlab.ingress.tls.configured"` returning
`!= "true"`. In our config (Gateway API on, ingress off, no
`*.ingress.tls.secretName` defaults set) the helper evaluates to "true"
because the chart picks up `gatewayApiResources.gateway.listeners.*.tls`
as "configured" (or for other reasons we couldn't fully trace), so the
self-signed Job is skipped and the listener certificateRefs resolve to
non-existent Secrets, breaking TLS.

To avoid that footgun we set `global.ingress.tls.secretName =
gitlab-wildcard-tls` in helm-values (so the helper explicitly returns
"true") and mint the Secret ourselves right after the chart's
cert-manager Issuer fails its ACME registration (the chart ships a
fallback Issuer for when `configureCertmanager: false` is set, but it
points at Let's Encrypt with an untrusted email — useless in dev).

What this installer does:

    1. Generate a CA + wildcard cert + key with `openssl` on the
       bootstrap host. SAN covers `*.local.bruj0.net` + the four
       chart-managed FQDNs (gitlab/registry/kas/minio).
    2. Materialise five Secrets in the `gitlab` namespace:
         - gitlab-wildcard-tls       (kubernetes.io/tls, cert + key)
         - gitlab-wildcard-tls-ca    (Opaque, key `cfssl_ca`)
         - registry-tls              (kubernetes.io/tls; same wildcard)
         - kas-tls                   (kubernetes.io/tls; same wildcard)
         - minio-tls                 (kubernetes.io/tls; same wildcard)
       The chart's chart-shipped Gateway listener `certificateRefs[0].name`
       is `gitlab-wildcard-tls` (we override in helm-values). The
       others are the chart-default listener cert-ref names that
       Envoy Gateway validates against.
    3. Re-run is idempotent: a fresh cert is only minted when the
       existing one expires within 30 days; existing Secrets are
       applied with `kubectl apply` (no-op when unchanged).
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..logger import Logger
from ..paths import Paths
from ..shell import CommandRunner, DryRunRunner


# Secrets the chart's Gateway listeners reference. We materialise all of
# them with the same wildcard cert so a single SAN covers every FQDN.
WILDCARD_LISTENER_SECRETS: tuple[str, ...] = (
    "gitlab-wildcard-tls",
    "registry-tls",
    "kas-tls",
    "minio-tls",
)


@dataclass(frozen=True)
class WildcardCertPaths:
    """Where the bootstrap host wrote the CA + cert + key + chain."""

    ca_pem: Path
    cert_pem: Path
    key_pem: Path
    chain_pem: Path  # cat ca.pem cert.pem — what kubectl create --from-file expects
    san_cnf: Path


class WildcardCertsInstaller:
    """Mint + apply a self-signed wildcard cert for `*.<hosts.domain>`."""

    def __init__(
        self,
        runner: CommandRunner,
        paths: Paths,
        log: Logger,
        *,
        namespace: str = "gitlab",
        domain: str = "local.bruj0.net",
        hosts: tuple[str, ...] = (
            "gitlab",
            "registry",
            "kas",
            "minio",
        ),
    ) -> None:
        self._r = runner
        self._paths = paths
        self._log = log
        self._ns = namespace
        self._domain = domain
        # Build SAN entries: bare apex + the chart-managed subdomains
        # (gitlab/registry/kas/minio) + the wildcard catch-all.
        self._sans: list[str] = [
            f"DNS.{i + 1} = {host}.{domain}"
            for i, host in enumerate(hosts)
        ]
        self._sans += [
            f"DNS.{len(hosts) + 1} = {domain}",
            f"DNS.{len(hosts) + 2} = *.{domain}",
        ]

    # ---------- public ----------

    def install(self) -> WildcardCertPaths:
        """Generate the cert on disk + apply all the Secrets. Idempotent."""
        if isinstance(self._r, DryRunRunner):
            self._log.info("[dry-run] skipping wildcard cert mint")
            return WildcardCertPaths(
                ca_pem=Path("/dev/null"),
                cert_pem=Path("/dev/null"),
                key_pem=Path("/dev/null"),
                chain_pem=Path("/dev/null"),
                san_cnf=Path("/dev/null"),
            )

        self._check_openssl()
        certs = self._generate_cert()
        self._log.info(
            f"Wildcard cert generated: subject=CN=*.{self._domain}, "
            f"SAN=[{', '.join(s.split(' = ', 1)[1] for s in self._sans)}]"
        )

        for name in WILDCARD_LISTENER_SECRETS:
            self._apply_tls_secret(name, certs)

        self._apply_ca_secret(certs)
        self._log.ok(
            f"Wildcard cert + {len(WILDCARD_LISTENER_SECRETS)} listener Secrets "
            f"applied to namespace {self._ns!r}"
        )
        return certs

    # ---------- generation ----------

    def _check_openssl(self) -> None:
        if shutil.which("openssl") is None:
            raise RuntimeError(
                "openssl not found on PATH — install OpenSSL (e.g. "
                "`pacman -S openssl` on Arch) before re-running."
            )

    def _generate_cert(self) -> WildcardCertPaths:
        """Run openssl, persisting under infra/tls/wildcard so re-runs reuse
        the CA + cert until they get close to expiring."""
        out_dir = self._paths.blueprint_dir / "infra" / "tls" / "wildcard"
        out_dir.mkdir(parents=True, exist_ok=True)

        san_cnf = out_dir / "san.cnf"
        san_cnf.write_text(self._san_config())

        ca_key = out_dir / "ca.key"
        ca_pem = out_dir / "ca.pem"
        cert_key = out_dir / "wildcard.key"
        cert_csr = out_dir / "wildcard.csr"
        cert_pem = out_dir / "wildcard.pem"
        chain_pem = out_dir / "wildcard-chain.pem"

        # Generate CA + wildcard cert only if missing OR expiring
        # within 30 days (so re-runs don't churn the secret every time
        # — and so a stale cert from a prior run is replaced).
        if self._is_cert_valid(cert_pem, min_remaining_days=30):
            self._log.info(
                f"Reusing existing wildcard cert at {cert_pem} "
                f"(valid for at least 30 more days)"
            )
        else:
            self._log.info(f"Minting fresh wildcard cert under {out_dir}")
            self._run_openssl(
                ["openssl", "genrsa", "-out", str(ca_key), "2048"],
            )
            self._run_openssl(
                [
                    "openssl", "req", "-x509", "-new", "-nodes",
                    "-key", str(ca_key), "-sha256", "-days", "3650",
                    "-out", str(ca_pem),
                    "-subj", "/CN=GitLab Wildcard CA/O=GitLab/C=US",
                ],
            )
            self._run_openssl(
                ["openssl", "genrsa", "-out", str(cert_key), "2048"],
            )
            self._run_openssl(
                [
                    "openssl", "req", "-new",
                    "-key", str(cert_key),
                    "-out", str(cert_csr),
                    "-config", str(san_cnf),
                ],
            )
            self._run_openssl(
                [
                    "openssl", "x509", "-req",
                    "-in", str(cert_csr),
                    "-CA", str(ca_pem), "-CAkey", str(ca_key),
                    "-CAcreateserial",
                    "-out", str(cert_pem),
                    "-days", "3650", "-sha256",
                    "-extfile", str(san_cnf), "-extensions", "v3_req",
                ],
            )

        # Always rebuild the chain file (cheap, never stale).
        chain_pem.write_bytes(ca_pem.read_bytes() + cert_pem.read_bytes())

        return WildcardCertPaths(
            ca_pem=ca_pem,
            cert_pem=cert_pem,
            key_pem=cert_key,
            chain_pem=chain_pem,
            san_cnf=san_cnf,
        )

    def _is_cert_valid(self, cert_pem: Path, min_remaining_days: int) -> bool:
        if not cert_pem.exists():
            return False
        try:
            out = subprocess.check_output(
                [
                    "openssl", "x509", "-in", str(cert_pem),
                    "-noout", "-enddate",
                ],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            # `notAfter=Jun 27 12:00:00 2036 GMT`
            not_after_str = out.split("=", 1)[1]
            not_after = datetime.datetime.strptime(
                not_after_str, "%b %d %H:%M:%S %Y %Z"
            )
            remaining = (not_after - datetime.datetime.utcnow()).days
            return remaining >= min_remaining_days
        except Exception:
            return False

    def _san_config(self) -> str:
        sans_block = "\n".join(self._sans)
        return (
            "[req]\n"
            "distinguished_name = req_distinguished_name\n"
            "req_extensions = v3_req\n"
            "prompt = no\n"
            "\n"
            "[req_distinguished_name]\n"
            f"CN = *.{self._domain}\n"
            "O = GitLab\n"
            "C = US\n"
            "\n"
            "[v3_req]\n"
            "keyUsage = digitalSignature, keyEncipherment\n"
            "extendedKeyUsage = serverAuth\n"
            "subjectAltName = @alt_names\n"
            "\n"
            "[alt_names]\n"
            f"{sans_block}\n"
        )

    def _run_openssl(self, cmd: list[str]) -> None:
        # Silence openssl's output unless it actually fails.
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"openssl command failed (rc={result.returncode}): "
                f"{' '.join(cmd)}\nstderr: {result.stderr}"
            )

    # ---------- secret application ----------

    def _apply_tls_secret(self, name: str, certs: WildcardCertPaths) -> None:
        """Materialise Secret/<name> as kubernetes.io/tls with cert+key.

        Strategy: render the Secret to YAML via `kubectl create --dry-run`,
        then `kubectl apply -f -` from stdin. This is idempotent: apply
        is a no-op when the YAML matches what's already on the server,
        and replaces the data when the cert file on disk changes.
        """
        yaml_doc = self._r.run(
            [
                "kubectl", "-n", self._ns,
                "create", "secret", "tls", name,
                "--cert", str(certs.cert_pem),
                "--key", str(certs.key_pem),
                "--dry-run=client", "-o", "yaml",
            ],
            check=True,
        ).stdout
        self._r.run(
            ["kubectl", "apply", "-n", self._ns, "-f", "-"],
            check=True,
            stdin=yaml_doc,
        )

    def _apply_ca_secret(self, certs: WildcardCertPaths) -> None:
        """`gitlab-wildcard-tls-ca` is the Opaque Secret the chart's
        bootstrap script creates with `cfssl_ca=<pem>`. We mirror the
        key name so downstream tooling (e.g. NOTES.txt's
        `kubectl get secret ... -o jsonpath='{.data.cfssl_ca}'`)
        keeps working unchanged.
        """
        yaml_doc = self._r.run(
            [
                "kubectl", "-n", self._ns,
                "create", "secret", "generic", "gitlab-wildcard-tls-ca",
                f"--from-file=cfssl_ca={certs.ca_pem}",
                "--dry-run=client", "-o", "yaml",
            ],
            check=True,
        ).stdout
        self._r.run(
            ["kubectl", "apply", "-n", self._ns, "-f", "-"],
            check=True,
            stdin=yaml_doc,
        )