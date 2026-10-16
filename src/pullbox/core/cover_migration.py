"""One-time migration to move covers from series folders to .covers/ directory.

Moves cover.{ext} and issue_*.{ext} files from series folders into the
centralized .covers/{series_id}/ directory. Sets a SystemConfig flag
when complete so the migration only runs once.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

_MIGRATION_KEY = "covers_migrated_to_dotcovers"
_IMAGE_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".webp"))


async def migrate_covers_to_dotcovers(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Move cover images from series folders to .covers/{series_id}/.

    Idempotent: skips files that already exist at the destination.
    Returns the total number of files moved.
    """
    from sqlalchemy import select

    from pullbox.models.config import SystemConfig
    from pullbox.models.series import Series
    from pullbox.services.cover_resolver import resolve_covers_dir

    async with session_factory() as session:
        # Check if already migrated
        migration_result = await session.execute(
            select(SystemConfig.value).where(SystemConfig.key == _MIGRATION_KEY)
        )
        if migration_result.scalar_one_or_none() == "true":
            return 0

        # Resolve target covers directory
        covers_base = await resolve_covers_dir(session)

        # Load all series with paths
        result = await session.execute(
            select(Series.id, Series.path).where(Series.path.isnot(None))
        )
        series_rows = result.all()

    if not series_rows:
        # Mark complete even if no series exist
        async with session_factory() as session:
            session.add(SystemConfig(key=_MIGRATION_KEY, value="true"))
            await session.commit()
        return 0

    moved = 0
    for series_id, series_path_str in series_rows:
        # The query excludes NULL paths; SQL predicates do not narrow row types.
        series_path = Path(cast("str", series_path_str))
        if not series_path.is_dir():
            continue

        dest_dir = covers_base / str(series_id)

        # Move series cover (cover.{ext} → series.{ext})
        for ext in _IMAGE_EXTENSIONS:
            src = series_path / f"cover{ext}"
            if src.is_file():
                dst = dest_dir / f"series{ext}"
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                    moved += 1
                    logger.debug(
                        "cover_migrated",
                        src=str(src),
                        dst=str(dst),
                        series_id=series_id,
                    )

        # Move issue covers (issue_*.{ext})
        for src in series_path.iterdir():
            if not src.is_file():
                continue
            if not src.name.startswith("issue_"):
                continue
            if src.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue

            dst = dest_dir / src.name
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved += 1
                logger.debug(
                    "cover_migrated",
                    src=str(src),
                    dst=str(dst),
                    series_id=series_id,
                )

    # Mark migration complete
    async with session_factory() as session:
        session.add(SystemConfig(key=_MIGRATION_KEY, value="true"))
        await session.commit()

    if moved:
        logger.info("cover_migration_complete", files_moved=moved)
    else:
        logger.info("cover_migration_complete", message="no covers to move")

    return moved
