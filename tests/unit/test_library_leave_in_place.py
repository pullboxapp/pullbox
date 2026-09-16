"""Tests for leave-in-place library file handling."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_leave_in_place import handle_leave_in_place
from pullbox.core.library_policy import LibraryIngestPolicy
from pullbox.models.issue import Issue, IssueType
from pullbox.models.library import LibraryRoot
from pullbox.models.series import Series

if TYPE_CHECKING:
    from pathlib import Path


def _policy() -> LibraryIngestPolicy:
    return LibraryIngestPolicy(
        rename_on_import=True,
        series_folder_template="{Series} ({Year})",
        comic_file_template="{Series} ({Year}) #{Issue:03d}",
        annual_file_template="{Series} ({Year}) Annual #{Issue:03d}",
        non_standard_file_template="{Series} ({Year}) {Type} v{Volume:02d}",
        single_non_standard_file_template="{Series} ({Year}) {Type}",
        replace_illegal_characters=True,
        colon_replacement="dash",
        post_processing_method="move",
        torrent_import_strategy="copy",
        normalize_imported_archives_to_cbz=False,
        skip_existing_files=False,
        update_embedded_comicinfo_from_match=False,
    )


@pytest.mark.asyncio
async def test_handle_leave_in_place_does_not_rename_outside_library_root(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    root_path.mkdir()
    source_path = tmp_path / "imports" / "loose-name.cbz"
    source_path.parent.mkdir()
    source_path.write_bytes(b"comic")
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    series = Series(title="Batman", sort_title="batman", year_start=2026)
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, series, issue])
    await db_session.flush()

    with pytest.raises(ConfigurationError, match="inside an enabled library root"):
        await handle_leave_in_place(
            db_session,
            source_path,
            issue,
            series,
            root,
            _policy(),
            rename=True,
        )

    assert source_path.exists()


@pytest.mark.asyncio
async def test_handle_leave_in_place_noops_when_computed_name_is_unchanged(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    root_path.mkdir()
    source_path = root_path / "Batman (2026) #002.cbz"
    source_path.write_bytes(b"comic")
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    series = Series(title="Batman", sort_title="batman", year_start=2026)
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, series, issue])
    await db_session.flush()

    result = await handle_leave_in_place(
        db_session,
        source_path,
        issue,
        series,
        root,
        _policy(),
        rename=False,
    )

    assert result == source_path
    assert source_path.read_bytes() == b"comic"


@pytest.mark.asyncio
async def test_handle_leave_in_place_rejects_rename_inside_library_root(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    root_path.mkdir()
    source_path = root_path / "loose-name.cbz"
    source_path.write_bytes(b"comic")
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    series = Series(title="Batman", sort_title="batman", year_start=2026)
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, series, issue])
    await db_session.flush()

    with pytest.raises(ConfigurationError, match="cannot rename"):
        await handle_leave_in_place(
            db_session,
            source_path,
            issue,
            series,
            root,
            _policy(),
            rename=True,
        )

    assert source_path.exists()
    assert source_path.read_bytes() == b"comic"
