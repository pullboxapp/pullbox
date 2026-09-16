"""Literal Unicode source paths must agree across Mylar preview, maps, and scan."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pullbox.core.filesystem_policy import resolve_preview_source
from pullbox.core.mylar3_path_mapping import normalize_mylar3_path_map
from pullbox.core.mylar3_reader import Mylar3Reader
from pullbox.models.import_job import ImportFileHandlingMode
from pullbox.models.library import LibraryRoot
from pullbox.schemas.import_mylar3_path_preflight import MylarPathMappingDraft
from pullbox.services.import_mylar3_path_preflight import Mylar3PathPreflightAnalyzer
from pullbox.services.import_safety_diagnostics import (
    ImportSafetyCategory,
    classify_import_safety_failure,
)
from scripts.mylar3_import_fixture import create_minimal_cbz, create_mylar3_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


UNICODE_NAMES = (
    "Rùjiān Tóng\u200bxué Rù Mó le",
    "Series\u200e",
    "Series\u200f",
    "Series\u061c",
    "عنوان\u200cکتاب",  # noqa: RUF001 -- Persian letters test a literal multilingual path.
    "Hero\u200dTeam",
    "Soft\u00adhyphen",
    "Word\u2060joiner",
    "Series\ufeffName",
    "Non\u00a0breaking\u202fspace",
    "Remède Impérial - L'Étrange ♀ ♂",
)


@pytest.mark.parametrize("name", UNICODE_NAMES)
def test_mapping_preserves_supported_unicode_text(name: str) -> None:
    mapping = {f"/stored/{name}": f"/visible/{name}"}
    assert normalize_mylar3_path_map(mapping) == mapping


@pytest.mark.parametrize("name", UNICODE_NAMES)
def test_preview_source_preserves_literal_unicode_identity(tmp_path: Path, name: str) -> None:
    folder = tmp_path / name
    folder.mkdir()
    assert resolve_preview_source(folder) == folder.resolve()


def test_invalid_path_text_remains_non_overrideable() -> None:
    failure = classify_import_safety_failure(
        "The Mylar comic folder contains unsupported control characters.",
        code="invalid_path_text",
    )
    assert failure.category == ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD
    assert failure.overrideable is False


async def test_unicode_format_characters_do_not_collapse_distinct_paths(tmp_path: Path) -> None:
    database = tmp_path / "mylar.db"
    names = ("SeriesName", "Series\u200bName")
    for name in names:
        create_minimal_cbz(tmp_path / name / "Issue 001.cbz")
    create_mylar3_db(
        database,
        series=[
            {
                "ComicID": str(index),
                "ComicName": name,
                "ComicLocation": str(tmp_path / name),
            }
            for index, name in enumerate(names, 1)
        ],
    )
    series = await Mylar3Reader(database).read_series()
    assert {item.source_folder for item in series} == {str(tmp_path / name) for name in names}
    assert all(len(item.files) == 1 for item in series)


@pytest.mark.parametrize("mapped", [False, True])
@pytest.mark.parametrize("in_place", [False, True])
@pytest.mark.parametrize("name", UNICODE_NAMES)
async def test_unicode_sources_pass_preview_and_paged_reader_unchanged(
    tmp_path: Path,
    db_session: AsyncSession,
    name: str,
    mapped: bool,
    in_place: bool,
) -> None:
    root = tmp_path / "library"
    folder = root / name
    stored_root = Path("/mylar-unicode-fixture") if mapped else root
    database = tmp_path / "mylar.db"
    files = [folder / f"Issue {number} - {name}.cbz" for number in ("0.5", "1000000")]
    for file in files:
        create_minimal_cbz(file)
    create_mylar3_db(
        database,
        series=[
            {
                "ComicID": "155965",
                "ComicName": name,
                "ComicYear": "2021",
                "ComicLocation": str(stored_root / name),
                "Total": 2,
            }
        ],
        issues=[
            {
                "IssueID": str(101 + index),
                "ComicID": "155965",
                "Issue_Number": number,
                "Location": str(stored_root / name / file.name),
            }
            for index, (number, file) in enumerate(zip(("0.5", "1000000"), files, strict=True))
        ],
    )
    original_db = database.read_bytes()
    original_files = {file: file.read_bytes() for file in files}
    db_session.add(
        LibraryRoot(
            name="Unicode source",
            path=str(root),
            enabled=True,
            allow_referenced_registrations=True,
        )
    )
    await db_session.flush()
    path_map = {str(stored_root): str(root)} if mapped else {}
    preview = await Mylar3PathPreflightAnalyzer().analyze(
        db_session,
        database,
        auto_detect=False,
        mappings=[
            MylarPathMappingDraft(stored_prefix=key, pullbox_prefix=value)
            for key, value in path_map.items()
        ],
        file_handling_mode=(
            ImportFileHandlingMode.IN_PLACE if in_place else ImportFileHandlingMode.MANAGED_COPY
        ),
    )
    assert preview.resolution.locations == 3
    assert preview.resolution.invalid == 0
    assert preview.resolution.identity_resolved + preview.resolution.mapped_existing == 3
    assert preview.can_confirm
    reader = Mylar3Reader(
        database,
        path_map=path_map,
        include_missing_files=in_place,
        reference_root_boundaries=((root, root.resolve()),) if in_place else None,
    )
    series = [series async for page in reader.iter_import_series_pages() for series in page]
    assert len(series) == 1
    assert series[0].raw_series_name == name
    assert series[0].file_count == 2
    assert {file.issue_number_raw for file in series[0].files} == {"0.5", "1000000"}
    assert {file.comicvine_issue_id for file in series[0].files} == {101, 102}
    assert {file.file_path for file in series[0].files} == {str(file) for file in files}
    assert database.read_bytes() == original_db
    assert {file: file.read_bytes() for file in files} == original_files


@pytest.mark.parametrize(
    "character",
    [
        "\x00",
        "\x01",
        "\n",
        "\r",
        "\t",
        "\x7f",
        "\x85",
        "\ud800",
        "\u2028",
        "\u2029",
        "\u202a",
        "\u202e",
        "\u2066",
    ],
)
def test_unicode_support_keeps_unsafe_mapping_text_blocked(character: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        normalize_mylar3_path_map({f"/stored/Bad{character}Name": "/visible"})
    with pytest.raises(ValueError, match="invalid"):
        normalize_mylar3_path_map({"/stored": f"/visible/Bad{character}Name"})


@pytest.mark.parametrize("location", ["/comics/bad\nname", "/comics/" + "a" * 4096])
async def test_rejected_path_text_is_not_reported_as_root_escape(
    tmp_path: Path,
    location: str,
) -> None:
    database = tmp_path / "mylar.db"
    create_mylar3_db(
        database,
        series=[
            {
                "ComicID": "42",
                "ComicName": "Invalid path",
                "ComicLocation": location,
            }
        ],
    )
    series = (await Mylar3Reader(database).read_series())[0]
    assert not series.files
    assert series.diagnostics["reason"] == "invalid_path_text"
    assert "outside" not in str(series.diagnostics["rejection_reason"])


@pytest.mark.parametrize("symlink", [False, True])
async def test_unicode_names_do_not_allow_mapped_root_escape(
    tmp_path: Path,
    symlink: bool,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "Outside\u200b"
    create_minimal_cbz(outside / "Issue 1.cbz")
    if symlink:
        (root / outside.name).symlink_to(outside, target_is_directory=True)
    location = "/comics/" + ("" if symlink else "../") + outside.name
    database = tmp_path / "mylar.db"
    create_mylar3_db(
        database,
        series=[
            {
                "ComicID": "42",
                "ComicName": "Escaping series",
                "ComicLocation": location,
            }
        ],
    )
    series = (await Mylar3Reader(database, path_map={"/comics": str(root)}).read_series())[0]
    assert not series.files
    assert series.diagnostics["reason"] == "unsafe_path_mapping"
