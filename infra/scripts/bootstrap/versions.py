"""Pinned versions, loaded once from VERSIONS.json.

This is the *single source of truth* for every version used by the
bootstrap. Each helper class reads from here — no class owns its own
hardcoded version string.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Re-exported for convenience: callers that want dict-like access can
# do `from bootstrap.versions import VERSIONS`. It is populated by
# load_versions() at app boot.
VERSIONS: dict[str, Any] = {}


def load_versions(path: Path) -> dict[str, Any]:
    """Load VERSIONS.json from disk into the module-level VERSIONS dict."""
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    VERSIONS.clear()
    VERSIONS.update(data)
    return data


def helm_repo(name: str) -> dict[str, Any]:
    """Lookup helper for the `helm_repositories` section."""
    repos = VERSIONS.get("helm_repositories", {})
    if name not in repos:
        raise KeyError(f"helm repo '{name}' not declared in VERSIONS.json")
    return repos[name]


def tool_pin(name: str) -> dict[str, Any]:
    """Lookup helper for the `tools` section."""
    tools = VERSIONS.get("tools", {})
    if name not in tools:
        raise KeyError(f"tool '{name}' not declared in VERSIONS.json")
    return tools[name]


def kindest_image() -> str:
    return VERSIONS["kubernetes"]["kindest_node_image"]