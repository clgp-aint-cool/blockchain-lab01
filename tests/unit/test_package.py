"""Validate submission layout and failure cases without requiring a real report."""

from zipfile import ZipFile

import pytest

package = pytest.importorskip("scripts.package", reason="Packaging tools are repository-only")


@pytest.fixture
def submission(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    rows = [f"| 2212000{i} | Member {i} | member{i} | Role {i} |" for i in range(1, 5)]
    (root / "README.md").write_text(
        "## Nhóm\n\n| MSSV | Họ tên | GitHub | Phần chính |\n|---|---|---|---|\n" + "\n".join(rows),
        encoding="utf-8",
    )
    for name in (
        "src/l1sim/__init__.py",
        "tests/run_tests.py",
        "tests/requirements.txt",
        "tests/pytest.ini",
        "tests/e2e/test_t3_invalid_signature.py",
        "tests/e2e/t3_cluster.py",
        "config/t3_bad_sig.json",
        "logs/results/t1_normal/V0.json",
        "logs/events/t1_normal/V0.jsonl",
        "logs/scratch/nested/venv/ignored.txt",
        "src/l1sim/__pycache__/ignored.pyc",
        "src/l1sim.egg-info/PKG-INFO",
        ".git/config",
        "pyproject.toml",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    report = root / "REPORT.pdf"
    report.write_bytes(b"%PDF-1.4\n% Test fixture, not a submission report.\n")
    return root, report, tmp_path / "output"


@pytest.mark.parametrize("heading", ["Nhóm", "Thành viên"])
def test_archive_contains_exact_six_items_and_excludes_repository_artifacts(submission, heading):
    root, report, output = submission
    readme = root / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("## Nhóm", f"## {heading}"), encoding="utf-8"
    )
    result = package.build_archive(root, report, output)
    assert result.name == "Lab01_22120001_22120002_22120003_22120004.zip"
    prefix = result.stem + "/"
    with ZipFile(result) as archive:
        names = archive.namelist()
        assert all(name.startswith(prefix) for name in names)
        contents = [name.removeprefix(prefix) for name in names]
        assert {name.split("/")[0] for name in contents} == {
            "src",
            "tests",
            "logs",
            "config",
            "README.md",
            "REPORT.pdf",
        }
        assert "tests/run_tests.py" in contents
        assert "tests/requirements.txt" in contents
        assert "config/t3_bad_sig.json" in contents
        assert "logs/results/t1_normal/V0.json" in contents
        assert not any("ignored" in name or ".egg-info" in name for name in names)
        assert archive.read(prefix + "REPORT.pdf") == report.read_bytes()


def test_placeholder_roster_does_not_create_archive(submission):
    root, report, output = submission
    text = (root / "README.md").read_text(encoding="utf-8").replace("22120001", "ID1")
    (root / "README.md").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="roster"):
        package.build_archive(root, report, output)
    assert not output.exists()


@pytest.mark.parametrize("missing", [False, True])
def test_report_must_exist_and_have_pdf_header(submission, missing):
    root, report, output = submission
    if missing:
        report.unlink()
    else:
        report.write_text("TODO: report", encoding="utf-8")
    with pytest.raises(ValueError, match="PDF|pdf"):
        package.build_archive(root, report, output)
    assert not output.exists()


def test_existing_archive_is_preserved(submission):
    root, report, output = submission
    result = package.build_archive(root, report, output)
    original = result.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        package.build_archive(root, report, output)
    assert result.read_bytes() == original


def test_missing_grading_entry_point_rejects_incomplete_archive(submission):
    root, report, output = submission
    (root / "tests/run_tests.py").unlink()
    with pytest.raises(ValueError, match="tests/run_tests.py"):
        package.build_archive(root, report, output)
