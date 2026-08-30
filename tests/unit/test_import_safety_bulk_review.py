"""Bounded-page and cooperative-stop contracts for bulk safety review."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

import pullbox.services.import_safety_bulk_review as bulk_review_service
from pullbox.core.exceptions import ValidationError
from pullbox.models.audit_log import AuditLog
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.user import User
from pullbox.services.import_safety_bulk_review import (
    ImportSafetyBulkInterruptedError,
    allow_import_safety_category_once,
    preview_import_safety_category,
)
from pullbox.services.import_safety_diagnostics import (
    ImportSafetyCategory,
    build_import_safety_diagnostics,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_size_blocks(session: AsyncSession, *, count: int) -> int:
    if await session.get(User, 42) is None:
        session.add(
            User(
                id=42,
                username="bulk-operator",
                password_hash="not-used-in-service-tests",
            )
        )
    job = ImportJob(
        source_path="/private/source",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    session.add(job)
    await session.flush()
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Page Bound",
        status=ImportSeriesStatus.MATCHED,
        selected_for_import=True,
    )
    session.add(series)
    await session.flush()
    for index in range(count):
        block = build_import_safety_diagnostics(
            ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value,
            code=ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value,
            overrideable_hint=True,
        )
        session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=series.id,
                file_path=f"/private/source/issue-{index}.cbz",
                file_name=f"issue-{index}.cbz",
                file_size=1,
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                diagnostics={"safety_block": block},
            )
        )
    await session.commit()
    return int(job.id)


@pytest.mark.asyncio
async def test_allow_once_processes_only_bounded_pages(db_session: AsyncSession) -> None:
    job_id = await _seed_size_blocks(db_session, count=5)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT
    preview = await preview_import_safety_category(
        db_session,
        job_id,
        category,
        actor_id=42,
    )
    assert preview.preview_token is not None

    result = await allow_import_safety_category_once(
        db_session,
        job_id,
        category,
        actor_id=42,
        preview_token=preview.preview_token,
        page_size=2,
    )

    assert result.affected_count == 5
    assert result.skipped_count == 0
    assert result.pages_processed == 3
    approved = (
        await db_session.execute(
            select(func.count(ImportedFile.id)).where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.status == ImportedFileStatus.SAFETY_APPROVED,
            )
        )
    ).scalar_one()
    assert approved == 5


@pytest.mark.asyncio
async def test_allow_once_observes_job_control_between_committed_pages(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _seed_size_blocks(db_session, count=5)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT
    preview = await preview_import_safety_category(
        db_session,
        job_id,
        category,
        actor_id=42,
    )
    assert preview.preview_token is not None

    original_commit = db_session.commit
    commit_calls = 0

    async def request_cancel_after_first_page() -> None:
        nonlocal commit_calls
        await original_commit()
        commit_calls += 1
        if commit_calls == 2:
            job = await db_session.get(ImportJob, job_id, populate_existing=True)
            assert job is not None
            job.control_request = ImportControlRequest.CANCEL
            await original_commit()

    monkeypatch.setattr(db_session, "commit", request_cancel_after_first_page)

    with pytest.raises(ImportSafetyBulkInterruptedError) as raised:
        await allow_import_safety_category_once(
            db_session,
            job_id,
            category,
            actor_id=42,
            preview_token=preview.preview_token,
            page_size=2,
        )

    assert raised.value.reason == "job_state_changed"
    assert raised.value.result.affected_count == 2
    assert raised.value.result.skipped_count == 3
    assert raised.value.result.pages_processed == 1
    approved = (
        await db_session.execute(
            select(func.count(ImportedFile.id)).where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.status == ImportedFileStatus.SAFETY_APPROVED,
            )
        )
    ).scalar_one()
    assert approved == 2


@pytest.mark.asyncio
async def test_preview_observes_job_control_between_digest_pages(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _seed_size_blocks(db_session, count=5)
    original_load_review_job = bulk_review_service._load_review_job
    review_checks = 0

    async def request_cancel_at_first_digest_boundary(
        session: AsyncSession,
        requested_job_id: int,
    ) -> ImportJob:
        nonlocal review_checks
        review_checks += 1
        if review_checks == 2:
            job = await session.get(ImportJob, job_id, populate_existing=True)
            assert job is not None
            job.control_request = ImportControlRequest.CANCEL
            await session.flush()
        return await original_load_review_job(session, requested_job_id)

    monkeypatch.setattr(bulk_review_service, "IMPORT_SAFETY_SNAPSHOT_PAGE_SIZE", 2)
    monkeypatch.setattr(
        bulk_review_service,
        "_load_review_job",
        request_cancel_at_first_digest_boundary,
    )

    with pytest.raises(ValidationError, match="pending control request"):
        await preview_import_safety_category(
            db_session,
            job_id,
            ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
            actor_id=42,
        )

    assert review_checks == 2
    blocked = (
        await db_session.execute(
            select(func.count(ImportedFile.id)).where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
            )
        )
    ).scalar_one()
    assert blocked == 5


@pytest.mark.asyncio
async def test_allow_once_preview_token_is_bound_to_the_previewing_actor(
    db_session: AsyncSession,
) -> None:
    job_id = await _seed_size_blocks(db_session, count=1)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT
    preview = await preview_import_safety_category(
        db_session,
        job_id,
        category,
        actor_id=42,
    )
    assert preview.preview_token is not None

    with pytest.raises(ValidationError, match="does not match this job and category"):
        await allow_import_safety_category_once(
            db_session,
            job_id,
            category,
            actor_id=43,
            preview_token=preview.preview_token,
        )

    item = (
        await db_session.execute(select(ImportedFile).where(ImportedFile.import_job_id == job_id))
    ).scalar_one()
    assert item.status == ImportedFileStatus.SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_first_mutation_page_cannot_commit_without_a_prior_durable_audit(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _seed_size_blocks(db_session, count=5)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT
    preview = await preview_import_safety_category(
        db_session,
        job_id,
        category,
        actor_id=42,
    )
    assert preview.preview_token is not None

    original_commit = db_session.commit
    commit_calls = 0

    async def crash_after_first_mutation_commit() -> None:
        nonlocal commit_calls
        await original_commit()
        commit_calls += 1
        if commit_calls == 2:
            raise RuntimeError("simulated worker death after first mutation commit")

    monkeypatch.setattr(db_session, "commit", crash_after_first_mutation_commit)

    with pytest.raises(RuntimeError, match="simulated worker death"):
        await allow_import_safety_category_once(
            db_session,
            job_id,
            category,
            actor_id=42,
            actor_username="bulk-operator",
            source_ip="127.0.0.1",
            preview_token=preview.preview_token,
            page_size=2,
        )

    audit_rows = list((await db_session.execute(select(AuditLog))).scalars().all())
    assert len(audit_rows) == 1
    assert audit_rows[0].event_type == "import_safety_bulk_override"
    assert '"outcome": "requested"' in (audit_rows[0].metadata_json or "")
    approved = (
        await db_session.execute(
            select(func.count(ImportedFile.id)).where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.status == ImportedFileStatus.SAFETY_APPROVED,
            )
        )
    ).scalar_one()
    assert approved == 2


@pytest.mark.asyncio
async def test_post_request_control_change_records_an_interrupted_result(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _seed_size_blocks(db_session, count=2)
    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT
    preview = await preview_import_safety_category(
        db_session,
        job_id,
        category,
        actor_id=42,
    )
    assert preview.preview_token is not None

    original_commit = db_session.commit
    commit_calls = 0

    async def request_cancel_after_requested_audit() -> None:
        nonlocal commit_calls
        await original_commit()
        commit_calls += 1
        if commit_calls == 1:
            job = await db_session.get(ImportJob, job_id, populate_existing=True)
            assert job is not None
            job.control_request = ImportControlRequest.CANCEL
            await original_commit()

    monkeypatch.setattr(db_session, "commit", request_cancel_after_requested_audit)

    with pytest.raises(ValidationError, match="pending control request"):
        await allow_import_safety_category_once(
            db_session,
            job_id,
            category,
            actor_id=42,
            preview_token=preview.preview_token,
        )

    audit_rows = list(
        (await db_session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
    )
    assert [item.detail for item in audit_rows] == [
        "Import safety category override requested.",
        "Import safety category override interrupted.",
    ]
    assert all(
        item.status == ImportedFileStatus.SAFETY_BLOCKED
        for item in (
            await db_session.execute(
                select(ImportedFile).where(ImportedFile.import_job_id == job_id)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_scope_digest_rejects_identity_swap_with_unchanged_aggregate_counts(
    db_session: AsyncSession,
) -> None:
    job_id = await _seed_size_blocks(db_session, count=4)
    files = list(
        (
            await db_session.execute(
                select(ImportedFile)
                .where(ImportedFile.import_job_id == job_id)
                .order_by(ImportedFile.id)
            )
        )
        .scalars()
        .all()
    )
    fixed_timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    zero_block = build_import_safety_diagnostics(
        ImportSafetyCategory.ZERO_BYTE.value,
        code=ImportSafetyCategory.ZERO_BYTE.value,
        overrideable_hint=False,
    )
    files[2].diagnostics = {"safety_block": zero_block}
    for item in files:
        item.updated_at = fixed_timestamp
    await db_session.commit()

    category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT
    preview = await preview_import_safety_category(
        db_session,
        job_id,
        category,
        actor_id=42,
    )
    assert preview.matching_count == 3
    assert preview.affected_count == 3
    assert preview.preview_token is not None

    size_block = build_import_safety_diagnostics(
        category.value,
        code=category.value,
        overrideable_hint=True,
    )
    files[1].diagnostics = {"safety_block": zero_block}
    files[2].diagnostics = {"safety_block": size_block}
    files[1].updated_at = fixed_timestamp
    files[2].updated_at = fixed_timestamp
    await db_session.commit()

    with pytest.raises(ValidationError, match="changed"):
        await allow_import_safety_category_once(
            db_session,
            job_id,
            category,
            actor_id=42,
            preview_token=preview.preview_token,
        )

    audit_rows = list(
        (await db_session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
    )
    assert [item.detail for item in audit_rows] == [
        "Import safety category override requested.",
        "Import safety category override interrupted.",
    ]
    assert all(item.status == ImportedFileStatus.SAFETY_BLOCKED for item in files)
