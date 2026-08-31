"""Server-rendered bulk safety review contracts for import Step 3."""

from __future__ import annotations

import os
import re
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


async def _seed_bulk_safety_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    include_dangerous: bool = False,
) -> dict[str, object]:
    blocked_count = 3 + int(include_dangerous)
    async with factory() as session:
        job = ImportJob(
            source_path="/private/import/source",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
        )
        session.add(job)
        await session.flush()

        series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Bulk Safety Series",
            source_folder="/private/import/source/Bulk Safety Series",
            status=ImportSeriesStatus.MATCHED,
            selected_for_import=True,
            files_total=blocked_count,
            files_matched=0,
            diagnostics={"safety_blocked_files": blocked_count},
        )
        session.add(series)
        await session.flush()

        files = [
            ImportedFile(
                import_job_id=job.id,
                import_series_id=series.id,
                file_path="/private/import/source/Oversize One.cbz",
                file_name="/private/import/source/Oversize One.cbz",
                file_size=4_000_000_000,
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                diagnostics={
                    "safety_block": _safety_block(
                        ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
                        overrideable=True,
                    )
                },
            ),
            ImportedFile(
                import_job_id=job.id,
                import_series_id=series.id,
                file_path="/private/import/source/Oversize Two.cbz",
                file_name=r"C:\Users\Adam\Oversize Two.cbz",
                file_size=4_000_000_001,
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                diagnostics={
                    "safety_block": _safety_block(
                        ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
                        overrideable=True,
                    )
                },
            ),
            ImportedFile(
                import_job_id=job.id,
                import_series_id=series.id,
                file_path="/private/import/source/Operator Disabled.cbz",
                file_name="/private/import/source/Operator Disabled.cbz",
                file_size=4_000_000_002,
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                diagnostics={
                    "safety_block": _safety_block(
                        ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
                        overrideable=False,
                    )
                },
            ),
        ]
        if include_dangerous:
            dangerous_block = _safety_block(
                ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD,
                overrideable=False,
            )
            # Persisted UI hints are not authoritative for dangerous categories.
            dangerous_block["overrideable"] = True
            files.append(
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=series.id,
                    file_path="/private/import/source/Dangerous Link.cbz",
                    file_name="/private/import/source/Dangerous Link.cbz",
                    file_size=1,
                    file_format="cbz",
                    status=ImportedFileStatus.SAFETY_BLOCKED,
                    diagnostics={"safety_block": dangerous_block},
                )
            )
        session.add_all(files)
        await session.commit()
        return {
            "job_id": int(job.id),
            "series_id": int(series.id),
            "file_ids": tuple(int(item.id) for item in files),
        }


def _preview_token(html: str) -> str:
    match = re.search(r'name="preview_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@pytest.mark.asyncio
async def test_safety_review_only_offers_bulk_preview_for_backend_overrideable_category(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_bulk_safety_job(sec_db, include_dangerous=True)

    response = await authenticated_client.get(
        f"/import/{seeded['job_id']}/review-partial?status=safety_blocked"
    )

    assert response.status_code == 200
    assert (
        'data-testid="import-review-safety-bulk-preview-decompression_size_limit"' in response.text
    )
    assert "Trusted override available for eligible files" in response.text
    assert "2 of 3 files can be allowed once" in response.text
    assert (
        f'action="/import/{seeded["job_id"]}/safety/categories/'
        "decompression_size_limit/preview" in response.text
    )
    assert 'data-testid="import-review-safety-bulk-preview-dangerous_path_or_payload"' not in (
        response.text
    )
    assert "dangerous_path_or_payload/preview" not in response.text
    assert 'name="preview_token"' not in response.text
    assert 'name="confirmation"' not in response.text


@pytest.mark.asyncio
async def test_bulk_safety_preview_is_signed_exact_sanitized_and_read_only(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_bulk_safety_job(sec_db)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value

    response = await authenticated_client.get(
        f"/import/{seeded['job_id']}/safety/categories/{category}/preview",
        params={"status": "safety_blocked", "sort": "series", "page": 1},
    )

    assert response.status_code == 200
    assert 'data-testid="import-review-safety-bulk-confirmation"' in response.text
    assert "2 of 3 files will be allowed once" in response.text
    assert "1 file will stay blocked" in response.text
    assert "Oversize One.cbz" in response.text
    assert "Oversize Two.cbz" in response.text
    assert "/private/import" not in response.text
    assert r"C:\Users\Adam" not in response.text
    assert _preview_token(response.text)
    assert 'name="confirmation"' in response.text
    assert 'pattern="ALLOW ONCE"' in response.text
    assert 'autocomplete="off"' in response.text
    assert 'aria-describedby="safety-bulk-confirmation-help-decompression_size_limit"' in (
        response.text
    )

    async with sec_db() as session:
        rows = [await session.get(ImportedFile, file_id) for file_id in seeded["file_ids"]]
        assert all(row is not None for row in rows)
        assert all(row.status == ImportedFileStatus.SAFETY_BLOCKED for row in rows if row)
        assert (await session.execute(select(AuditLog))).scalars().all() == []


@pytest.mark.asyncio
async def test_bulk_safety_confirmation_must_match_exact_text_before_mutation(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.ui import import_routes

    seeded = await _seed_bulk_safety_job(sec_db)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value
    preview = await authenticated_client.get(
        f"/import/{seeded['job_id']}/safety/categories/{category}/preview"
    )
    triggered_rematches: list[int] = []
    monkeypatch.setattr(
        import_routes,
        "trigger_import_safety_bulk_rematch",
        triggered_rematches.append,
    )

    response = await authenticated_client.post(
        f"/import/{seeded['job_id']}/safety/categories/{category}/allow-once",
        data={
            "preview_token": _preview_token(preview.text),
            "confirmation": "allow once",
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert "Type ALLOW ONCE exactly" in response.text
    assert triggered_rematches == []
    async with sec_db() as session:
        rows = [await session.get(ImportedFile, file_id) for file_id in seeded["file_ids"]]
        assert all(row is not None for row in rows)
        assert all(row.status == ImportedFileStatus.SAFETY_BLOCKED for row in rows if row)
        assert (await session.execute(select(AuditLog))).scalars().all() == []


@pytest.mark.asyncio
async def test_confirmed_bulk_safety_action_audits_actor_refreshes_and_triggers_rematch(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    sec_user,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.ui import import_routes

    seeded = await _seed_bulk_safety_job(sec_db)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value
    preview = await authenticated_client.get(
        f"/import/{seeded['job_id']}/safety/categories/{category}/preview"
    )
    triggered_rematches: list[int] = []
    monkeypatch.setattr(
        import_routes,
        "trigger_import_safety_bulk_rematch",
        triggered_rematches.append,
    )

    response = await authenticated_client.post(
        f"/import/{seeded['job_id']}/safety/categories/{category}/allow-once",
        data={
            "preview_token": _preview_token(preview.text),
            "confirmation": "ALLOW ONCE",
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    assert 'data-testid="import-collection-review"' in response.text
    assert 'data-testid="import-review-safety-rematch-spinner"' in response.text
    assert "/private/import" not in response.text
    assert r"C:\Users\Adam" not in response.text
    assert triggered_rematches == [seeded["job_id"]]

    async with sec_db() as session:
        rows = [await session.get(ImportedFile, file_id) for file_id in seeded["file_ids"]]
        assert all(row is not None for row in rows)
        assert [row.status for row in rows if row].count(ImportedFileStatus.SAFETY_APPROVED) == 2
        assert [row.status for row in rows if row].count(ImportedFileStatus.SAFETY_BLOCKED) == 1
        audit_rows = list(
            (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        )
        assert [row.user_id for row in audit_rows] == [sec_user.id, sec_user.id]
        assert [row.username for row in audit_rows] == [
            sec_user.username,
            sec_user.username,
        ]
        assert [row.source_ip for row in audit_rows] == ["127.0.0.1", "127.0.0.1"]
        assert all("/private" not in (row.metadata_json or "") for row in audit_rows)


@pytest.mark.asyncio
async def test_stale_bulk_safety_preview_refreshes_review_without_mutation(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.ui import import_routes

    seeded = await _seed_bulk_safety_job(sec_db)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value
    preview = await authenticated_client.get(
        f"/import/{seeded['job_id']}/safety/categories/{category}/preview"
    )
    triggered_rematches: list[int] = []
    monkeypatch.setattr(
        import_routes,
        "trigger_import_safety_bulk_rematch",
        triggered_rematches.append,
    )

    response = await authenticated_client.post(
        f"/import/{seeded['job_id']}/safety/categories/{category}/allow-once",
        data={
            "preview_token": _preview_token(preview.text) + "tampered",
            "confirmation": "ALLOW ONCE",
        },
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    assert 'data-testid="import-collection-review"' in response.text
    assert 'role="alert"' in response.text
    assert "The safety preview is invalid" in response.text
    assert (
        'data-testid="import-review-safety-bulk-preview-decompression_size_limit"' in response.text
    )
    assert triggered_rematches == []
    async with sec_db() as session:
        rows = [await session.get(ImportedFile, file_id) for file_id in seeded["file_ids"]]
        assert all(row is not None for row in rows)
        assert all(row.status == ImportedFileStatus.SAFETY_BLOCKED for row in rows if row)
        assert (await session.execute(select(AuditLog))).scalars().all() == []


@pytest.mark.asyncio
async def test_dangerous_bulk_safety_routes_fail_closed_without_mutation(
    authenticated_client: AsyncClient,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_bulk_safety_job(sec_db, include_dangerous=True)
    category = ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD.value

    preview = await authenticated_client.get(
        f"/import/{seeded['job_id']}/safety/categories/{category}/preview"
    )
    apply = await authenticated_client.post(
        f"/import/{seeded['job_id']}/safety/categories/{category}/allow-once",
        data={"preview_token": "not-a-preview", "confirmation": "ALLOW ONCE"},
        headers=_csrf_header_for(authenticated_client),
    )

    assert preview.status_code == 404
    assert apply.status_code == 404
    async with sec_db() as session:
        rows = [await session.get(ImportedFile, file_id) for file_id in seeded["file_ids"]]
        assert all(row is not None for row in rows)
        assert all(row.status == ImportedFileStatus.SAFETY_BLOCKED for row in rows if row)
        assert (await session.execute(select(AuditLog))).scalars().all() == []


@pytest.mark.asyncio
async def test_bulk_safety_ui_rejects_api_key_only_operator_actions(
    unauthenticated_client: AsyncClient,
    sec_api_key: str,
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_bulk_safety_job(sec_db)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value
    headers = {"X-Api-Key": sec_api_key}

    preview = await unauthenticated_client.get(
        f"/import/{seeded['job_id']}/safety/categories/{category}/preview",
        headers=headers,
    )
    apply = await unauthenticated_client.post(
        f"/import/{seeded['job_id']}/safety/categories/{category}/allow-once",
        data={"preview_token": "not-a-preview", "confirmation": "ALLOW ONCE"},
        headers=headers,
    )

    assert preview.status_code == 401
    assert apply.status_code == 401
    async with sec_db() as session:
        rows = [await session.get(ImportedFile, file_id) for file_id in seeded["file_ids"]]
        assert all(row is not None for row in rows)
        assert all(row.status == ImportedFileStatus.SAFETY_BLOCKED for row in rows if row)
        assert (await session.execute(select(AuditLog))).scalars().all() == []
