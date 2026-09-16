"""Split-series review and future-placement tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.services.import_split_series import (
    load_selected_split_series_review,
    require_preferred_managed_root_for_selected_split_series,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


async def _root(session: AsyncSession, path: Path, name: str) -> LibraryRoot:
    path.mkdir()
    root = LibraryRoot(
        name=name,
        path=str(path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=True,
    )
    session.add(root)
    await session.flush()
    return root


async def _selected_series(session: AsyncSession, job: ImportJob) -> ImportedSeries:
    item = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        status=ImportSeriesStatus.MATCHED,
        cv_id=796,
        selected_for_import=True,
        file_count=2,
        files_total=2,
        files_matched=2,
    )
    session.add(item)
    await session.flush()
    return item


async def _import_file(
    session: AsyncSession,
    job: ImportJob,
    item: ImportedSeries,
    path: Path,
    issue_number: float,
) -> ImportedFile:
    path.write_bytes(b"comic")
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=item.id,
        file_path=str(path),
        file_name=path.name,
        file_size=path.stat().st_size,
        file_format="cbz",
        parsed_issue_number=issue_number,
        status=ImportedFileStatus.MATCHED,
    )
    session.add(imported_file)
    await session.flush()
    return imported_file


async def test_selected_in_place_series_across_two_roots_requires_explicit_future_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    first_root = await _root(db_session, tmp_path / "main", "Main")
    second_root = await _root(db_session, tmp_path / "archive", "Archive")
    job = ImportJob(
        source_path=str(tmp_path),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
    )
    db_session.add(job)
    await db_session.flush()
    item = await _selected_series(db_session, job)
    await _import_file(db_session, job, item, tmp_path / "main" / "Batman 001.cbz", 1)
    await _import_file(db_session, job, item, tmp_path / "archive" / "Batman 002.cbz", 2)

    with pytest.raises(ValidationError, match="preferred managed destination"):
        await require_preferred_managed_root_for_selected_split_series(
            db_session,
            job,
            preferred_library_root_id=None,
        )

    review = await require_preferred_managed_root_for_selected_split_series(
        db_session,
        job,
        preferred_library_root_id=first_root.id,
    )

    assert len(review.items) == 1
    assert review.items[0].imported_series_ids == (item.id,)
    assert review.items[0].root_ids == tuple(sorted((first_root.id, second_root.id)))
    assert job.target_library_root_id is None


async def test_non_split_in_place_series_keeps_future_root_optional(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    await _root(db_session, tmp_path / "main", "Main")
    job = ImportJob(
        source_path=str(tmp_path / "main"),
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
    )
    db_session.add(job)
    await db_session.flush()
    item = await _selected_series(db_session, job)
    source = tmp_path / "main" / "Batman 001.cbz"
    await _import_file(db_session, job, item, source, 1)

    review = await require_preferred_managed_root_for_selected_split_series(
        db_session,
        job,
        preferred_library_root_id=None,
    )

    assert review.items == ()
    assert source.read_bytes() == b"comic"


async def test_managed_copy_does_not_require_split_source_destination_choice(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    await _root(db_session, tmp_path / "main", "Main")
    await _root(db_session, tmp_path / "archive", "Archive")
    job = ImportJob(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
        file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
    )
    db_session.add(job)
    await db_session.flush()
    item = await _selected_series(db_session, job)
    await _import_file(db_session, job, item, tmp_path / "main" / "Batman 001.cbz", 1)
    await _import_file(db_session, job, item, tmp_path / "archive" / "Batman 002.cbz", 2)

    review = await require_preferred_managed_root_for_selected_split_series(
        db_session,
        job,
        preferred_library_root_id=None,
    )

    assert review.items == ()


async def test_existing_canonical_series_root_combines_with_selected_incoming_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    first_root = await _root(db_session, tmp_path / "main", "Main")
    second_root = await _root(db_session, tmp_path / "archive", "Archive")
    existing_path = tmp_path / "main" / "Batman 001.cbz"
    existing_path.write_bytes(b"existing")
    canonical_series = Series(
        comicvine_id=796,
        title="Batman",
        sort_title="Batman",
        library_root_id=first_root.id,
        path=str(tmp_path / "main"),
    )
    db_session.add(canonical_series)
    await db_session.flush()
    issue = Issue(series_id=canonical_series.id, issue_number=1)
    db_session.add(issue)
    await db_session.flush()
    db_session.add(
        LibraryFile(
            file_path=str(existing_path),
            file_name=existing_path.name,
            file_size=existing_path.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=first_root.id,
        )
    )
    job = ImportJob(
        source_path=str(tmp_path / "archive"),
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
    )
    db_session.add(job)
    await db_session.flush()
    duplicate = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        status=ImportSeriesStatus.DUPLICATE,
        cv_id=796,
        series_id=canonical_series.id,
        file_count=1,
        files_total=1,
        files_matched=1,
    )
    db_session.add(duplicate)
    await db_session.flush()
    incoming_path = tmp_path / "archive" / "Batman 002.cbz"
    incoming = await _import_file(db_session, job, duplicate, incoming_path, 2)
    incoming.include_in_import = True
    await db_session.flush()

    review = await load_selected_split_series_review(db_session, job)

    assert len(review.items) == 1
    assert review.items[0].canonical_series_id == canonical_series.id
    assert review.items[0].root_ids == tuple(sorted((first_root.id, second_root.id)))
    assert await db_session.scalar(select(func.count(Series.id))) == 1
    assert existing_path.read_bytes() == b"existing"
    assert incoming_path.read_bytes() == b"comic"
