#!/usr/bin/env bash
# T8: run the same scenario twice and verify byte-identical result outputs.
set -eu

PYTHON="${PYTHON:-python}"
if ! command -v "$PYTHON" &>/dev/null; then
  if command -v python3 &>/dev/null; then
    PYTHON="python3"
  elif command -v py &>/dev/null; then
    PYTHON="py"
  fi
fi

SCENARIO="${1:-config/t1_normal.json}"
OUT="logs/determinism"
rm -rf "$OUT"
mkdir -p "$OUT/run1" "$OUT/run2"

echo "=== Run 1: $SCENARIO ==="
"$PYTHON" launch_local.py --scenario "$SCENARIO" --quiet
cp -r logs/results/* "$OUT/run1/"

echo "=== Run 2: $SCENARIO ==="
"$PYTHON" launch_local.py --scenario "$SCENARIO" --quiet
cp -r logs/results/* "$OUT/run2/"

echo "=== Checking T8 Reproducibility ==="
"$PYTHON" tests/check_reproducible.py "$OUT/run1" "$OUT/run2"
