#!/usr/bin/env python3
"""
Phase 1 PKI helper.

Generates:
  - A self-signed RSA-4096 root CA (10y)
  - A wildcard leaf cert for *.local.bruj0.net signed by that CA
  - A SAN list covering the bare domain and the planned Phase 2 hostnames

No external Python deps — we shell out to openssl. That keeps the helper
auditable and matches the user's toolchain (openssl ships everywhere).

Outputs:
  <private-dir>/ca.crt                # public CA cert
  <private-dir>/ca.key                # CA private key
  <private-dir>/_.<domain>.crt        # wildcard leaf cert
  <private-dir>/_.<domain>.key        # wildcard leaf key
  <public-dir>/ca.crt                 # copy of CA cert (non-sensitive)

Re-running is safe: existing files are overwritten (idempotent from the
cluster's perspective — the leaf key never changes, just the certs).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_DOMAIN = "local.bruj0.net"
PHASE2_HOSTS = ["gitlab.local.bruj0.net", "traefik.local.bruj0.net", "openbao.local.bruj0.net"]
CA_DAYS = 365 * 10
LEAF_DAYS = 365


def run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def write_openssl_cnf(path: Path, *, common_name: str, sans: list[str], is_ca: bool) -> None:
    """Render a minimal openssl v3 ext config inline (no template files)."""
    san_lines = "\n".join(f"DNS.{i + 1} = {s}" for i, s in enumerate(sans))
    basic = "critical, digitalSignature, keyCertSign, cRLSign" if is_ca else "critical, digitalSignature, keyEncipherment"
    key_usage = (
        "keyUsage = critical, keyCertSign, cRLSign\nextendedKeyUsage = clientAuth"
        if is_ca
        else "keyUsage = critical, digitalSignature, keyEncipherment\nextendedKeyUsage = serverAuth, clientAuth"
    )
    if is_ca:
        ext = f"""[req]
distinguished_name = dn
prompt = no
[dn]
CN = {common_name}
[v3_ca]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints = critical, CA:TRUE, pathlen:1
{key_usage}
"""
    else:
        ext = f"""[req]
distinguished_name = dn
prompt = no
[dn]
CN = {common_name}
[v3_leaf]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
basicConstraints = critical, CA:FALSE
{key_usage}
subjectAltName = @san
[san]
{san_lines}
"""
    path.write_text(ext)


def gen_ca(private_dir: Path, cn: str = "bruj0-local-dev Root CA") -> tuple[Path, Path]:
    private_dir.mkdir(parents=True, exist_ok=True)
    key = private_dir / "ca.key"
    crt = private_dir / "ca.crt"
    cnf = private_dir / "ca.cnf"
    write_openssl_cnf(cnf, common_name=cn, sans=[cn], is_ca=True)
    run(["openssl", "genrsa", "-out", str(key), "4096"])
    run(["openssl", "req", "-x509", "-new", "-nodes",
         "-key", str(key), "-days", str(CA_DAYS),
         "-config", str(cnf), "-extensions", "v3_ca",
         "-out", str(crt)])
    return crt, key


def gen_wildcard(domain: str, private_dir: Path, ca_crt: Path, ca_key: Path) -> tuple[Path, Path]:
    cn = f"*.{domain}"
    sans = [f"*.{domain}", domain, *PHASE2_HOSTS]
    key = private_dir / f"_.{domain}.key"
    crt = private_dir / f"_.{domain}.crt"
    csr = private_dir / f"_.{domain}.csr"
    cnf = private_dir / f"_.{domain}.cnf"
    write_openssl_cnf(cnf, common_name=cn, sans=sans, is_ca=False)
    run(["openssl", "genrsa", "-out", str(key), "2048"])
    run(["openssl", "req", "-new", "-key", str(key), "-out", str(csr), "-config", str(cnf)])
    run(["openssl", "x509", "-req", "-in", str(csr),
         "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
         "-days", str(LEAF_DAYS),
         "-extfile", str(cnf), "-extensions", "v3_leaf",
         "-out", str(crt)])
    return crt, key


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint local CA + wildcard leaf for Phase 1.")
    ap.add_argument("--domain", default=DEFAULT_DOMAIN)
    ap.add_argument("--private-dir", required=True, type=Path)
    ap.add_argument("--public-dir", required=True, type=Path)
    args = ap.parse_args()

    args.private_dir.mkdir(parents=True, exist_ok=True)
    args.public_dir.mkdir(parents=True, exist_ok=True)

    ca_crt, ca_key = gen_ca(args.private_dir)
    print(f"CA cert  : {ca_crt}")
    print(f"CA key   : {ca_key}")

    leaf_crt, leaf_key = gen_wildcard(args.domain, args.private_dir, ca_crt, ca_key)
    print(f"Leaf cert: {leaf_crt}")
    print(f"Leaf key : {leaf_key}")

    # Copy CA cert to the public side. Leaf key is NEVER copied.
    public_ca = args.public_dir / "ca.crt"
    shutil.copyfile(ca_crt, public_ca)
    os.chmod(public_ca, 0o644)
    print(f"Public CA: {public_ca}")

    # Echo a tiny validity summary
    print()
    print(subprocess.run(["openssl", "x509", "-in", str(leaf_crt), "-noout",
                          "-subject", "-issuer", "-dates", "-ext", "subjectAltName"],
                         capture_output=True, text=True).stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())