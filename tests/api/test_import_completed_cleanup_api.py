"""API contracts for completed-import recovery cleanup."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
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
