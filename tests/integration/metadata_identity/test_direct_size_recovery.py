"""Retained size-review artifacts must never enter download-route recovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectAcquisitionState
from pullbox.models.pending_match import PendingMatch
from pullbox.services.intervention_service import InterventionService
from tests.integration.metadata_identity.test_direct_paired_metadata import (
    approve_size,
    direct_artifact,
    execute,
    no_global_sync,  # noqa: F401
    paired_download_setting,  # noqa: F401
)


@pytest.mark.usefixtures("paired_download_setting")
async def test_size_recovery_refuses_route_refresh_before_creating_attempt(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await direct_artifact(factory, tmp_path, large=True)
    await execute(factory, tmp_path, attempt_id, artifact_id)
    original = source.read_bytes()
    planner = AsyncMock()
    runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
    async with factory() as session:
        pending = await session.scalar(select(PendingMatch))
        before = await session.scalar(select(func.count()).select_from(DirectAcquisitionAttempt))
        with pytest.raises(ValueError, match="size approval"):
            await InterventionService(
                direct_planner=planner, direct_runner_getter=lambda: runner
            ).retry_direct_recovery(session, pending.id)
        assert (
            await session.scalar(select(func.count()).select_from(DirectAcquisitionAttempt))
            == before
        )
        assert pending.status == "pending"
    planner.assert_not_awaited()
    runner.dispatch.assert_not_awaited()
    assert source.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("paired_enabled", [True, False])
async def test_size_worker_rejects_changed_file_even_without_paired_writer(
    identity_probe_db, tmp_path, monkeypatch, paired_enabled
):
    from pullbox.config import get_settings
    from pullbox.models.library import LibraryFile

    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await direct_artifact(factory, tmp_path, large=True)
    await execute(factory, tmp_path, attempt_id, artifact_id)
    await approve_size(factory)
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_DIRECT_WRITER_ENABLED", str(paired_enabled).lower())
    get_settings.cache_clear()
    source.write_bytes(b"changed after approval")
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.INTERVENTION
    assert source.read_bytes() == b"changed after approval"
    async with factory() as session:
        assert await session.scalar(select(LibraryFile)) is None
        attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
        assert attempt.failure_code == "artifact_size_approval_changed"


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("damage", ["missing", "changed", "no_signature", "symlink"])
async def test_size_approval_revalidates_retained_file_before_dispatch(
    identity_probe_db, tmp_path, damage
):
    from pullbox.services.direct_artifact_quarantine import DirectArtifactValidationError

    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await direct_artifact(factory, tmp_path, large=True)
    await execute(factory, tmp_path, attempt_id, artifact_id)
    if damage == "missing":
        source.unlink()
    elif damage == "changed":
        source.write_bytes(b"changed")
    elif damage == "symlink":
        alternate = source.with_name("other.cbz")
        source.rename(alternate)
        source.symlink_to(alternate)
    runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
    planner = AsyncMock()
    async with factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
        if damage == "no_signature":
            review = dict(attempt.plan_snapshot["safety_review"])
            review.pop("source_signature")
            attempt.plan_snapshot = {**attempt.plan_snapshot, "safety_review": review}
            await session.commit()
        pending = await session.scalar(select(PendingMatch))
        with pytest.raises(DirectArtifactValidationError):
            await InterventionService(
                direct_planner=planner, direct_runner_getter=lambda: runner
            ).approve_match(session, pending.id)
        assert attempt.state is DirectAcquisitionState.INTERVENTION
        assert pending.status == "pending"
        assert not attempt.plan_snapshot["safety_review"].get("allowed_once")
    runner.dispatch.assert_not_awaited()
    planner.assert_not_awaited()
