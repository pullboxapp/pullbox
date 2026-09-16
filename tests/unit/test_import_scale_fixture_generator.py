"""Deterministic Comic Vine-backed import scale fixture coverage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.import_scale_fixtures.generator import (
    FixtureRequest,
    PlannedSeries,
    _series_directory,
    generate_import_scale_fixture,
    plan_import_scale_fixture,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_catalog(path: Path, *, issue_counts: tuple[int, ...] = (8, 4, 5, 4, 3, 2)) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE cv_publisher (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE cv_volume (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                start_year INTEGER,
                publisher_id INTEGER,
                count_of_issues INTEGER
            );
            CREATE TABLE cv_issue (
                id INTEGER PRIMARY KEY,
                volume_id INTEGER NOT NULL,
                name TEXT,
                issue_number TEXT NOT NULL,
                cover_date TEXT
            );
            CREATE INDEX idx_cv_issue_volume_id ON cv_issue(volume_id);
            """
        )
        connection.execute("INSERT INTO cv_publisher VALUES (1, ?)", ("Publisher/One",))
        issue_id = 1000
        for offset, issue_count in enumerate(issue_counts, start=1):
            volume_id = 100 + offset
            series_name = "CON" if offset == 1 else f"Series: {offset}"
            connection.execute(
                "INSERT INTO cv_volume VALUES (?, ?, ?, ?, ?)",
                (volume_id, series_name, 2000 + offset, 1, issue_count),
            )
            for number in range(1, issue_count + 1):
                issue_id += 1
                issue_number = "0.5" if number == 1 and offset == 2 else str(number)
                connection.execute(
                    "INSERT INTO cv_issue VALUES (?, ?, ?, ?, ?)",
                    (
                        issue_id,
                        volume_id,
                        f"Issue / Title {issue_id}",
                        issue_number,
                        f"{2000 + offset}-01-01",
                    ),
                )


def _request(catalog: Path, output: Path, *, profile: str = "balanced") -> FixtureRequest:
    return FixtureRequest(
        catalog_path=catalog,
        output_path=output,
        series_count=4,
        file_count=16,
        seed=1300,
        profile=profile,
        max_issues_per_series=6,
    )


def test_balanced_plan_is_exact_deterministic_and_uses_real_catalog_ids(tmp_path: Path) -> None:
    catalog = tmp_path / "localcv.db"
    _build_catalog(catalog)
    before = _sha256(catalog)

    first = plan_import_scale_fixture(_request(catalog, tmp_path / "unused"))
    second = plan_import_scale_fixture(_request(catalog, tmp_path / "unused-2"))

    assert first == second
    assert len(first.series) == 4
    assert sum(len(series.issues) for series in first.series) == 16
    assert {series.volume_id for series in first.series} <= {101, 102, 103, 104, 105, 106}
    assert all(
        issue.volume_id == series.volume_id for series in first.series for issue in series.issues
    )
    assert any(issue.issue_number == "0.5" for series in first.series for issue in series.issues)
    assert _sha256(catalog) == before


def test_realistic_skew_plan_is_exact_deterministic_and_honors_cap(tmp_path: Path) -> None:
    catalog = tmp_path / "localcv.db"
    _build_catalog(catalog, issue_counts=(20, 10, 8, 5, 4, 3))
    request = _request(catalog, tmp_path / "unused", profile="realistic-skew")

    first = plan_import_scale_fixture(request)
    second = plan_import_scale_fixture(request)
    issue_counts = [len(series.issues) for series in first.series]

    assert first == second
    assert sum(issue_counts) == 16
    assert max(issue_counts) <= 6
    assert len(set(issue_counts)) > 1


def test_plan_treats_malformed_catalog_years_as_unknown(tmp_path: Path) -> None:
    catalog = tmp_path / "localcv.db"
    _build_catalog(catalog)
    with sqlite3.connect(catalog) as connection:
        connection.execute("UPDATE cv_volume SET start_year = '195-?'")

    plan = plan_import_scale_fixture(_request(catalog, tmp_path / "unused"))

    assert all(series.start_year is None for series in plan.series)


def test_plan_fails_before_output_mutation_when_catalog_capacity_is_insufficient(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "localcv.db"
    output = tmp_path / "fixture"
    _build_catalog(catalog, issue_counts=(2, 2))
    request = FixtureRequest(
        catalog_path=catalog,
        output_path=output,
        series_count=3,
        file_count=8,
        seed=1300,
        profile="balanced",
        max_issues_per_series=10,
    )

    with pytest.raises(ValueError, match="capacity"):
        generate_import_scale_fixture(request)

    assert not output.exists()


def test_generator_writes_valid_unique_archives_and_path_safe_streaming_manifest(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "localcv.db"
    output = tmp_path / "fixture"
    _build_catalog(catalog)

    summary = generate_import_scale_fixture(_request(catalog, output))

    rows = [
        json.loads(line)
        for line in (output / "fixture-manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    relative_paths = [row["relative_path"] for row in rows]
    archives = [output / "source" / relative_path for relative_path in relative_paths]
    assert summary["schema_version"] == 1
    assert summary["series_count"] == 4
    assert summary["file_count"] == 16
    assert "catalog_path" not in summary
    assert len(summary["path_samples"]) <= 10
    assert len(rows) == 16
    assert len(set(relative_paths)) == 16
    assert all(
        not Path(path).is_absolute() and ".." not in Path(path).parts for path in relative_paths
    )
    assert all(
        "/" not in part and "\\" not in part and ":" not in part
        for path in relative_paths
        for part in Path(path).parts
    )
    assert all(path.is_file() and zipfile.is_zipfile(path) for path in archives)
    assert len({_sha256(path) for path in archives}) == 16

    half_row = next(row for row in rows if row["issue_number"] == "0.5")
    with zipfile.ZipFile(output / "source" / half_row["relative_path"]) as archive:
        comic_info = archive.read("ComicInfo.xml").decode("utf-8")
        assert len([name for name in archive.namelist() if name.endswith(".png")]) == 32
    assert "<Number>0.5</Number>" in comic_info
    assert f"[cv_vol_id:{half_row['volume_id']}]" in comic_info
    assert f"[cv_issue_id:{half_row['issue_id']}]" in comic_info


def test_generator_refuses_existing_output_and_preserves_operator_files(tmp_path: Path) -> None:
    catalog = tmp_path / "localcv.db"
    output = tmp_path / "fixture"
    _build_catalog(catalog)
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("operator data", encoding="utf-8")

    with pytest.raises(FileExistsError, match="must not already exist"):
        generate_import_scale_fixture(_request(catalog, output))

    assert marker.read_text(encoding="utf-8") == "operator data"


def test_multipage_generator_has_explicit_sparse_safety_exceptions(tmp_path: Path) -> None:
    catalog = tmp_path / "localcv.db"
    output = tmp_path / "fixture"
    _build_catalog(catalog)
    summary = generate_import_scale_fixture(replace(_request(catalog, output), single_page_every=5))
    assert summary["single_page_files"] == 3
    rows = [
        json.loads(line) for line in (output / "fixture-manifest.jsonl").read_text().splitlines()
    ]
    for ordinal, row in enumerate(rows, start=1):
        expected = 1 if ordinal % 5 == 0 else 32
        with zipfile.ZipFile(output / "source" / row["relative_path"]) as archive:
            assert len([name for name in archive.namelist() if name.endswith(".png")]) == expected
        assert row["expected_safety_review"] == (expected == 1)


def test_long_series_directory_preserves_unknown_year_and_volume_id_suffix() -> None:
    series = PlannedSeries(
        volume_id=155371,
        name="Remède Impérial - L'Étrange Médecin de la Cour " * 5,
        start_year=None,
        publisher=None,
        issues=(),
    )

    directory = _series_directory(series, 0, layout_profile="series")

    assert directory.name.endswith(" (Unknown Year) [cv-155371]")
    assert len(directory.name.encode("utf-8")) <= 140


def test_layout_profile_keeps_certification_uniform_and_mixed_stress_explicit(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "localcv.db"
    _build_catalog(catalog, issue_counts=(4,) * 10)
    uniform = tmp_path / "uniform"
    mixed = tmp_path / "mixed"
    common = {
        "catalog_path": catalog,
        "series_count": 10,
        "file_count": 20,
        "seed": 1300,
        "profile": "balanced",
        "max_issues_per_series": 4,
    }

    uniform_summary = generate_import_scale_fixture(
        FixtureRequest(output_path=uniform, **common)  # type: ignore[arg-type]
    )
    mixed_summary = generate_import_scale_fixture(
        FixtureRequest(  # type: ignore[arg-type]
            output_path=mixed,
            layout_profile="mixed",
            **common,
        )
    )

    uniform_paths = [
        Path(json.loads(line)["relative_path"])
        for line in (uniform / "fixture-manifest.jsonl").read_text().splitlines()
    ]
    mixed_paths = [
        Path(json.loads(line)["relative_path"])
        for line in (mixed / "fixture-manifest.jsonl").read_text().splitlines()
    ]
    assert uniform_summary["layout_profile"] == "series"
    assert mixed_summary["layout_profile"] == "mixed"
    assert all(len(path.parts) == 2 for path in uniform_paths)
    assert any(len(path.parts) > 2 for path in mixed_paths)
