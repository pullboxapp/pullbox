"""Tests for import file staging and ComicInfo helper contracts."""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.models.creator import Creator, IssueCreator
from pullbox.models.import_job import ImportedFile, ImportJob
from pullbox.models.issue import Issue
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series
from pullbox.services.import_file_preparation import (
    PreparedImportFile,
    apply_comicinfo_to_imported_artifact,
    build_comicinfo_payload_for_issue,
    cleanup_prepared_file,
    format_comicinfo_issue_number,
    inspect_archive_page_count,
    prepare_import_file,
    repaired_cbz_output_path,
    rewrite_import_file_comicinfo,
)
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def test_format_comicinfo_issue_number_preserves_integer_and_fractional_values() -> None:
    assert format_comicinfo_issue_number(None) is None
    assert format_comicinfo_issue_number(4.0) == "4"
    assert format_comicinfo_issue_number(4.5) == "4.5"
    assert format_comicinfo_issue_number(1_000_000.0) == "1000000"


def test_inspect_archive_page_count_counts_image_entries_only(tmp_path: Path) -> None:
    archive_path = tmp_path / "issue.cbz"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("001.jpg", b"page one")
        archive.writestr("002.png", b"page two")
        archive.writestr("metadata/ComicInfo.xml", b"<ComicInfo />")
        archive.writestr("__MACOSX/._001.jpg", b"resource fork")
        archive.writestr("notes.txt", b"not a page")

    assert inspect_archive_page_count(archive_path) == 2


@pytest.mark.asyncio
async def test_prepare_import_file_normalizes_non_cbz_with_injected_converter(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Chicken Devil 004.cb7"
    source.write_bytes(b"placeholder")
    converted = tmp_path / "converted.cbz"
    calls: list[tuple[Path, str, Path | None]] = []

    async def fake_converter(
        source_path: Path,
        target_format: str,
        destination: Path | None = None,
        *,
        progress_callback: object | None = None,
    ) -> Path:
        _ = progress_callback
        calls.append((source_path, target_format, destination))
        converted.write_bytes(b"converted")
        return converted

    prepared = await prepare_import_file(
        ImportJob(
            move_to_library=True,
            transfer_method="copy",
            convert_to_preferred_format=True,
        ),
        ImportedFile(file_path=str(source), file_name=source.name),
        converter=fake_converter,
    )

    assert calls
    assert calls[0][0] == source
    assert calls[0][1] == "cbz"
    assert calls[0][2] is not None
    assert prepared.registration_source == converted
    assert prepared.original_source == source
    assert prepared.converted is True
    assert prepared.cleanup_paths


@pytest.mark.asyncio
async def test_prepare_import_file_surfaces_pdf_decompression_bomb_for_safety_review(
    tmp_path: Path,
) -> None:
    source = tmp_path / "The Joker - Endgame (2015) #001.pdf"
    source.write_bytes(b"%PDF-1.7 placeholder")

    async def fake_converter(
        source_path: Path,
        target_format: str,
        destination: Path | None = None,
        *,
        progress_callback: object | None = None,
    ) -> Path:
        _ = source_path, target_format, destination, progress_callback
        raise RuntimeError(
            "Image.py:3574: DecompressionBombWarning: Image size exceeds limit. "
            '{"type": "DecompressionBombError", "message": "Image size exceeds limit"}'
        )

    with pytest.raises(RuntimeError, match="DecompressionBomb"):
        await prepare_import_file(
            ImportJob(
                move_to_library=True,
                transfer_method="copy",
                convert_to_preferred_format=True,
                update_embedded_comicinfo_from_match=True,
            ),
            ImportedFile(file_path=str(source), file_name=source.name),
            converter=fake_converter,
        )


@pytest.mark.asyncio
async def test_prepare_import_file_rejects_hardlink_normalization(tmp_path: Path) -> None:
    source = tmp_path / "Chicken Devil 004.cb7"
    source.write_bytes(b"placeholder")

    with pytest.raises(ValidationError, match="Move or Copy"):
        await prepare_import_file(
            ImportJob(
                move_to_library=True,
                transfer_method="hardlink",
                update_embedded_comicinfo_from_match=True,
            ),
            ImportedFile(file_path=str(source), file_name=source.name),
        )


def test_apply_comicinfo_requires_cbz_artifact(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="CBZ archive"):
        apply_comicinfo_to_imported_artifact(tmp_path / "issue.cbr", {})


def test_cleanup_prepared_file_removes_temporary_paths(tmp_path: Path) -> None:
    temp_dir = tmp_path / "cleanup-dir"
    temp_dir.mkdir()
    temp_file = tmp_path / "cleanup-file.tmp"
    temp_file.write_bytes(b"temporary")

    cleanup_prepared_file(
        PreparedImportFile(
            registration_source=temp_file,
            original_source=temp_file,
            cleanup_paths=[temp_dir, temp_file],
        )
    )

    assert not temp_dir.exists()
    assert not temp_file.exists()


def test_repaired_cbz_output_path_uses_unique_sibling_name(tmp_path: Path) -> None:
    source = tmp_path / "Chicken Devil 004.cb7"
    source.write_bytes(b"source")
    first = tmp_path / "Chicken Devil 004 [Pullbox Repaired].cbz"
    first.write_bytes(b"existing")

    assert repaired_cbz_output_path(source) == (
        tmp_path / "Chicken Devil 004 [Pullbox Repaired 2].cbz"
    )


@pytest.mark.asyncio
async def test_build_comicinfo_payload_for_issue_includes_authoritative_metadata(
    db_session: AsyncSession,
) -> None:
    publisher = Publisher(name="AfterShock Comics", comicvine_id=123)
    db_session.add(publisher)
    await db_session.flush()
    series = Series(
        title="Chicken Devil",
        sort_title="chicken devil",
        year_start=2021,
        comicvine_id=139451,
        comicvine_url="https://comicvine.gamespot.com/chicken-devil/4050-139451/",
        issue_count=5,
        publisher_id=publisher.id,
    )
    db_session.add(series)
    await db_session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=4.0,
        comicvine_id=905404,
        title="The Chicken is in the Details",
        description="Mitchell Moss is in deep poultry trouble.",
        page_count=32,
        comicvine_url=(
            "https://comicvine.gamespot.com/"
            "chicken-devil-4-the-chicken-is-in-the-details/4000-905404/"
        ),
        release_date=date(2022, 4, 13),
    )
    db_session.add(issue)
    await db_session.flush()

    creators = [
        Creator(name="Brian Buccellato", comicvine_id=1),
        Creator(name="Hayden Sherman", comicvine_id=2),
        Creator(name="Matt Herms", comicvine_id=3),
        Creator(name="Hassan Otsmane-Elhaou", comicvine_id=4),
        Creator(name="Andy Clarke", comicvine_id=5),
        Creator(name="Christina Harrington", comicvine_id=6),
    ]
    db_session.add_all(creators)
    await db_session.flush()
    db_session.add_all(
        [
            IssueCreator(issue_id=issue.id, creator_id=creators[0].id, role="Writer"),
            IssueCreator(issue_id=issue.id, creator_id=creators[1].id, role="Artist"),
            IssueCreator(issue_id=issue.id, creator_id=creators[2].id, role="Colorist"),
            IssueCreator(issue_id=issue.id, creator_id=creators[3].id, role="Letterer"),
            IssueCreator(issue_id=issue.id, creator_id=creators[4].id, role="Cover Artist"),
            IssueCreator(issue_id=issue.id, creator_id=creators[5].id, role="Editor"),
        ]
    )
    await db_session.flush()

    payload = await build_comicinfo_payload_for_issue(db_session, issue, page_count=198)

    assert payload["Series"] == "Chicken Devil"
    assert payload["Number"] == "4"
    assert payload["Title"] == "The Chicken is in the Details"
    assert payload["Summary"] == "Mitchell Moss is in deep poultry trouble."
    assert payload["Publisher"] == "AfterShock Comics"
    assert payload["Year"] == 2022
    assert payload["Month"] == 4
    assert payload["Day"] == 13
    assert payload["PageCount"] == 198
    assert payload["Count"] == 5
    assert payload["Volume"] == 2021
    assert payload["Notes"] == "[cv_vol_id:139451] [cv_issue_id:905404]"
    assert payload["Writer"] == "Brian Buccellato"
    assert payload["Penciller"] == "Hayden Sherman"
    assert payload["Colorist"] == "Matt Herms"
    assert payload["Letterer"] == "Hassan Otsmane-Elhaou"
    assert payload["CoverArtist"] == "Andy Clarke"
    assert payload["Editor"] == "Christina Harrington"


@pytest.mark.asyncio
async def test_rewrite_import_file_comicinfo_normalizes_non_cbz_with_injected_helpers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Chicken Devil 004.cb7"
    source.write_bytes(b"source")
    embedded_paths: list[Path] = []

    async def fake_converter(
        source_path: Path,
        target_format: str,
        destination: Path | None = None,
        *,
        progress_callback: object | None = None,
    ) -> Path:
        _ = progress_callback
        assert source_path == source
        assert target_format == "cbz"
        assert destination is not None
        converted = destination / "converted.cbz"
        converted.write_bytes(b"converted")
        return converted

    def fake_embedder(
        artifact_path: Path,
        comicinfo_payload: dict[str, object],
        *,
        progress_callback: object | None = None,
    ) -> None:
        _ = progress_callback
        assert comicinfo_payload == {"Series": "Chicken Devil"}
        embedded_paths.append(artifact_path)

    repaired_path, repair_mode, original_source = await rewrite_import_file_comicinfo(
        ImportedFile(file_path=str(source), file_name=source.name),
        {"Series": "Chicken Devil"},
        converter=fake_converter,
        embedder=fake_embedder,
    )

    assert repair_mode == "normalized_to_cbz"
    assert original_source == str(source)
    assert repaired_path == tmp_path / "Chicken Devil 004 [Pullbox Repaired].cbz"
    assert repaired_path.exists()
    assert embedded_paths


def test_import_service_file_preparation_shims_remain_available() -> None:
    assert ImportService._format_comicinfo_issue_number(4.0) == "4"
    assert ImportService._repaired_cbz_output_path(Path("/tmp/Chicken Devil 004.cb7")) == Path(
        "/tmp/Chicken Devil 004 [Pullbox Repaired].cbz"
    )
