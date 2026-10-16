"""Retention helpers for URL-free direct-search discoveries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete

from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession


async def prune_unstarted_direct_discoveries(
    session: AsyncSession,
    search_log_ids: Select[int] | Sequence[int],
) -> int:
    """Delete only discoveries that never progressed beyond search results."""
    result = await session.execute(
        delete(DirectAcquisitionAttempt).where(
            DirectAcquisitionAttempt.search_log_id.in_(search_log_ids),
            DirectAcquisitionAttempt.state == DirectAcquisitionState.DISCOVERED,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)
