"""Build the six-item Lab01 submission without repository-only files."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("src", "tests", "logs", "config")
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".tmp", ".zip", ".bak"}


def member_ids(readme: str) -> list[str]:
    """Use the README roster as the single source for the submission filename."""
    heading = re.search(r"^## (?:Nhóm|Thành viên)\s*$", readme, re.MULTILINE)
    if heading is None:
        raise ValueError("README must include a member roster section.")
    section = readme[heading.end() :].split("\n## ", 1)[0]
    members = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or cells[0] == "MSSV" or set(cells[0]) <= {"-", ":"}:
            continue
        if len(cells) != 4 or not all(cells) or not re.fullmatch(r"[0-9]+", cells[0]):
            raise ValueError("Complete the README roster: real student ID, name, GitHub and role.")
        members.append(cells[0])
    if len(members) not in (4, 5) or len(set(members)) != len(members):
        raise ValueError("README must list four or five members with distinct student IDs.")
    return members


def submission_files(root: Path):
    """Select files explicitly; never follow symlinks outside the submission."""
    for directory in DIRECTORIES:
        base = root / directory
        if not base.is_dir() or base.is_symlink():
            raise ValueError(f"Missing or unsafe directory: {directory}")
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(root)
            if any(part.startswith(".") or part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if any(part.endswith(".egg-info") for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"Symlink is not allowed in submission: {relative}")
            if path.is_file() and path.suffix.lower() not in EXCLUDED_SUFFIXES:
                # Grading scratch directories can contain entire nested test environments.
                if directory == "logs" and relative.parts[1] not in ("events", "results", "nodes"):
                    continue
                yield path


def build_archive(root: Path, report: Path, output_dir: Path) -> Path:
    readme = (root / "README.md").read_text(encoding="utf-8")
    ids = member_ids(readme)
    if not report.is_file() or report.is_symlink():
        raise ValueError(f"Report PDF not found: {report}")
    with report.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("REPORT.pdf must be a PDF file, not a placeholder.")

    files = list(submission_files(root))
    required = (
        "tests/run_tests.py",
        "tests/requirements.txt",
        "tests/pytest.ini",
        "config/t3_bad_sig.json",
        "tests/e2e/test_t3_invalid_signature.py",
        "tests/e2e/t3_cluster.py",
    )
    for name in required:
        if root / name not in files:
            raise ValueError(f"Required submission file missing: {name}")

    name = "Lab01_" + "_".join(ids)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{name}.zip"
    if output.exists():
        raise ValueError(f"Archive already exists; choose a new --output-dir: {output}")
    # Build atomically so validation or write errors cannot leave a half-written zip.
    with NamedTemporaryFile(dir=output_dir, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for directory in DIRECTORIES:
                archive.writestr(f"{name}/{directory}/", "")
            for path in files:
                archive.write(path, f"{name}/{path.relative_to(root).as_posix()}")
            archive.writestr(f"{name}/README.md", readme)
            archive.write(report, f"{name}/REPORT.pdf")
        temporary.rename(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "REPORT.pdf")
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        output = build_archive(ROOT, args.report.resolve(), args.output_dir.resolve())
    except (OSError, ValueError) as error:
        print(f"Packaging failed: {error}", file=sys.stderr)
        return 1
    print(f"Created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
