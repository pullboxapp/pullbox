"""Only explicitly synthetic fixtures may be copied into multipage benchmarks."""

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from scripts.import_scale_fixtures.mylar_multipage import upgrade_fixture
from scripts.iu9_acceptance_fixtures.shared import create_deterministic_cbz, sha256_file


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "original"
    source.mkdir()
    (source / "generation-report.json").write_text(
        json.dumps(
            {
                "fixture_kind": "mylar-import-from-cv-stress-manifest",
                "recorded_source_root": "/imports/original/source",
                "series_count": 1,
                "issue_count": 4,
            }
        )
    )
    with sqlite3.connect(source / "mylar.db") as db:
        db.executescript(
            "CREATE TABLE comics(ComicID TEXT, ComicLocation TEXT);"
            "CREATE TABLE issues(IssueID TEXT, ComicID TEXT, Location TEXT, ComicSize TEXT);"
        )
        db.execute("INSERT INTO comics VALUES ('1','/imports/original/source/Test')")
        for number in range(1, 5):
            path = source / "source" / "Test" / f"{number}.cbz"
            create_deterministic_cbz(
                path, seed=1, case_id=str(number), series="Test", number=str(number), page_count=1
            )
            db.execute(
                "INSERT INTO issues VALUES (?, '1', ?, ?)",
                (str(number), path.name, str(path.stat().st_size)),
            )
    return source


def test_upgrade_preserves_original_and_builds_explicit_multipage_and_safety_cases(tmp_path):
    source = _source(tmp_path)
    before = {
        str(path.relative_to(source)): sha256_file(path)
        for path in source.rglob("*")
        if path.is_file()
    }
    output = tmp_path / "new"
    result = upgrade_fixture(
        source, output, container_root="/imports/new/source", pages=32, single_page_every=3
    )
    assert result["issue_count"] == 4
    assert result["single_page_files"] == 1
    assert result["sqlite_integrity_check"] == "ok"
    with sqlite3.connect(output / "mylar.db") as db:
        assert (
            db.execute("SELECT ComicLocation FROM comics").fetchone()[0]
            == "/imports/new/source/Test"
        )
        for number, size in db.execute("SELECT IssueID, ComicSize FROM issues"):
            path = output / "source" / "Test" / f"{number}.cbz"
            assert int(size) == path.stat().st_size
            with zipfile.ZipFile(path) as archive:
                assert archive.testzip() is None
                assert len([n for n in archive.namelist() if n.endswith(".png")]) == (
                    1 if number == "3" else 32
                )
                with zipfile.ZipFile(source / "source" / "Test" / f"{number}.cbz") as old:
                    assert archive.read("ComicInfo.xml") == old.read("ComicInfo.xml")
    assert before == {
        str(path.relative_to(source)): sha256_file(path)
        for path in source.rglob("*")
        if path.is_file()
    }
    with pytest.raises(FileExistsError):
        upgrade_fixture(source, output, container_root="/imports/new/source")


@pytest.mark.parametrize("unsafe", ["outside", "symlink", "not_synthetic"])
def test_upgrade_rejects_non_fixture_or_escaping_sources_without_output(tmp_path, unsafe):
    source = _source(tmp_path)
    if unsafe == "not_synthetic":
        (source / "generation-report.json").write_text('{"fixture_kind":"real-library"}')
    elif unsafe == "symlink":
        original = source / "source" / "Test" / "1.cbz"
        link = source / "source" / "Test" / "2.cbz"
        link.unlink()
        link.symlink_to(original)
    else:
        with sqlite3.connect(source / "mylar.db") as db:
            db.execute("UPDATE issues SET Location='../outside.cbz' WHERE IssueID='1'")
    with pytest.raises(ValueError):
        upgrade_fixture(source, tmp_path / "new", container_root="/imports/new/source")
    assert not (tmp_path / "new").exists()
