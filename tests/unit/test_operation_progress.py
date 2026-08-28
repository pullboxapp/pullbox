"""Tests for the shared durable operation-progress contract."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, Float

from pullbox.models.operation_progress import (
    OperationProgress,
    OperationProgressState,
    OperationProgressType,
    OperationProgressVisibility,
)
from pullbox.services.operation_activity import acknowledge_operation, list_operation_activity
from pullbox.services.operation_progress import (
    OperationItemProgress,
    OperationProgressMeasure,
    OperationProgressUpdate,
    publish_operation_progress,
)


def _update(
    *,
    revision: int,
    overall_percent: float | None = 10,
    overall_current: int | None = None,
    overall_total: int | None = None,
    item_key: str | None = "file:1",
    item_percent: float | None = 25,
    item_phase: str = "scanning",
    state: OperationProgressState = OperationProgressState.RUNNING,
) -> OperationProgressUpdate:
    item = None
    if item_key is not None:
        item = OperationItemProgress(
            key=item_key,
            label="Batman 001.cbz",
            phase=item_phase,
            message="Scanning file",
            measure=OperationProgressMeasure(percent=item_percent, unit="percent"),
        )
    return OperationProgressUpdate(
        operation_type=OperationProgressType.IMPORT,
        operation_key="42",
        revision=revision,
        state=state,
        phase="file_matching",
        title="Folder import",
        message="Matching files to issues",
        detail_url="/import?resume_job_id=42",
        visibility=OperationProgressVisibility.PROMINENT,
        overall=OperationProgressMeasure(
            current=overall_current,
            total=overall_total,
            percent=overall_percent,
            unit="items",
        ),
        item=item,
    )


def test_operation_progress_model_uses_large_counters_and_float_percentages() -> None:
    columns = OperationProgress.__table__.columns

    assert isinstance(columns["overall_current"].type, BigInteger)
    assert isinstance(columns["overall_total"].type, BigInteger)
    assert isinstance(columns["item_current"].type, BigInteger)
    assert isinstance(columns["item_total"].type, BigInteger)
    assert isinstance(columns["overall_percent"].type, Float)
    assert isinstance(columns["item_percent"].type, Float)
    assert {
        "ix_operation_progress_activity",
        "ix_operation_progress_group",
    }.issubset({index.name for index in OperationProgress.__table__.indexes})


async def test_publish_operation_progress_rejects_stale_and_equal_revisions(db_session) -> None:
    first = await publish_operation_progress(db_session, _update(revision=3))
    stale = await publish_operation_progress(
        db_session,
        _update(revision=2, overall_percent=80),
    )
    equal = await publish_operation_progress(
        db_session,
        _update(revision=3, overall_percent=90),
    )

    assert first.accepted is True
    assert stale.accepted is False
    assert equal.accepted is False
    assert stale.operation.overall_percent == 10
    assert equal.operation.overall_percent == 10
    assert equal.operation.revision == 3


async def test_publish_operation_progress_keeps_overall_percentage_monotonic(db_session) -> None:
    await publish_operation_progress(db_session, _update(revision=1, overall_percent=60))
    result = await publish_operation_progress(
        db_session,
        _update(revision=2, overall_percent=40),
    )

    assert result.accepted is True
    assert result.operation.overall_percent == 60


async def test_new_active_phase_resets_completed_overall_percentage(db_session) -> None:
    await publish_operation_progress(
        db_session,
        _update(
            revision=1,
            overall_percent=100,
            state=OperationProgressState.COMPLETED,
        ),
    )

    result = await publish_operation_progress(
        db_session,
        _update(
            revision=2,
            overall_percent=12,
            state=OperationProgressState.RUNNING,
        ),
    )

    assert result.accepted is True
    assert result.operation.state is OperationProgressState.RUNNING
    assert result.operation.overall_percent == 12
    assert result.operation.completed_at is None


async def test_item_progress_only_resets_for_a_new_item_or_phase(db_session) -> None:
    await publish_operation_progress(
        db_session,
        _update(revision=1, item_percent=80),
    )
    same_item = await publish_operation_progress(
        db_session,
        _update(revision=2, item_percent=20),
    )
    same_item_percent = same_item.operation.item_percent
    new_phase = await publish_operation_progress(
        db_session,
        _update(revision=3, item_percent=15, item_phase="extracting"),
    )
    new_phase_percent = new_phase.operation.item_percent
    new_item = await publish_operation_progress(
        db_session,
        _update(revision=4, item_key="file:2", item_percent=5),
    )

    assert same_item_percent == 80
    assert new_phase_percent == 15
    assert new_item.operation.item_percent == 5


async def test_unknown_total_is_indeterminate_instead_of_fake_zero_percent(db_session) -> None:
    result = await publish_operation_progress(
        db_session,
        _update(
            revision=1,
            overall_percent=None,
            overall_current=8192,
            overall_total=None,
            item_percent=None,
        ),
    )

    assert result.operation.overall_current == 8192
    assert result.operation.overall_percent is None
    assert result.operation.overall_indeterminate is True
    assert result.operation.item_percent is None
    assert result.operation.item_indeterminate is True


async def test_completed_operation_finishes_at_one_hundred_percent(db_session) -> None:
    result = await publish_operation_progress(
        db_session,
        _update(
            revision=1,
            overall_percent=74,
            state=OperationProgressState.COMPLETED,
        ),
    )

    assert result.operation.overall_percent == 100
    assert result.operation.overall_indeterminate is False
    assert result.operation.completed_at is not None


async def test_activity_list_promotes_user_work_and_hides_quiet_success(db_session) -> None:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    prominent = _update(revision=1)
    await publish_operation_progress(db_session, prominent)
    quiet = OperationProgressUpdate(
        operation_type=OperationProgressType.UTILITY,
        operation_key="nightly-db",
        revision=1,
        state=OperationProgressState.RUNNING,
        phase="optimizing",
        title="Database maintenance",
        visibility=OperationProgressVisibility.QUIET,
        event_at=now,
    )
    await publish_operation_progress(db_session, quiet)

    activity = await list_operation_activity(db_session, now=now)

    assert [item.operation_key for item in activity.operations] == ["42"]
    assert activity.active_count == 1
    assert activity.spinner_count == 1


async def test_attention_failure_stays_visible_until_acknowledged(db_session) -> None:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    failed = OperationProgressUpdate(
        operation_type=OperationProgressType.POST_PROCESSING,
        operation_key="9",
        revision=1,
        state=OperationProgressState.FAILED,
        phase="failed",
        title="Post-processing Batman 001",
        attention_required=True,
        visibility=OperationProgressVisibility.QUIET,
        event_at=now - timedelta(days=2),
    )
    result = await publish_operation_progress(db_session, failed)

    before = await list_operation_activity(db_session, now=now)
    await acknowledge_operation(db_session, result.operation.id, at=now)
    after = await list_operation_activity(db_session, now=now)

    assert before.attention_count == 1
    assert [item.id for item in before.operations] == [result.operation.id]
    assert after.attention_count == 0
    assert after.operations == []


async def test_materially_new_failure_clears_an_old_acknowledgement(db_session) -> None:
    first = replace(
        _update(revision=1, state=OperationProgressState.FAILED),
        attention_required=True,
        message="First failure",
    )
    created = await publish_operation_progress(db_session, first)
    await acknowledge_operation(db_session, created.operation.id)

    repeated = await publish_operation_progress(
        db_session,
        replace(first, revision=2),
    )
    repeated_acknowledged_at = repeated.operation.acknowledged_at
    changed = await publish_operation_progress(
        db_session,
        replace(first, revision=3, message="A new failure occurred"),
    )

    assert repeated_acknowledged_at is not None
    assert changed.operation.acknowledged_at is None


async def test_recent_completion_expires_from_activity_list(db_session) -> None:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    recent = replace(
        _update(revision=1, state=OperationProgressState.COMPLETED),
        event_at=now - timedelta(seconds=5),
    )
    old = OperationProgressUpdate(
        operation_type=OperationProgressType.UTILITY,
        operation_key="old",
        revision=1,
        state=OperationProgressState.COMPLETED,
        phase="done",
        title="Old utility",
        event_at=now - timedelta(minutes=5),
    )
    await publish_operation_progress(db_session, recent)
    await publish_operation_progress(db_session, old)

    activity = await list_operation_activity(db_session, now=now)

    assert [item.operation_key for item in activity.operations] == ["42"]
