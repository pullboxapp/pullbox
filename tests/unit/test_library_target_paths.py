"""Tests for library target path planning helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_policy import LibraryIngestPolicy
from pullbox.core.library_target_paths import (
    predict_library_target_path,
    resolve_library_target_path,
)
from pullbox.models.issue import Issue, IssueType
from pullbox.models.library import LibraryRoot
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series

if TYPE_CHECKING:
    from pathlib import Path


def _policy(
    *,
    series_path_template: str = "{Series} ({Year})",
    replace_illegal_characters: bool = True,
) -> LibraryIngestPolicy:
    return LibraryIngestPolicy(
        rename_on_import=True,
        series_folder_template="{Series} ({Year})",
        comic_file_template="{Series} ({Year}) #{Issue:03d}",
        annual_file_template="{Series} ({Year}) Annual #{Issue:03d}",
        non_standard_file_template="{Series} ({Year}) {Type} v{Volume:02d}",
        single_non_standard_file_template="{Series} ({Year}) {Type}",
        replace_illegal_characters=replace_illegal_characters,
        colon_replacement="dash",
        post_processing_method="move",
        torrent_import_strategy="copy",
        normalize_imported_archives_to_cbz=False,
        skip_existing_files=False,
        update_embedded_comicinfo_from_match=False,
        series_path_template=series_path_template,
    )


@pytest.mark.asyncio
async def test_predict_library_target_path_uses_series_folder_and_renamed_file(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    root_path.mkdir()
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    series = Series(title="Batman", sort_title="batman", year_start=2026)
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, series, issue])
    await db_session.flush()

    target = await predict_library_target_path(
        db_session,
        tmp_path / "imports" / "random-name.cbz",
        issue,
        series,
        root,
        _policy(),
        rename=True,
    )

    assert target == root_path / "Batman (2026)" / "Batman (2026) #002.cbz"


@pytest.mark.asyncio
async def test_resolve_library_target_path_creates_folder_and_suffixes_collision(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    root_path.mkdir()
    series_folder = root_path / "Batman (2026)"
    series_folder.mkdir()
    existing_target = series_folder / "Batman 002.cbz"
    existing_target.write_bytes(b"already here")
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    series = Series(title="Batman", sort_title="batman", year_start=2026)
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, series, issue])
    await db_session.flush()

    target = await resolve_library_target_path(
        db_session,
        tmp_path / "imports" / "Batman 002.cbz",
        issue,
        series,
        root,
        _policy(),
        rename=False,
    )

    assert target.path == series_folder / "Batman 002 (1).cbz"
    assert target.series_folder_created is False


@pytest.mark.asyncio
async def test_resolve_library_target_path_raises_when_root_is_missing(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root = LibraryRoot(name="Missing", path=str(tmp_path / "missing"), enabled=True)
    series = Series(title="Batman", sort_title="batman", year_start=2026)
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, series, issue])
    await db_session.flush()

    with pytest.raises(ConfigurationError, match="Comics directory does not exist"):
        await resolve_library_target_path(
            db_session,
            tmp_path / "imports" / "Batman 002.cbz",
            issue,
            series,
            root,
            _policy(),
            rename=False,
        )


@pytest.mark.asyncio
async def test_predict_library_target_path_renders_nested_segments_independently(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    root_path.mkdir()
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    publisher = Publisher(name="DC/Black Label")
    series = Series(
        title="Batman",
        sort_title="batman",
        year_start=2026,
        publisher=publisher,
    )
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, publisher, series, issue])
    await db_session.flush()

    target = await predict_library_target_path(
        db_session,
        tmp_path / "imports" / "random-name.cbz",
        issue,
        series,
        root,
        _policy(series_path_template="{Publisher}/{Series} ({Year})"),
        rename=True,
    )

    assert target == (root_path / "DC - Black Label" / "Batman (2026)" / "Batman (2026) #002.cbz")


@pytest.mark.asyncio
async def test_predict_library_target_path_uses_deterministic_missing_token_fallback(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    root_path.mkdir()
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    series = Series(title="Batman", sort_title="batman", year_start=None)
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, series, issue])
    await db_session.flush()

    target = await predict_library_target_path(
        db_session,
        tmp_path / "imports" / "random-name.cbz",
        issue,
        series,
        root,
        _policy(series_path_template="{Publisher}/{Series} ({Year})"),
        rename=True,
    )

    assert target.parent == root_path / "Unknown" / "Batman (Unknown)"


@pytest.mark.asyncio
async def test_predict_library_target_path_preserves_existing_series_path(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    existing_path = root_path / "Custom" / "Batman"
    existing_path.mkdir(parents=True)
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    series = Series(
        title="Batman",
        sort_title="batman",
        year_start=2026,
        path=str(existing_path),
        library_root=root,
    )
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, series, issue])
    await db_session.flush()

    target = await predict_library_target_path(
        db_session,
        tmp_path / "imports" / "random-name.cbz",
        issue,
        series,
        root,
        _policy(series_path_template="{Publisher}/{Series} ({Year})"),
        rename=True,
    )

    assert target.parent == existing_path


@pytest.mark.asyncio
async def test_predict_library_target_path_rejects_existing_series_path_outside_root(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    root_path.mkdir()
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    series = Series(
        title="Batman",
        sort_title="batman",
        year_start=2026,
        path=str(tmp_path / "elsewhere" / "Batman"),
        library_root=root,
    )
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, series, issue])
    await db_session.flush()

    with pytest.raises(ConfigurationError, match="outside its library root"):
        await predict_library_target_path(
            db_session,
            tmp_path / "imports" / "random-name.cbz",
            issue,
            series,
            root,
            _policy(series_path_template="{Publisher}/{Series} ({Year})"),
            rename=True,
        )


@pytest.mark.asyncio
async def test_predict_library_target_path_blocks_token_path_escape_when_replacement_disabled(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    root_path.mkdir()
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    publisher = Publisher(name="../outside")
    series = Series(
        title="Batman",
        sort_title="batman",
        year_start=2026,
        publisher=publisher,
    )
    issue = Issue(series=series, issue_number=2.0, issue_type=IssueType.ISSUE)
    db_session.add_all([root, publisher, series, issue])
    await db_session.flush()

    with pytest.raises(ConfigurationError, match="unsafe path separator"):
        await predict_library_target_path(
            db_session,
            tmp_path / "imports" / "random-name.cbz",
            issue,
            series,
            root,
            _policy(
                series_path_template="{Publisher}/{Series} ({Year})",
                replace_illegal_characters=False,
            ),
            rename=True,
        )
