#!/usr/bin/env python3
"""Thin compatibility shim for the legacy monolithic bootstrap.py.

The real implementation lives in the `bootstrap/` package. This file is
kept so any script or doc that already runs
`python3 infra/scripts/bootstrap.py ...` continues to work.

New code should prefer:
    python3 -m bootstrap ...        # uses bootstrap/__main__.py
    or:
    from bootstrap import BootstrapApp
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# Make sure we import the package next to this file, not anything
# previously installed site-wide.
sys.path.insert(0, str(_HERE))

from bootstrap.app import BootstrapApp  # noqa: E402


def main() -> int:
    app = BootstrapApp.from_argv(_HERE / "bootstrap", sys.argv[1:])
    return app.run()


if __name__ == "__main__":
    sys.exit(main())