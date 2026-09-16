"""Issue service — retrieval and status management for issues."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from pullbox.core.exceptions import NotFoundError
from pullbox.models.issue import Issue, IssueStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class IssueService:
    """Manages issue retrieval and status transitions."""

    @staticmethod
    async def get_by_id(session: AsyncSession, issue_id: int) -> Issue:
        """Get an issue by ID."""
        issue = await session.get(Issue, issue_id)
        if not issue:
            raise NotFoundError("Issue", issue_id)
        # noinspection PyTypeChecker
        return issue

    @staticmethod
    async def get_for_series(
        session: AsyncSession,
        series_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Issue], int]:
        """Get all issues for a series with pagination."""
        total = (
            await session.execute(select(func.count(Issue.id)).where(Issue.series_id == series_id))
        ).scalar_one()

        result = await session.execute(
            select(Issue)
            .options(joinedload(Issue.library_file))
            .where(Issue.series_id == series_id)
            .order_by(Issue.issue_number, Issue.issue_number_text, Issue.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.unique().scalars().all()), total

    @staticmethod
    async def get_wanted(session: AsyncSession) -> list[Issue]:
        """Get all issues with WANTED status across all series."""
        result = await session.execute(
            select(Issue)
            .where(Issue.status == IssueStatus.WANTED)
            .order_by(
                Issue.series_id,
                Issue.issue_number,
                Issue.issue_number_text,
                Issue.id,
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def mark_status(
        session: AsyncSession,
        issue_id: int,
        status: IssueStatus,
    ) -> Issue:
        """Update an issue's status."""
        issue = await session.get(Issue, issue_id)
        if not issue:
            raise NotFoundError("Issue", issue_id)

        old_status = issue.status
        issue.status = status
        logger.info(
            "issue_status_changed",
            issue_id=issue_id,
            old_status=old_status,
            new_status=status,
        )
        # noinspection PyTypeChecker
        return issue
