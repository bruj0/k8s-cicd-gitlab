"""Allow `python3 -m bootstrap ...` invocation."""

from __future__ import annotations

import sys
from pathlib import Path

from .app import BootstrapApp


def main() -> int:
    bootstrap_dir = Path(__file__).resolve().parent
    app = BootstrapApp.from_argv(bootstrap_dir, sys.argv[1:])
    return app.run()


if __name__ == "__main__":
    sys.exit(main())