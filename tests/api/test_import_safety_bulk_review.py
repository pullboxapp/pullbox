"""Category-scoped bulk review contracts for import safety blocks."""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from pullbox.models.audit_log import AuditLog
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


def _safety_block(
    category: ImportSafetyCategory,
    *,
    overrideable: bool,
) -> dict[str, object]:
    diagnostics = build_import_safety_diagnostics(
        category.value,
        code=category.value,
        overrideable_hint=overrideable,
    )
    diagnostics["overrideable"] = overrideable
    return diagnostics


async def _add_safety_file(
    session: AsyncSession,
    *,
    job: ImportJob,
    series: ImportedSeries,
    file_name: str,
    safety_block: dict[str, object],
) -> ImportedFile:
    item = ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path=f"/private/source/{file_name}",
        file_name=file_name,
        file_size=1,
        file_format="cbz",
        status=ImportedFileStatus.SAFETY_BLOCKED,
        diagnostics={"safe_existing_key": "kept", "safety_block": safety_block},
    )
    session.add(item)
    await session.flush()
    return item


async def _seed_scoped_safety_rows(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    async with factory() as session:
        job = ImportJob(
            source_path="/private/source",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
        )
        other_job = ImportJob(
            source_path="/other/private/source",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.REVIEW,
        )
        session.add_all([job, other_job])
        await session.flush()

        series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Scoped Series",
            status=ImportSeriesStatus.MATCHED,
            selected_for_import=True,
        )
        other_series = ImportedSeries(
            import_job_id=other_job.id,
            raw_series_name="Other Source Series",
            status=ImportSeriesStatus.MATCHED,
            selected_for_import=True,
        )
        session.add_all([series, other_series])
        await session.flush()

        eligible_one = await _add_safety_file(
            session,
            job=job,
            series=series,
            file_name="/mnt/private/Oversize One.cbz",
            safety_block=_safety_block(
                ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
                overrideable=True,
            ),
        )
        eligible_two = await _add_safety_file(
            session,
            job=job,
            series=series,
            file_name=r"C:\Users\Adam\Oversize Two.cbz",
            safety_block=_safety_block(
                ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
                overrideable=True,
            ),
        )
        ineligible_size = await _add_safety_file(
            session,
            job=job,
            series=series,
            file_name="Size Override Disabled.cbz",
            safety_block=_safety_block(
                ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
                overrideable=False,
            ),
        )
        dangerous_block = _safety_block(
            ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD,
            overrideable=False,
        )
        # A persisted true flag must never make a dangerous category eligible.
        dangerous_block["overrideable"] = True
        dangerous = await _add_safety_file(
            session,
            job=job,
            series=series,
            file_name="Dangerous Link.cbz",
            safety_block=dangerous_block,
        )
        other_source = await _add_safety_file(
            session,
            job=other_job,
            series=other_series,
            file_name="Other Source Oversize.cbz",
            safety_block=_safety_block(
                ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
                overrideable=True,
            ),
        )
        legacy = await _add_safety_file(
            session,
            job=job,
            series=series,
            file_name="Legacy Unstructured.cbz",
            safety_block={
                "reason": "decompressed size limit",
                "overrideable": True,
            },
        )
        await session.commit()
        return {
            "job_id": int(job.id),
            "series_id": int(series.id),
            "other_job_id": int(other_job.id),
            "eligible_one_id": int(eligible_one.id),
            "eligible_two_id": int(eligible_two.id),
            "ineligible_size_id": int(ineligible_size.id),
            "dangerous_id": int(dangerous.id),
            "other_source_id": int(other_source.id),
            "legacy_id": int(legacy.id),
        }


@pytest.mark.asyncio
async def test_bulk_safety_preview_and_confirmed_allow_once_are_scoped_and_audited(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_user,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.api.v1 import import_safety_bulk as bulk_api

    triggered_rematches: list[int] = []
    monkeypatch.setattr(
        bulk_api,
        "trigger_import_safety_bulk_rematch",
        triggered_rematches.append,
    )
    seeded = await _seed_scoped_safety_rows(sec_db)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value
    preview_url = f"/api/v1/import/{seeded['job_id']}/safety/categories/{category}/preview"

    preview_response = await authenticated_client.get(preview_url)

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["job_id"] == seeded["job_id"]
    assert preview["source_type"] == ImportSourceType.FILESYSTEM.value
    assert preview["category"] == category
    assert preview["action"] == "allow_once"
    assert preview["matching_count"] == 3
    assert preview["affected_count"] == 2
    assert preview["skipped_count"] == 1
    assert preview["overrideable"] is True
    assert preview["requires_confirmation"] is True
    assert preview["confirmation_text"] == "ALLOW ONCE"
    assert preview["examples"] == [
        "Oversize One.cbz",
        "Oversize Two.cbz",
        "Size Override Disabled.cbz",
    ]
    assert preview["preview_token"]
    assert "/mnt/private" not in preview_response.text
    assert "C:\\Users" not in preview_response.text

    missing_confirmation = await authenticated_client.post(
        f"/api/v1/import/{seeded['job_id']}/safety/categories/{category}/allow-once",
        headers=_csrf_header_for(authenticated_client),
        json={"preview_token": preview["preview_token"]},
    )
    assert missing_confirmation.status_code == 422

    response = await authenticated_client.post(
        f"/api/v1/import/{seeded['job_id']}/safety/categories/{category}/allow-once",
        headers=_csrf_header_for(authenticated_client),
        json={
            "preview_token": preview["preview_token"],
            "confirmation": "ALLOW ONCE",
        },
    )

    assert response.status_code == 200
    assert triggered_rematches == [seeded["job_id"]]
    payload = response.json()
    assert payload == {
        "job_id": seeded["job_id"],
        "source_type": ImportSourceType.FILESYSTEM.value,
        "category": category,
        "action": "allow_once",
        "affected_count": 2,
        "skipped_count": 1,
    }
    assert "/private" not in response.text

    async with sec_db() as session:
        eligible_files = [
            await session.get(ImportedFile, seeded["eligible_one_id"]),
            await session.get(ImportedFile, seeded["eligible_two_id"]),
        ]
        for item in eligible_files:
            assert item is not None
            assert item.status == ImportedFileStatus.SAFETY_APPROVED
            assert item.include_in_import is False
            assert item.error_message is None
            assert item.diagnostics["safe_existing_key"] == "kept"
            assert "safety_block" not in item.diagnostics
            safety_exception = item.diagnostics["safety_exception"]
            assert safety_exception["allowed_once"] is True
            assert safety_exception["previous_block"]["category"] == category

        for file_id in (
            seeded["ineligible_size_id"],
            seeded["dangerous_id"],
            seeded["other_source_id"],
            seeded["legacy_id"],
        ):
            item = await session.get(ImportedFile, file_id)
            assert item is not None
            assert item.status == ImportedFileStatus.SAFETY_BLOCKED
            assert "safety_exception" not in item.diagnostics

        series = await session.get(ImportedSeries, seeded["series_id"])
        assert series is not None
        assert series.selected_for_import is False
        assert series.diagnostics["rematch_pending"] is True

        audit_rows = list(
            (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        )
        assert len(audit_rows) == 2
        assert [audit.event_type for audit in audit_rows] == [
            "import_safety_bulk_override",
            "import_safety_bulk_override",
        ]
        assert [audit.user_id for audit in audit_rows] == [sec_user.id, sec_user.id]
        assert [audit.username for audit in audit_rows] == [
            sec_user.username,
            sec_user.username,
        ]
        assert [audit.detail for audit in audit_rows] == [
            "Import safety category override requested.",
            "Import safety category override completed.",
        ]
        expected_audit_metadata = {
            "action": "allow_once",
            "affected_count": 2,
            "category": category,
            "job_id": seeded["job_id"],
            "skipped_count": 1,
            "source_type": ImportSourceType.FILESYSTEM.value,
        }
        assert [json.loads(audit.metadata_json or "{}") for audit in audit_rows] == [
            {**expected_audit_metadata, "outcome": "requested"},
            {**expected_audit_metadata, "outcome": "completed"},
        ]
        for audit in audit_rows:
            assert "/private" not in (audit.metadata_json or "")
            assert "Oversize" not in (audit.metadata_json or "")


@pytest.mark.asyncio
async def test_dangerous_category_preview_is_bounded_but_never_overrideable(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_scoped_safety_rows(sec_db)
    dangerous = ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD.value

    response = await authenticated_client.get(
        f"/api/v1/import/{seeded['job_id']}/safety/categories/{dangerous}/preview"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["matching_count"] == 1
    assert payload["affected_count"] == 0
    assert payload["skipped_count"] == 1
    assert payload["overrideable"] is False
    assert payload["preview_token"] is None
    assert payload["confirmation_text"] is None

    apply_response = await authenticated_client.post(
        f"/api/v1/import/{seeded['job_id']}/safety/categories/{dangerous}/allow-once",
        headers=_csrf_header_for(authenticated_client),
        json={"preview_token": "not-a-preview", "confirmation": "ALLOW ONCE"},
    )
    assert apply_response.status_code == 422

    async with sec_db() as session:
        item = await session.get(ImportedFile, seeded["dangerous_id"])
        assert item is not None
        assert item.status == ImportedFileStatus.SAFETY_BLOCKED
        assert (await session.execute(select(AuditLog))).scalars().all() == []


@pytest.mark.asyncio
async def test_bulk_safety_apply_rejects_a_stale_preview_without_partial_mutation(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_scoped_safety_rows(sec_db)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value
    preview = (
        await authenticated_client.get(
            f"/api/v1/import/{seeded['job_id']}/safety/categories/{category}/preview"
        )
    ).json()

    async with sec_db() as session:
        job = await session.get(ImportJob, seeded["job_id"])
        series = await session.get(ImportedSeries, seeded["series_id"])
        assert job is not None
        assert series is not None
        await _add_safety_file(
            session,
            job=job,
            series=series,
            file_name="Arrived After Preview.cbz",
            safety_block=_safety_block(
                ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
                overrideable=True,
            ),
        )
        await session.commit()

    response = await authenticated_client.post(
        f"/api/v1/import/{seeded['job_id']}/safety/categories/{category}/allow-once",
        headers=_csrf_header_for(authenticated_client),
        json={
            "preview_token": preview["preview_token"],
            "confirmation": "ALLOW ONCE",
        },
    )

    assert response.status_code == 422
    assert "preview" in response.text.lower()
    async with sec_db() as session:
        eligible_one = await session.get(ImportedFile, seeded["eligible_one_id"])
        eligible_two = await session.get(ImportedFile, seeded["eligible_two_id"])
        assert eligible_one is not None
        assert eligible_two is not None
        assert eligible_one.status == ImportedFileStatus.SAFETY_BLOCKED
        assert eligible_two.status == ImportedFileStatus.SAFETY_BLOCKED
        audit_rows = list(
            (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        )
        assert [json.loads(item.metadata_json or "{}")["outcome"] for item in audit_rows] == [
            "requested",
            "interrupted",
        ]


@pytest.mark.asyncio
async def test_bulk_safety_preview_requires_an_existing_structured_category(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_scoped_safety_rows(sec_db)

    absent = await authenticated_client.get(
        f"/api/v1/import/{seeded['job_id']}/safety/categories/zero_byte/preview"
    )
    invalid = await authenticated_client.get(
        f"/api/v1/import/{seeded['job_id']}/safety/categories/not-a-category/preview"
    )

    assert absent.status_code == 422
    assert "structured category" in absent.text.lower()
    assert invalid.status_code == 422
