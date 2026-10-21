"""Snapshot and expiry guarantees for the header activity feed."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from pullbox.models.operation_progress import (
    OperationProgress,
    OperationProgressState,
    OperationProgressType,
)
from pullbox.services.operation_activity import acknowledge_operation, list_operation_activity


def operation(key="1", **kwargs):
    return OperationProgress(
        operation_type=OperationProgressType.DOWNLOAD,
        operation_key=key,
        state=kwargs.pop("state", OperationProgressState.RUNNING),
        phase="downloading",
        title=f"Download {key}",
        last_event_at=datetime.now(UTC),
        **kwargs,
    )


async def test_rows_and_counts_share_snapshot_when_work_finishes(db_session):
    db_session.add(operation())
    await db_session.flush()

    class FinishAfterRead:
        def __init__(self):
            self.reads = 0

        async def execute(self, statement):
            result = await db_session.execute(statement)
            self.reads += 1
            if self.reads == 1:
                await db_session.execute(delete(OperationProgress))
            return result

    session = FinishAfterRead()
    activity = await list_operation_activity(session)

    assert len(activity.operations) == 1
    assert activity.active_count == 1
    assert activity.spinner_count == 1
    assert session.reads == 1


async def test_completion_disappears_at_grace_boundary(db_session):
    now = datetime.now(UTC)
    db_session.add(
        operation(state=OperationProgressState.COMPLETED, completed_at=now - timedelta(seconds=15))
    )
    await db_session.flush()

    activity = await list_operation_activity(db_session, now=now)

    assert activity.operations == []


async def test_paging_keeps_global_counts_and_off_page_expiry(db_session):
    now = datetime.now(UTC)
    db_session.add_all([operation(str(i)) for i in range(27)])
    db_session.add(
        operation(
            "recent",
            state=OperationProgressState.COMPLETED,
            completed_at=now - timedelta(seconds=10),
        )
    )
    await db_session.flush()

    first = await list_operation_activity(db_session, now=now)
    second = await list_operation_activity(db_session, now=now, offset=20)
    gone = await list_operation_activity(db_session, now=now, offset=100)

    assert len(first.operations) == 20
    assert len(second.operations) == 8
    assert {o.id for o in first.operations}.isdisjoint(o.id for o in second.operations)
    for page in (first, second, gone):
        assert page.active_count == 27
        assert page.spinner_count == 27
        assert page.total_count == 28
        assert page.refresh_after_ms == 5000
    assert gone.operations == []


async def test_empty_activity_has_no_expiry(db_session):
    activity = await list_operation_activity(db_session)
    assert activity.operations == []
    assert activity.total_count == activity.active_count == 0
    assert activity.refresh_after_ms is None


async def test_dismiss_notification_hides_recent_failure_immediately(db_session):
    row = operation(
        state=OperationProgressState.FAILED, attention_required=True, completed_at=datetime.now(UTC)
    )
    db_session.add(row)
    await db_session.flush()
    await acknowledge_operation(db_session, row.id)
    activity = await list_operation_activity(db_session)
    assert activity.operations == []
    assert activity.attention_count == activity.total_count == 0
    assert row.state is OperationProgressState.FAILED
    assert row.attention_required


@pytest.mark.parametrize("acknowledged", [False, True])
async def test_attention_does_not_expire_until_acknowledged(db_session, acknowledged):
    now = datetime.now(UTC)
    db_session.add(
        operation(
            state=OperationProgressState.FAILED,
            attention_required=True,
            completed_at=now - timedelta(seconds=5),
            acknowledged_at=now if acknowledged else None,
        )
    )
    await db_session.flush()
    activity = await list_operation_activity(db_session, now=now)
    assert activity.attention_count == (0 if acknowledged else 1)
    assert activity.refresh_after_ms is None
    later = await list_operation_activity(db_session, now=now + timedelta(seconds=20))
    assert len(later.operations) == (0 if acknowledged else 1)
