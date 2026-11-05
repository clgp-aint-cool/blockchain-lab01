"""Safety and state consistency verification for L1 simulation results."""

from __future__ import annotations

import sys

from l1sim.verification.check_safety import check_safety, check_safety_for_nodes, main

__all__ = ["check_safety", "check_safety_for_nodes", "main"]

if __name__ == "__main__":
    sys.exit(main())
