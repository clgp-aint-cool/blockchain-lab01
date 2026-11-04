#!/usr/bin/env python3
"""Root CLI entry point for launch_local."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from l1sim.launch_local import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
