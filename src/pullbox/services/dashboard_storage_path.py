"""Storage path resolution for dashboard filesystem metrics."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.config import get_settings
from pullbox.models.library import LibraryRoot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_dashboard_storage_path(session: AsyncSession) -> Path:
    """Return the filesystem path dashboard storage cards should measure.

    The dashboard is library-focused, so prefer the explicit default managed
    destination. A managed-capable root is the fallback before a default has
    been selected; reference-only roots are measured last. Runtime settings
    remain the first-run fallback before a database-backed root exists.
    """
    root_path = (
        await session.execute(
            select(LibraryRoot.path)
            .where(LibraryRoot.enabled.is_(True))
            .order_by(
                LibraryRoot.is_default_managed_destination.desc(),
                LibraryRoot.allow_managed_writes.desc(),
                LibraryRoot.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if root_path:
        return Path(root_path)

    settings = get_settings()
    if settings.library_root.exists():
        return settings.library_root
    if settings.data_dir != Path("/data"):
        return settings.data_dir
    return Path.cwd()
