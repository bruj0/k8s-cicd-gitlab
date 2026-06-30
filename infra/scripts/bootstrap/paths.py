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
        )

    def ensure_dirs(self) -> None:
        for d in (self.tls_private, self.tls_public, self.helm_charts_dir):
            d.mkdir(parents=True, exist_ok=True)

    def ensure_secrets_dir(self) -> None:
        """Create the secrets dir with restrictive perms (Phase 2 PKI material).

        Mode 0o700 because OpenBao's unseal key lives here.
        """
        import os
        os.makedirs(self.secrets_dir, mode=0o700, exist_ok=True)