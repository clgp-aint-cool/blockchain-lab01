"""T8 Reproducibility checker: verify byte-identical result outputs across runs."""

from __future__ import annotations

import sys

from l1sim.verification.check_reproducible import compare_result_dirs, main

__all__ = ["compare_result_dirs", "main"]

if __name__ == "__main__":
    sys.exit(main())
