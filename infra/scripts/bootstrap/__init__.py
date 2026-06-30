"""Bootstrap package.

A SOLID refactor of the original bootstrap.py into single-responsibility
classes. The package owns the host-side orchestration for Phase 1 (and is
designed to grow into Phase 2 without growing into a single module).

Public entry points:
  - BootstrapApp:  composition root that wires and runs the pipeline.
  - VERSIONS:      the pinned-version source of truth (dict + load()).
"""

from __future__ import annotations

from .app import BootstrapApp
from .paths import Paths
from .versions import VERSIONS, load_versions

__all__ = ["BootstrapApp", "Paths", "VERSIONS", "load_versions"]