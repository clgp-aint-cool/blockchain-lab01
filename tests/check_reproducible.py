"""T8 Reproducibility checker: verify byte-identical result outputs across runs."""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path


def compare_result_dirs(dir1: str | Path, dir2: str | Path) -> tuple[bool, list[str]]:
    """Compare all result files between two directories for byte-identical reproducibility.

    Args:
        dir1: path to first results directory.
        dir2: path to second results directory.

    Returns:
        (identical: bool, diffs: list of strings describing mismatches).
    """
    p1 = Path(dir1)
    p2 = Path(dir2)

    if not p1.is_dir():
        raise FileNotFoundError(f"directory not found: {p1}")
    if not p2.is_dir():
        raise FileNotFoundError(f"directory not found: {p2}")

    files1 = {str(f.relative_to(p1)): f for f in p1.rglob("*.json")}
    files2 = {str(f.relative_to(p2)): f for f in p2.rglob("*.json")}

    diffs: list[str] = []

    # Check for missing/extra files
    only_in_1 = sorted(set(files1.keys()) - set(files2.keys()))
    only_in_2 = sorted(set(files2.keys()) - set(files1.keys()))

    for name in only_in_1:
        diffs.append(f"File {name} only present in {p1}")
    for name in only_in_2:
        diffs.append(f"File {name} only present in {p2}")

    # Byte-compare common files
    common = sorted(set(files1.keys()) & set(files2.keys()))
    if not common and not diffs:
        diffs.append(f"No result files (*.json) found in {p1} or {p2}")
        return False, diffs

    for name in common:
        b1 = files1[name].read_bytes()
        b2 = files2[name].read_bytes()
        if b1 != b2:
            try:
                t1 = files1[name].read_text(encoding="utf-8").splitlines(keepends=True)
                t2 = files2[name].read_text(encoding="utf-8").splitlines(keepends=True)
                text_diff = "".join(
                    difflib.unified_diff(
                        t1, t2, fromfile=f"{p1.name}/{name}", tofile=f"{p2.name}/{name}"
                    )
                )
                diffs.append(f"Mismatch in {name}:\n{text_diff}")
            except Exception:
                diffs.append(f"Byte mismatch in binary content: {name}")

    identical = len(diffs) == 0
    return identical, diffs


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for check_reproducible."""
    parser = argparse.ArgumentParser(
        description="Verify that two simulation result directories are byte-identical (T8)."
    )
    parser.add_argument(
        "dirs",
        nargs="*",
        help="Two directories to compare (e.g. logs/run1/t1_normal logs/run2/t1_normal).",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help="Scenario name to compare across --run1 and --run2.",
    )
    parser.add_argument(
        "--run1",
        default=None,
        help="First results root directory (used with --scenario).",
    )
    parser.add_argument(
        "--run2",
        default=None,
        help="Second results root directory (used with --scenario).",
    )

    args = parser.parse_args(argv)

    if len(args.dirs) == 2:
        dir1, dir2 = Path(args.dirs[0]), Path(args.dirs[1])
    elif args.scenario and args.run1 and args.run2:
        dir1 = Path(args.run1) / args.scenario
        dir2 = Path(args.run2) / args.scenario
    else:
        parser.print_help(sys.stderr)
        return 2

    try:
        identical, diffs = compare_result_dirs(dir1, dir2)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if identical:
        print(f"OK: {dir1} and {dir2} are byte-identical")
        return 0
    else:
        print(f"FAIL: {dir1} and {dir2} differ:", file=sys.stderr)
        for d in diffs:
            print(f"  {d}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
