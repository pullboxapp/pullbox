"""Daily, bounded provider membership discovery for monitored story arcs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from pullbox.core.comicvine_key import get_comicvine_api_key
from pullbox.core.scheduler import scheduled_task
from pullbox.database import get_session_factory
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcLifecycle
from pullbox.providers.metadata.comicvine import ComicVineError, ComicVineProvider
from pullbox.services.import_activity import has_active_import_scheduler_protection
from pullbox.services.story_arc_catalog import MAX_CATALOG_PARENTS, StoryArcCatalogService
from pullbox.services.story_arc_service import StoryArcServiceError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.sql.elements import ColumnElement

logger = structlog.get_logger(__name__)
_PAGE_SIZE = 25


def _active_monitored() -> tuple[ColumnElement[bool], ...]:
    return (
        StoryArc.monitored.is_(True),
        StoryArc.lifecycle == StoryArcLifecycle.ACTIVE,
        StoryArc.comicvine_id.is_not(None),
    )


async def _refresh_arc(
    factory: async_sessionmaker[AsyncSession], service: StoryArcCatalogService, arc_id: int
) -> int:
    """Snapshot, fetch outside the session, recheck consent, then persist."""
    async with factory() as session:
        arc = await session.scalar(
            select(StoryArc).where(StoryArc.id == arc_id, *_active_monitored())
        )
        if arc is None:
            return 0
        provider_id, revision = str(arc.comicvine_id), arc.revision
        known_parents = tuple(
            str(value)
            for value in await session.scalars(
                select(Series.comicvine_id)
                .join(Issue, Issue.series_id == Series.id)
                .join(IssueStoryArc, IssueStoryArc.issue_id == Issue.id)
                .where(IssueStoryArc.story_arc_id == arc_id, Series.comicvine_id.is_not(None))
                .distinct()
                .limit(MAX_CATALOG_PARENTS)
            )
        )
    preview = await service.preview(provider_id, known_series_provider_ids=known_parents)
    if await has_active_import_scheduler_protection(factory):
        return 0
    async with factory() as session:
        arc = await session.scalar(
            select(StoryArc).where(StoryArc.id == arc_id, *_active_monitored())
        )
        if arc is None or arc.revision != revision:
            return 0
        # Older imports may lack an arc-specific future destination. Only the
        # explicitly configured default managed root is a safe fallback. Never
        # infer it from an import source or the separate arc-copy directory.
        default_roots = list(
            await session.scalars(
                select(LibraryRoot.id)
                .where(LibraryRoot.is_default_managed_destination.is_(True))
                .limit(2)
            )
        )
        result = await service.refresh(
            session,
            arc_id,
            preview,
            expected_revision=revision,
            library_root_id=default_roots[0] if len(default_roots) == 1 else None,
        )
        await session.commit()
        # The shared wanted sweep observes new members after this commit and
        # rechecks monitoring, explicit skips, dates, and duplicate downloads.
        return len(result.added_membership_ids)


async def sync_story_arc_metadata() -> None:
    """Refresh each eligible arc once, isolating provider failures per arc."""
    factory = get_session_factory()
    if await has_active_import_scheduler_protection(factory):
        return
    async with factory() as session:
        api_key = await get_comicvine_api_key(session)
        ceiling = await session.scalar(select(func.max(StoryArc.id)).where(*_active_monitored()))
    if not api_key or ceiling is None:
        return
    provider = ComicVineProvider(api_key=api_key)
    service = StoryArcCatalogService(provider)
    cursor = refreshed = added = failed = 0
    try:
        while cursor < ceiling:
            async with factory() as session:
                ids = list(
                    await session.scalars(
                        select(StoryArc.id)
                        .where(StoryArc.id > cursor, StoryArc.id <= ceiling, *_active_monitored())
                        .order_by(StoryArc.id)
                        .limit(_PAGE_SIZE)
                    )
                )
            if not ids:
                break
            for arc_id in ids:
                if await has_active_import_scheduler_protection(factory):
                    return
                cursor = arc_id
                try:
                    added += await _refresh_arc(factory, service, arc_id)
                    refreshed += 1
                except (ComicVineError, StoryArcServiceError, SQLAlchemyError) as exc:
                    failed += 1
                    code = getattr(exc, "code", "provider_unavailable")
                    logger.warning(
                        "story_arc_metadata_refresh_failed", story_arc_id=arc_id, category=code
                    )
                    async with factory() as session:
                        arc = await session.get(StoryArc, arc_id)
                        if arc is not None:
                            arc.diagnostics = {
                                **arc.diagnostics,
                                "provider_refresh_error": {
                                    "code": str(code),
                                    "checked_at": datetime.now(UTC).isoformat(),
                                },
                            }
                            await session.commit()
    finally:
        await provider.close()
    logger.info("story_arc_metadata_refresh_done", refreshed=refreshed, added=added, failed=failed)


@scheduled_task(
    task_id="sync_story_arc_metadata",
    trigger="cron",
    display_name="Sync Story Arc Members",
    hour=1,
    minute=30,
)
async def scheduled_sync_story_arc_metadata() -> None:
    await sync_story_arc_metadata()
