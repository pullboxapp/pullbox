"""Deterministic Mylar-import acceptance fixture coverage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from pullbox.core.mylar3_reader import Mylar3Reader
from scripts.iu9_acceptance_fixtures.mylar import generate_mylar_fixture
from scripts.iu9_acceptance_fixtures.shared import RAR3_SIGNATURE, RAR5_SIGNATURE

if TYPE_CHECKING:
    from pathlib import Path


def _manifest(root: Path) -> dict[str, object]:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _table_names(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }


def _write_cbr_seeds(seed_dir: Path) -> dict[str, str]:
    seed_dir.mkdir()
    rows: list[dict[str, object]] = []
    digests: dict[str, str] = {}
    for seed_id, filename, payload in (
        ("rar3", "iu9-rar3.cbr", RAR3_SIGNATURE + b"mylar-rar3" * 8),
        ("rar5", "iu9-rar5.cbr", RAR5_SIGNATURE + b"mylar-rar5" * 8),
    ):
        (seed_dir / filename).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        digests[seed_id] = digest
        rows.append(
            {
                "id": seed_id,
                "filename": filename,
                "archive_format": seed_id,
                "sha256": digest,
                "source_url": f"https://example.invalid/{filename}",
                "license": "CC0-1.0",
                "expected_members": ["001.png"],
            }
        )
    (seed_dir / "cbr-seeds.json").write_text(
        json.dumps({"schema_version": 1, "seeds": rows}),
        encoding="utf-8",
    )
    return digests


def test_mylar_fixture_is_deterministic_and_covers_path_resolution_cases(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    generate_mylar_fixture(first, seed=1300)
    generate_mylar_fixture(second, seed=1300)

    first_manifest = _manifest(first)
    second_manifest = _manifest(second)
    assert first_manifest == second_manifest
    assert first_manifest["fixture_kind"] == "iu9-mylar-import"
    assert first_manifest["seed"] == 1300
    assert first_manifest["runtime_mounts"] == [
        {
            "container_path": "/iu9/mylar-a",
            "root_id": "mylar-a",
            "source_relative": "roots/library-a",
        },
        {
            "container_path": "/iu9/mylar-b",
            "root_id": "mylar-b",
            "source_relative": "roots/library-b",
        },
        {
            "container_path": "/iu9/story-arcs",
            "root_id": "story-arcs",
            "source_relative": "roots/story-arcs",
        },
    ]
    assert first_manifest["suggested_path_maps"] == [
        {
            "source_prefix": "/legacy/comics-b",
            "target_prefix": "/iu9/mylar-b",
        }
    ]

    cases = {case["id"]: case for case in first_manifest["cases"]}
    assert {
        "identity_root",
        "explicit_path_mapping",
        "missing_comic_location_absolute_issue",
        "split_series",
        "missing_root",
        "duplicate_comic_id",
        "malformed_comic_id",
        "annual_release_identity",
        "weird_issue_numbers",
        "story_arc_full",
        "readlist_full",
        "optional_tables_absent",
        "optional_tables_empty",
        "legacy_storyarc_table",
    } <= cases.keys()

    with sqlite3.connect(first / "mylar.db") as connection:
        rows = {
            name: location
            for name, location in connection.execute(
                "SELECT ComicName, ComicLocation FROM comics ORDER BY rowid"
            )
        }
        assert rows["Identity Series"] == "/iu9/mylar-a/DC Comics/Identity Series (2020)"
        assert rows["Mapped Series"] == "/legacy/comics-b/Image/Mapped Series (2021)"
        assert rows["No Location Series"] is None
        absolute_issue = connection.execute(
            "SELECT Location FROM issues WHERE ComicName = 'No Location Series'"
        ).fetchone()[0]
        assert absolute_issue.startswith("/iu9/mylar-b/")
        split_locations = {
            row[0]
            for row in connection.execute(
                "SELECT Location FROM issues WHERE ComicName = 'Split Series'"
            )
        }
        assert any(location.startswith("/iu9/mylar-b/") for location in split_locations)
        assert any(not location.startswith("/") for location in split_locations)


def test_mylar_fixture_has_storyarc_readlist_and_schema_variants(tmp_path: Path) -> None:
    root = tmp_path / "mylar-fixture"
    generate_mylar_fixture(root, seed=55)

    with sqlite3.connect(root / "mylar.db") as connection:
        issue_numbers = {row[0] for row in connection.execute("SELECT Issue_Number FROM issues")}
        assert {"0", ".5", "0.5", "1/2", "1A", "10000", "1000000"} <= issue_numbers
        assert connection.execute("SELECT COUNT(*) FROM storyarcs").fetchone()[0] >= 3
        assert connection.execute("SELECT COUNT(*) FROM readlist").fetchone()[0] >= 2

    absent = root / "variants" / "optional-tables-absent.db"
    empty = root / "variants" / "optional-tables-empty.db"
    legacy = root / "variants" / "legacy-storyarcs.db"
    assert {"storyarcs", "readlist"}.isdisjoint(_table_names(absent))
    assert {"storyarcs", "readlist"} <= _table_names(empty)
    assert {"storyarcs", "readlist"} <= _table_names(legacy)
    with sqlite3.connect(empty) as connection:
        assert connection.execute("SELECT COUNT(*) FROM storyarcs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM readlist").fetchone()[0] == 0
    with sqlite3.connect(legacy) as connection:
        legacy_columns = {row[1] for row in connection.execute("PRAGMA table_info(storyarcs)")}
    assert legacy_columns == {"StoryArcID", "StoryArc", "ReadingOrder", "IssueNumber"}


def test_mylar_fixture_omits_private_data_and_metroninfo(tmp_path: Path) -> None:
    root = tmp_path / "mylar-fixture"
    generate_mylar_fixture(root, seed=91)
    manifest_text = (root / "manifest.json").read_text(encoding="utf-8")

    assert "/Users/" not in manifest_text
    assert "/mnt/tank" not in manifest_text
    assert "metroninfo" not in manifest_text.casefold()
    assert "metroninfo.xml" not in {path.name.casefold() for path in root.rglob("*")}
    config = (root / "config.ini").read_text(encoding="utf-8")
    assert "API_KEY" not in config
    assert "STORYARC_LOCATION = /iu9/story-arcs" in config


@pytest.mark.asyncio
async def test_main_mylar_fixture_is_readable_through_the_product_reader(tmp_path: Path) -> None:
    root = tmp_path / "mylar-fixture"
    generate_mylar_fixture(root, seed=1300)
    path_map = {
        "/iu9/mylar-a": str(root / "roots" / "library-a"),
        "/iu9/mylar-b": str(root / "roots" / "library-b"),
        "/legacy/comics-b": str(root / "roots" / "library-b"),
        "/iu9/story-arcs": str(root / "roots" / "story-arcs"),
    }

    snapshot = await Mylar3Reader(
        root / "mylar.db",
        path_map=path_map,
        config_path=root / "config.ini",
        include_missing_files=True,
    ).read_snapshot()

    discovered = {series.raw_series_name: series for series in snapshot.series}
    assert {"Identity Series", "Mapped Series", "No Location Series", "Split Series"} <= set(
        discovered
    )
    assert discovered["Identity Series"].file_count == 2
    assert discovered["Mapped Series"].file_count == 1
    assert discovered["Split Series"].file_count == 2
    assert any(file.issue_number_raw == "1000000" for file in discovered["Number Lab"].files)
    assert snapshot.storyarcs_present is True
    assert {arc.name for arc in snapshot.story_arcs} == {
        "Fixture Crisis",
        "Duplicate Order Arc",
    }
    assert snapshot.readlist_present is True
    assert snapshot.readlist_count == 2


def test_mylar_fixture_adds_mapped_genuine_cbr_conversion_cases(tmp_path: Path) -> None:
    seed_dir = tmp_path / "cbr-seeds"
    digests = _write_cbr_seeds(seed_dir)
    root = tmp_path / "mylar-fixture"

    generate_mylar_fixture(
        root,
        seed=1300,
        cbr_seed_dir=seed_dir,
        cbr_expected_sha256=digests,
    )

    manifest = _manifest(root)
    cases = {case["id"]: case for case in manifest["cases"]}
    assert manifest["cbr_seed_set"]["status"] == "provided"
    assert cases["mapped_genuine_cbr_conversion"]["expected_outcome"] == "success"
    assert cases["mapped_genuine_cbr_conversion"]["expected_materialization"] == "cbz"
    with sqlite3.connect(root / "mylar.db") as connection:
        location = connection.execute(
            "SELECT ComicLocation FROM comics WHERE ComicName = 'Mapped CBR Series'"
        ).fetchone()[0]
        issue_locations = {
            row[0]
            for row in connection.execute(
                "SELECT Location FROM issues WHERE ComicName = 'Mapped CBR Series'"
            )
        }
    assert location == "/legacy/comics-b/Fixture House/Mapped CBR Series (2025)"
    assert issue_locations == {
        "Mapped CBR Series 001 (2025).cbr",
        "Mapped CBR Series 002 (2025).cbr",
    }
    cbr_root = root / "roots/library-b/Fixture House/Mapped CBR Series (2025)"
    assert (cbr_root / "Mapped CBR Series 001 (2025).cbr").read_bytes().startswith(RAR3_SIGNATURE)
    assert (cbr_root / "Mapped CBR Series 002 (2025).cbr").read_bytes().startswith(RAR5_SIGNATURE)
