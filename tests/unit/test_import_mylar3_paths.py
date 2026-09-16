"""Tests for Mylar3 import path-map helpers."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from pullbox.services.import_mylar3_paths import auto_detect_mylar3_path_map
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from pathlib import Path


def _create_mylar_db(db_path: Path, comic_location: str | list[str] | None) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE comics (ComicLocation TEXT)")
    if isinstance(comic_location, list):
        conn.executemany("INSERT INTO comics VALUES (?)", [(item,) for item in comic_location])
    elif comic_location is not None:
        conn.execute("INSERT INTO comics VALUES (?)", (comic_location,))
    conn.commit()
    conn.close()


def test_auto_detect_mylar3_path_map_matches_nearby_directory(tmp_path: Path) -> None:
    config_dir = tmp_path / "mylar3"
    config_dir.mkdir()
    db_path = config_dir / "mylar.db"
    host_comics = tmp_path / "comics"
    (host_comics / "Absolute Wonder Woman (2024)").mkdir(parents=True)
    _create_mylar_db(db_path, "/comics/Absolute Wonder Woman (2024)")

    assert auto_detect_mylar3_path_map(db_path) == {"/comics": str(host_comics)}


def test_auto_detect_mylar3_path_map_prefers_existing_identity_over_alias(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "imports"
    config_dir.mkdir()
    db_path = config_dir / "mylar.db"
    identity_root = tmp_path / "primary" / "comics"
    identity_series = identity_root / "Absolute Wonder Woman (2024)"
    identity_series.mkdir(parents=True)
    alias_root = tmp_path / "comics"
    (alias_root / identity_series.name).mkdir(parents=True)
    _create_mylar_db(db_path, str(identity_series))

    assert auto_detect_mylar3_path_map(db_path) is None


def test_auto_detect_mylar3_path_map_prefers_nested_comics_mount(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "imports"
    config_dir.mkdir()
    db_path = config_dir / "mylar.db"
    host_comics = tmp_path / "comics"
    (host_comics / "Absolute Batman (2024)").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    _create_mylar_db(db_path, "/data/comics/Absolute Batman (2024)")

    assert auto_detect_mylar3_path_map(db_path) == {"/data/comics": str(host_comics)}


def test_auto_detect_mylar3_path_map_rejects_self_map_without_translated_series(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "imports"
    config_dir.mkdir()
    db_path = config_dir / "mylar.db"
    (tmp_path / "data").mkdir()
    _create_mylar_db(db_path, "/data/comics/Absolute Batman (2024)")

    assert auto_detect_mylar3_path_map(db_path) is None


def test_auto_detect_mylar3_path_map_uses_later_existing_location(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "imports"
    config_dir.mkdir()
    db_path = config_dir / "mylar.db"
    host_comics = tmp_path / "comics"
    (host_comics / "Absolute Flash (2025)").mkdir(parents=True)
    _create_mylar_db(
        db_path,
        [
            "/data/comics/Missing Series (2024)",
            "/data/comics/Absolute Flash (2025)",
        ],
    )

    assert auto_detect_mylar3_path_map(db_path) == {"/data/comics": str(host_comics)}


def test_auto_detect_mylar3_path_map_collects_independent_prefixes(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "imports"
    config_dir.mkdir()
    db_path = config_dir / "mylar.db"
    primary_host = tmp_path / "primary-comics"
    archive_host = tmp_path / "archive-comics"
    (primary_host / "Batman (2011)").mkdir(parents=True)
    (archive_host / "Saga (2012)").mkdir(parents=True)
    _create_mylar_db(
        db_path,
        [
            "/primary-comics/Batman (2011)",
            "/archive-comics/Saga (2012)",
        ],
    )

    assert auto_detect_mylar3_path_map(db_path) == {
        "/archive-comics": str(archive_host),
        "/primary-comics": str(primary_host),
    }


def test_auto_detect_mylar3_path_map_rejects_ambiguous_alias_candidates(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "imports"
    config_dir.mkdir()
    db_path = config_dir / "mylar.db"
    series_name = "Batman (2011)"
    (config_dir / "comics" / series_name).mkdir(parents=True)
    (tmp_path / "comics" / series_name).mkdir(parents=True)
    _create_mylar_db(db_path, f"/data/comics/{series_name}")

    assert auto_detect_mylar3_path_map(db_path) is None


def test_auto_detect_mylar3_path_map_returns_none_for_empty_comics_table(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    _create_mylar_db(db_path, None)

    assert auto_detect_mylar3_path_map(db_path) is None


def test_auto_detect_mylar3_path_map_returns_none_for_single_segment_location(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    _create_mylar_db(db_path, "comics")

    assert auto_detect_mylar3_path_map(db_path) is None


def test_auto_detect_mylar3_path_map_returns_none_for_invalid_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    db_path.write_text("not sqlite")

    assert auto_detect_mylar3_path_map(db_path) is None


def test_import_service_mylar3_path_map_shim_remains_available(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    _create_mylar_db(db_path, "/comics")

    assert ImportService._auto_detect_mylar3_path_map(db_path) is None
