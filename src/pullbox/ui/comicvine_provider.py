"""Shared Comic Vine provider lifecycle for UI discovery."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from pullbox.services.comicvine_persistent_cache import PersistentComicVineCacheProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ComicVineNotConfiguredError(RuntimeError):
    """Raised when a Comic Vine-backed UI action has no configured API key."""


@asynccontextmanager
async def open_comicvine_ui_provider(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[Any]:
    """Open the configured Comic Vine provider for one UI operation."""
    from pullbox.core.comicvine_key import get_comicvine_api_key

    api_key = await get_comicvine_api_key(session)
    await session.rollback()
    if not api_key:
        raise ComicVineNotConfiguredError("Comic Vine is not configured")

    # Import lazily so provider test doubles remain isolated from app startup.
    from pullbox.providers.metadata.comicvine import ComicVineProvider

    provider = ComicVineProvider(api_key=api_key)
    opened: Any = (
        PersistentComicVineCacheProvider(provider, session_factory)
        if session_factory is not None
        else provider
    )
    try:
        yield opened
    finally:
        await provider.close()
