"""Single source of derived filesystem paths (SRP, immutable)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """Resolved paths used by every other class. Construct once, share freely."""

    script_dir: Path      # .../blueprint/infra/scripts
    bootstrap_dir: Path   # .../blueprint/infra/scripts/bootstrap
    blueprint_dir: Path   # .../blueprint
    infra_dir: Path       # .../blueprint/infra
    tofu_dir: Path        # .../blueprint/infra/tofu
    tls_dir: Path         # .../blueprint/infra/tls
    tls_private: Path     # .../blueprint/infra/tls/private
    tls_public: Path      # .../blueprint/infra/tls/public
    helm_charts_dir: Path  # .../blueprint/infra/helm-charts (flat, no subdir)
    # Phase 2 paths
    secrets_dir: Path          # .../blueprint/infra/secrets  (gitignored)
    phase2_refs_dir: Path      # .../blueprint/infra/scripts/bootstrap/phase2/references
    # PersistentVolume backing (hostPath on each kind node).
    # infra/data/  — gitignored; tofu binds <root>/shared onto every node
    # at /var/local/shared. The Phase 2 bootstrap wires rancher/
    # local-path-provisioner on top so chart-managed PVCs each get a
    # sub-directory under infra/data/shared/.
    data_dir: Path             # .../blueprint/infra/data
    data_shared: Path          # .../blueprint/infra/data/shared

    @classmethod
    def from_bootstrap_dir(cls, bootstrap_dir: Path) -> "Paths":
        bootstrap_dir = bootstrap_dir.resolve()
        infra = bootstrap_dir.parent.parent
        return cls(
            script_dir=bootstrap_dir.parent,
            bootstrap_dir=bootstrap_dir,
            blueprint_dir=infra.parent,
            infra_dir=infra,
            tofu_dir=infra / "tofu",
            tls_dir=infra / "tls",
            tls_private=infra / "tls" / "private",
            tls_public=infra / "tls" / "public",
            helm_charts_dir=infra / "helm-charts",
            secrets_dir=infra / "secrets",
            phase2_refs_dir=bootstrap_dir / "phase2" / "references",
            data_dir=infra / "data",
            data_shared=infra / "data" / "shared",
        )

    def ensure_dirs(self) -> None:
        for d in (self.tls_private, self.tls_public, self.helm_charts_dir, self.data_shared):
            d.mkdir(parents=True, exist_ok=True)

    def ensure_secrets_dir(self) -> None:
        """Create the secrets dir with restrictive perms (Phase 2 PKI material).

        Mode 0o700 because OpenBao's unseal key lives here.
        """
        import os
        os.makedirs(self.secrets_dir, mode=0o700, exist_ok=True)