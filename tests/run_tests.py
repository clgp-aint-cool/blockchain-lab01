"""Cross-platform grading entry point; run from any working directory."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {
    "T1": "tests/e2e/test_local_nodes.py",
    "T2": "tests/e2e/test_t2_dup_reorder.py",
    "T3": "tests/e2e/test_t3_invalid_signature.py",
    "T4": "tests/e2e/test_t4_replay_tx.py",
    "T5": "tests/e2e/test_t5_pre_gst.py",
    "T6": ("tests/e2e/test_byzantine_scenarios.py::test_t6_silent_proposer_is_replaced_in_round_1"),
    "T7": (
        "tests/e2e/test_byzantine_scenarios.py::test_t7_equivocators_are_caught_and_results_reproduce"
    ),
    "T8": "tests/e2e/test_t8_reproducible.py",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--scenario", nargs="+", type=str.upper, choices=SCENARIOS, help="Run selected T1-T8 tests"
    )
    selection.add_argument("--unit", action="store_true", help="Run unit tests only")
    selection.add_argument("--e2e", action="store_true", help="Run all end-to-end tests")
    parser.add_argument(
        "pytest_args", nargs=argparse.REMAINDER, help="Extra pytest options after -- (e.g. -- -v)"
    )
    args = parser.parse_args(argv)

    missing = [
        name for name in ("pytest", "cryptography") if importlib.util.find_spec(name) is None
    ]
    if missing:
        print(
            f"Missing dependencies: {', '.join(missing)}. From the repository root, run:\n"
            "  python -m pip install -r tests/requirements.txt",
            file=sys.stderr,
        )
        return 1

    if args.scenario:
        targets = list(dict.fromkeys(SCENARIOS[name] for name in args.scenario))
    elif args.unit:
        targets = ["tests/unit"]
    elif args.e2e:
        targets = ["tests/e2e"]
    else:
        targets = ["tests"]

    extra = args.pytest_args
    if extra[:1] == ["--"]:
        extra = extra[1:]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT / "src"), str(ROOT), env.get("PYTHONPATH", "")))
    )
    env["PYTHONUTF8"] = "1"
    command = [sys.executable, "-m", "pytest", *targets, *extra]
    print(f"Running: {' '.join(targets)}", flush=True)
    try:
        return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
