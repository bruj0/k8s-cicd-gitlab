"""Host OS detection.

Returns a (family, distro_id) tuple. `family` is the bucket the rest of
the package dispatches on (`arch`, `debian`, `rhel`, `darwin`, `other`).
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Final

OSFamily = str  # 'arch' | 'debian' | 'rhel' | 'darwin' | 'other'

SUPPORTED_FAMILIES: Final[frozenset[str]] = frozenset({"arch", "debian", "rhel", "darwin"})

_DISTRO_TO_FAMILY: Final[dict[str, str]] = {
    "arch": "arch", "manjaro": "arch", "endeavouros": "arch",
    "debian": "debian", "ubuntu": "debian", "pop": "debian", "elementary": "debian", "linuxmint": "debian",
    "fedora": "rhel", "rhel": "rhel", "centos": "rhel", "rocky": "rhel", "almalinux": "rhel",
}


def detect_os() -> tuple[OSFamily, str | None]:
    """Return (family, distro_id). distro_id may be None on macOS."""
    system = platform.system().lower()
    if system == "darwin":
        return "darwin", None
    os_release = Path("/etc/os-release")
    if not os_release.exists():
        return "other", None
    info: dict[str, str] = {}
    for line in os_release.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip().strip('"')
    distro = info.get("ID", "").lower()
    return _DISTRO_TO_FAMILY.get(distro, "other"), distro


def is_supported(family: OSFamily) -> bool:
    return family in SUPPORTED_FAMILIES