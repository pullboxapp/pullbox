"""Completed-import clean-library adoption contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

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
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.series import Series
from pullbox.models.user import User
from pullbox.services.import_library_adoption import (
    create_clean_library_import,
    prepare_clean_library_import,
    preview_clean_library_import,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_referenced_import(
    session: AsyncSession,
) -> tuple[ImportJob, ImportedFile, LibraryFile, LibraryRoot, LibraryRoot]:
    session.add(User(id=73, username="library-organizer", password_hash="unused"))
    source_root = LibraryRoot(
        name="Mylar library",
        path="/legacy-comics",
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=True,
    )
    target_root = LibraryRoot(
        name="Clean Pullbox library",
        path="/pullbox-comics",
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=True,
    )
    series = Series(
        comicvine_id=123,
        title="Action Comics",
        sort_title="action comics",
        year_start=1938,
        monitored=True,
        library_root=source_root,
    )
    session.add_all([source_root, target_root, series])
    await session.flush()
    issue = Issue(
        series_id=series.id,
        comicvine_id=456,
        issue_number=1002,
        issue_number_text="1002",
    )
    session.add(issue)
    await session.flush()
    library_file = LibraryFile(
        issue_id=issue.id,
        library_root_id=source_root.id,
        file_path="/legacy-comics/Fritzi Ritz/Action Comics 1002.cbr",
        file_name="Action Comics 1002.cbr",
        file_size=4096,
        file_format=FileFormat.CBR,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        storage_mode=LibraryFileStorageMode.REFERENCED,
        source_signature={"size": 4096, "mtime_ns": 10},
    )
    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.COMPLETED,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        source_preserved=True,
    )
    imported_series = ImportedSeries(
        import_job=job,
        raw_series_name="Action Comics",
        cv_id=123,
        cv_title="Action Comics",
        cv_year=1938,
        status=ImportSeriesStatus.IMPORTED,
        series_id=series.id,
    )
    session.add_all([library_file, job, imported_series])
    await session.flush()
    imported_file = ImportedFile(
        import_job=job,
        import_series=imported_series,
        file_path=library_file.file_path,
        file_name=library_file.file_name,
        file_size=library_file.file_size,
        file_format="cbr",
        parsed_series="Action Comics",
        parsed_issue_number=1002,
        issue_number_raw="1002",
        status=ImportedFileStatus.IMPORTED,
        matched_issue_id=issue.id,
        matched_issue_cv_id=issue.comicvine_id,
        match_confidence="high",
        match_method="comicinfo",
        include_in_import=False,
        library_file_id=library_file.id,
        source_signature=dict(library_file.source_signature),
    )
    session.add(imported_file)
    await session.commit()
    return job, imported_file, library_file, source_root, target_root


@pytest.mark.asyncio
async def test_preview_create_and_prepare_clean_library_import_clone_referenced_ownership(
    db_session: AsyncSession,
) -> None:
    (
        source_job,
        _source_file,
        library_file,
        _source_root,
        target_root,
    ) = await _seed_referenced_import(db_session)

    preview = await preview_clean_library_import(
        db_session,
        source_job.id,
        target_root_id=target_root.id,
        actor_id=73,
    )

    assert preview.eligible_file_count == 1
    assert preview.eligible_series_count == 1
    assert preview.total_bytes == 4096
    assert preview.source_preserved is True

    result = await create_clean_library_import(
        db_session,
        source_job.id,
        target_root_id=target_root.id,
        actor_id=73,
        preview_token=preview.preview_token,
    )

    clean_job = await db_session.get(ImportJob, result.job_id)
    assert clean_job is not None
    assert clean_job.status is ImportJobStatus.IMPORTING
    assert clean_job.file_handling_mode is ImportFileHandlingMode.MANAGED_COPY
    assert clean_job.effective_transfer_method == "copy"
    assert clean_job.source_preserved is True
    assert clean_job.target_library_root_id == target_root.id
    assert clean_job.progress_snapshot["source_import_job_id"] == source_job.id
    assert clean_job.progress_snapshot["clean_library_adoption_prepared"] is False

    cloned_files_before_prepare = list(
        (
            await db_session.scalars(
                select(ImportedFile).where(ImportedFile.import_job_id == clean_job.id)
            )
        ).all()
    )
    assert cloned_files_before_prepare == []

    await prepare_clean_library_import(db_session, clean_job.id)
    await db_session.refresh(clean_job)
    assert clean_job.progress_snapshot["clean_library_adoption_prepared"] is True

    cloned_file = (
        await db_session.scalars(
            select(ImportedFile).where(ImportedFile.import_job_id == clean_job.id)
        )
    ).one()
    assert cloned_file.status is ImportedFileStatus.CONFIRMED
    assert cloned_file.include_in_import is True
    assert cloned_file.library_file_id is None
    adoption = cloned_file.diagnostics["library_adoption"]
    assert adoption["source_library_file_id"] == library_file.id
    assert adoption["source_path"] == library_file.file_path
    assert adoption["source_preserved"] is True


@pytest.mark.asyncio
async def test_clean_library_import_requires_a_separate_managed_root(
    db_session: AsyncSession,
) -> None:
    (
        source_job,
        _source_file,
        _library_file,
        source_root,
        _target_root,
    ) = await _seed_referenced_import(db_session)

    with pytest.raises(ValidationError, match="separate managed library root"):
        await preview_clean_library_import(
            db_session,
            source_job.id,
            target_root_id=source_root.id,
            actor_id=73,
        )


@pytest.mark.asyncio
async def test_clean_library_import_rejects_another_active_import(
    db_session: AsyncSession,
) -> None:
    (
        source_job,
        _source_file,
        _library_file,
        _source_root,
        target_root,
    ) = await _seed_referenced_import(db_session)
    preview = await preview_clean_library_import(
        db_session,
        source_job.id,
        target_root_id=target_root.id,
        actor_id=73,
    )
    db_session.add(
        ImportJob(
            source_path="/imports/already-running",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.SCANNING,
        )
    )
    await db_session.commit()

    with pytest.raises(ValidationError, match="Only one import can be active at a time"):
        await create_clean_library_import(
            db_session,
            source_job.id,
            target_root_id=target_root.id,
            actor_id=73,
            preview_token=preview.preview_token,
        )


@pytest.mark.asyncio
async def test_clean_library_import_requires_exact_mixed_folder_repairs_first(
    db_session: AsyncSession,
) -> None:
    (
        source_job,
        source_file,
        _library_file,
        _source_root,
        target_root,
    ) = await _seed_referenced_import(db_session)
    imported_series = await db_session.get(ImportedSeries, source_file.import_series_id)
    assert imported_series is not None
    wrong_series = Series(
        title="Fritzi Ritz",
        sort_title="fritzi ritz",
        year_start=1953,
        monitored=True,
    )
    db_session.add(wrong_series)
    await db_session.flush()
    imported_series.raw_series_name = wrong_series.title
    imported_series.cv_title = wrong_series.title
    imported_series.series_id = wrong_series.id
    source_file.diagnostics = {
        "metadata_signals": {
            "series_name": "comicinfo",
            "issue_number": "comicinfo",
        },
        "source_metadata": {"comicinfo": {"series": "Action Comics", "number": "1002"}},
    }
    await db_session.commit()

    with pytest.raises(ValidationError, match="Resolve mixed-folder files"):
        await preview_clean_library_import(
            db_session,
            source_job.id,
            target_root_id=target_root.id,
            actor_id=73,
        )
