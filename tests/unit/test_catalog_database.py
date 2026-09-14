"""Reject invalid catalogs and reproduce cumulative updates from the weekly base."""

import sqlite3

import pytest

from pullbox.services.catalog.contract import CatalogError
from pullbox.services.catalog.database import apply_catalog_patch, validate_snapshot
from tests.catalog_fixtures import build_patch, build_snapshot


def test_validates_snapshot_content_and_identity(tmp_path):
    path = tmp_path / "base.db"
    expected = build_snapshot(path)
    result = validate_snapshot(path, "20260913T050000Z")
    assert result == expected


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE series SET name='corrupt'",
        "PRAGMA application_id=0",
        "PRAGMA user_version=99",
        "DELETE FROM series_fts",
        "CREATE TABLE private_payload(secret TEXT)",
        "UPDATE issues SET series_id=999",
        "CREATE TRIGGER extra AFTER INSERT ON series BEGIN DELETE FROM issues; END",
    ],
)
def test_rejects_invalid_snapshot(tmp_path, sql):
    path = tmp_path / "base.db"
    build_snapshot(path)
    with sqlite3.connect(path) as db:
        db.execute(sql)
    with pytest.raises(CatalogError):
        validate_snapshot(path, "20260913T050000Z")


def test_rejects_symlink_snapshot(tmp_path):
    base = tmp_path / "base.db"
    build_snapshot(base)
    link = tmp_path / "link.db"
    link.symlink_to(base)
    with pytest.raises(CatalogError):
        validate_snapshot(link, "20260913T050000Z")


def test_daily_updates_rebuild_from_base_including_reverted_changes(tmp_path):
    base, day1, day2 = (tmp_path / name for name in ("base.db", "day1.db", "day2.db"))
    build_snapshot(base)
    build_snapshot(day1, "20260914T050000Z", "Changed", extra_issue=True)
    expected = build_snapshot(day2, "20260915T050000Z")
    for target, version in ((day1, "20260914T050000Z"), (day2, "20260915T050000Z")):
        patch, output = tmp_path / f"{version}.patch", tmp_path / f"{version}.db"
        build_patch(patch, base, target)
        apply_catalog_patch(base, patch, output, "20260913T050000Z", version)
        assert output.exists()
    assert validate_snapshot(output, "20260915T050000Z") == expected
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT name FROM series").fetchone()[0] == "Batman"
        assert db.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 1


def test_wrong_base_fails_without_modifying_it(tmp_path):
    base, target, patch = (tmp_path / name for name in ("base.db", "target.db", "patch.db"))
    build_snapshot(base)
    build_snapshot(target, "20260914T050000Z")
    build_patch(patch, base, target)
    original = base.read_bytes()
    with pytest.raises(CatalogError):
        apply_catalog_patch(
            base, patch, tmp_path / "out.db", "20260912T050000Z", "20260914T050000Z"
        )
    assert base.read_bytes() == original
