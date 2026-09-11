"""API contracts for completed-import recovery cleanup."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.series import Series
from pullbox.services.import_safety_diagnostics import (
    ImportSafetyCategory,
    build_import_safety_diagnostics,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest_plugins = ["conftest_security"]

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _csrf_header_for(client: AsyncClient) -> dict[str, str]:
    from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

    session_token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(session_token) or ""
    return {"X-CSRF-Token": csrf}


async def _seed_missing_reference(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with factory() as session:
        job = ImportJob(
            source_path="/imports/mylar.db",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.COMPLETED,
        )
        session.add(job)
        await session.flush()
        imported_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Old Mylar record",
            status=ImportSeriesStatus.IMPORTED,
        )
        session.add(imported_series)
        await session.flush()
        block = build_import_safety_diagnostics(
            ImportSafetyCategory.SOURCE_MISSING.value,
            code=ImportSafetyCategory.SOURCE_MISSING.value,
        )
        file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path="/comics/missing.cbz",
            file_name="missing.cbz",
            file_size=0,
            file_format="cbz",
            status=ImportedFileStatus.SAFETY_BLOCKED,
            diagnostics={"safety_block": block},
        )
        session.add(file)
        await session.commit()
        return int(job.id), int(file.id)


async def _seed_mixed_folder_file(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with factory() as session:
        job = ImportJob(
            source_path="/imports/mylar.db",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.COMPLETED,
        )
        source_series = ImportedSeries(
            import_job=job,
            raw_series_name="Fritzi Ritz",
            cv_title="Fritzi Ritz",
            status=ImportSeriesStatus.IMPORTED,
        )
        target = Series(
            title="Action Comics",
            sort_title="action comics",
            year_start=1938,
            monitored=True,
        )
        session.add_all([job, source_series, target])
        await session.flush()
        issue = Issue(series_id=target.id, issue_number=1002, issue_number_text="1002")
        target_import_series = ImportedSeries(
            import_job=job,
            raw_series_name="Action Comics",
            cv_title="Action Comics",
            status=ImportSeriesStatus.IMPORTED,
            series_id=target.id,
        )
        session.add_all([issue, target_import_series])
        await session.flush()
        imported_file = ImportedFile(
            import_job=job,
            import_series=source_series,
            file_path="/comics/Fritzi Ritz/Action Comics 1002.cbz",
            file_name="Action Comics 1002.cbz",
            file_size=1024,
            file_format="cbz",
            status=ImportedFileStatus.NO_MATCH,
            diagnostics={
                "metadata_signals": {
                    "series_name": "comicinfo",
                    "issue_number": "comicinfo",
                },
                "source_metadata": {"comicinfo": {"series": "Action Comics", "number": "1002"}},
            },
        )
        session.add(imported_file)
        await session.commit()
        return int(job.id), int(imported_file.id)


async def _seed_referenced_library(
    factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    async with factory() as session:
        source_path = tmp_path / "mylar" / "Batman" / "Batman 001.cbr"
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"legacy comic")
        target_path = tmp_path / "clean-library"
        target_path.mkdir()
        source_root = LibraryRoot(
            name="Mylar",
            path=str(tmp_path / "mylar"),
            allow_managed_writes=False,
        )
        target_root = LibraryRoot(name="Clean library", path=str(target_path))
        series = Series(title="Batman", sort_title="batman", year_start=2016)
        session.add_all([source_root, target_root, series])
        await session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            issue_number_text="1",
            status=IssueStatus.OWNED,
        )
        job = ImportJob(
            source_path="/imports/mylar.db",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.COMPLETED,
        )
        session.add_all([issue, job])
        await session.flush()
        imported_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name=series.title,
            raw_year=series.year_start,
            status=ImportSeriesStatus.IMPORTED,
            series_id=series.id,
        )
        library_file = LibraryFile(
            file_path=str(source_path),
            file_name=source_path.name,
            file_size=source_path.stat().st_size,
            file_format=FileFormat.CBR,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=source_root.id,
            storage_mode=LibraryFileStorageMode.REFERENCED,
            source_signature=build_file_identity_signature(source_path),
        )
        session.add_all([imported_series, library_file])
        await session.flush()
        session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=imported_series.id,
                file_path=str(source_path),
                file_name=source_path.name,
                file_size=source_path.stat().st_size,
                file_format="cbr",
                status=ImportedFileStatus.IMPORTED,
                matched_issue_id=issue.id,
                library_file_id=library_file.id,
            )
        )
        await session.commit()
        return int(job.id), int(target_root.id)


@pytest.mark.asyncio
async def test_preview_and_apply_completed_cleanup_are_authenticated_and_scoped(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    job_id, file_id = await _seed_missing_reference(sec_db)
    action = "dismiss_missing_references"

    preview_response = await authenticated_client.get(
        f"/api/v1/import/{job_id}/cleanup/{action}/preview"
    )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["affected_count"] == 1
    assert preview["examples"] == ["missing.cbz"]
    assert preview["confirmation_text"] == "APPLY CLEANUP"

    response = await authenticated_client.post(
        f"/api/v1/import/{job_id}/cleanup/{action}",
        headers=_csrf_header_for(authenticated_client),
        json={
            "preview_token": preview["preview_token"],
            "confirmation": "APPLY CLEANUP",
        },
    )

    assert response.status_code == 200
    assert response.json()["requires_import_retry"] is False
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file is not None
        assert file.status is ImportedFileStatus.SKIPPED


@pytest.mark.asyncio
async def test_cleanup_apply_rejects_missing_confirmation(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    job_id, _file_id = await _seed_missing_reference(sec_db)
    action = "dismiss_missing_references"
    preview = (
        await authenticated_client.get(f"/api/v1/import/{job_id}/cleanup/{action}/preview")
    ).json()

    response = await authenticated_client.post(
        f"/api/v1/import/{job_id}/cleanup/{action}",
        headers=_csrf_header_for(authenticated_client),
        json={"preview_token": preview["preview_token"]},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_mixed_folder_cleanup_api_resumes_only_resolved_files(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, file_id = await _seed_mixed_folder_file(sec_db)
    triggered_jobs: list[int] = []
    monkeypatch.setattr(
        "pullbox.api.v1.import_completed_cleanup.trigger_import_execute",
        triggered_jobs.append,
    )
    action = "resolve_mixed_folder_files"
    preview_response = await authenticated_client.get(
        f"/api/v1/import/{job_id}/cleanup/{action}/preview"
    )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["affected_file_count"] == 1

    response = await authenticated_client.post(
        f"/api/v1/import/{job_id}/cleanup/{action}",
        headers=_csrf_header_for(authenticated_client),
        json={
            "preview_token": preview["preview_token"],
            "confirmation": "APPLY CLEANUP",
        },
    )

    assert response.status_code == 200
    assert response.json()["requires_import_retry"] is True
    assert triggered_jobs == [job_id]
    async with sec_db() as session:
        imported_file = await session.get(ImportedFile, file_id)
        assert imported_file is not None
        assert imported_file.status is ImportedFileStatus.CONFIRMED


@pytest.mark.asyncio
async def test_clean_library_api_previews_and_starts_managed_copy(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    job_id, target_root_id = await _seed_referenced_library(sec_db, tmp_path)
    triggered_jobs: list[int] = []
    monkeypatch.setattr(
        "pullbox.api.v1.import_completed_cleanup.trigger_import_execute",
        triggered_jobs.append,
    )

    preview_response = await authenticated_client.get(
        f"/api/v1/import/{job_id}/clean-library/preview",
        params={"target_root_id": target_root_id},
    )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["eligible_file_count"] == 1
    assert preview["eligible_series_count"] == 1
    assert preview["source_preserved"] is True
    assert preview["confirmation_text"] == "BUILD CLEAN LIBRARY"

    response = await authenticated_client.post(
        f"/api/v1/import/{job_id}/clean-library",
        headers=_csrf_header_for(authenticated_client),
        json={
            "target_root_id": target_root_id,
            "preview_token": preview["preview_token"],
            "confirmation": "BUILD CLEAN LIBRARY",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["source_job_id"] == job_id
    assert result["job_id"] != job_id
    assert triggered_jobs == [result["job_id"]]
    async with sec_db() as session:
        created = await session.get(ImportJob, result["job_id"])
        assert created is not None
        assert created.file_handling_mode.value == "managed_copy"
        assert created.source_preserved is True
        assert created.progress_snapshot["clean_library_adoption_prepared"] is False
        created_file_count = await session.scalar(
            select(func.count())
            .select_from(ImportedFile)
            .where(ImportedFile.import_job_id == created.id)
        )
        assert created_file_count == 0


@pytest.mark.asyncio
async def test_finished_import_can_be_archived_and_restored_through_api(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    job_id, _file_id = await _seed_missing_reference(sec_db)
    headers = _csrf_header_for(authenticated_client)

    archived = await authenticated_client.post(
        f"/api/v1/import/{job_id}/archive",
        headers=headers,
    )
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert archived.json()["archived_at"] is not None

    restored = await authenticated_client.post(
        f"/api/v1/import/{job_id}/restore",
        headers=headers,
    )
    assert restored.status_code == 200
    assert restored.json()["archived"] is False
    assert restored.json()["archived_at"] is None
