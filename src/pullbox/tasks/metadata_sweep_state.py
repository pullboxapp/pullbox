"""Durable, bounded nightly metadata sweep checkpoints (no schema migration)."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from pullbox.core.scheduler import get_scheduler
from pullbox.models.config import SystemConfig
from pullbox.models.series import Series

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

TASK_IDS = ("sync_new_issues", "refresh_metadata")


@dataclass
class MetadataSweep:
    cursor: int = 0
    upper_bound: int = 0
    retry_at: float = 0.0
    active: bool = False


async def load_sweep(session: AsyncSession, task_id: str) -> MetadataSweep:
    row = await session.get(SystemConfig, f"metadata_sweep_{task_id}")
    try:
        data = json.loads(row.value) if row else {}
        state = MetadataSweep(**data)
        if (
            type(state.cursor) is int
            and type(state.upper_bound) is int
            and type(state.active) is bool
            and isinstance(state.retry_at, int | float)
            and math.isfinite(state.retry_at)
            and state.retry_at >= 0
            and 0 <= state.cursor <= state.upper_bound
        ):
            return state
    except (TypeError, ValueError):
        pass
    return MetadataSweep()


async def start_sweep(session: AsyncSession, task_id: str) -> MetadataSweep:
    state = await load_sweep(session, task_id)
    if not state.active:
        upper = await session.scalar(
            select(func.max(Series.id)).where(Series.comicvine_id.isnot(None))
        )
        state = MetadataSweep(upper_bound=upper or 0, active=True)
        await save_sweep(session, task_id, state)
    await session.commit()
    return state


async def save_sweep(session: AsyncSession, task_id: str, state: MetadataSweep) -> None:
    key = f"metadata_sweep_{task_id}"
    row = await session.get(SystemConfig, key)
    value = json.dumps(asdict(state))
    if row is None:
        session.add(SystemConfig(key=key, value=value, value_type="string"))
    else:
        row.value = value


def schedule_sweep(task_id: str, state: MetadataSweep) -> None:
    if state.active:
        get_scheduler().schedule_task_continuation(
            task_id,
            run_at=max(
                datetime.now(UTC) + timedelta(seconds=60),
                datetime.fromtimestamp(state.retry_at, UTC),
            ),
            interval_seconds=60,
        )
    else:
        get_scheduler().clear_task_continuation(task_id)


async def recover_metadata_sweep_schedules() -> None:
    """Restore interrupted batches without waiting for tomorrow's cron."""
    from pullbox.database import get_session_factory

    async with get_session_factory()() as session:
        for task_id in TASK_IDS:
            state = await load_sweep(session, task_id)
            if state.active:
                schedule_sweep(task_id, state)
