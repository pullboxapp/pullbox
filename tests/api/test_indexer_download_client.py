"""API contracts for pinning an indexer's grabs to one download client."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.api.v1 import clients as clients_api
from pullbox.api.v1 import indexers as indexers_api
from pullbox.core.exceptions import ValidationError
from pullbox.models import Base
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType
from pullbox.models.indexer import IndexerConfig
from pullbox.schemas.indexer import IndexerCreate, IndexerUpdate

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]


async def _seed_clients(session: AsyncSession) -> tuple[DownloadClientConfig, DownloadClientConfig]:
    torrent = DownloadClientConfig(
        name="qBittorrent (tracker)",
        client_type=DownloadClientType.QBITTORRENT,
        url="http://qbit-tracker.test",
        priority=90,
    )
    usenet = DownloadClientConfig(
        name="SABnzbd",
        client_type=DownloadClientType.SABNZBD,
        url="http://sab.test",
    )
    session.add_all([torrent, usenet])
    await session.flush()
    return torrent, usenet


def _torznab(**overrides: object) -> IndexerCreate:
    payload: dict[str, object] = {
        "name": "Private Tracker",
        "indexer_type": "torznab",
        "url": "http://tracker.example",
        "api_key": "tracker-secret",
    }
    payload.update(overrides)
    return IndexerCreate.model_validate(payload)


@pytest.mark.asyncio
class TestIndexerDownloadClientRoutes:
    async def test_create_and_read_back_pinned_client(
        self,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            torrent, _usenet = await _seed_clients(session)
            created = await indexers_api.add_indexer(
                _torznab(download_client_id=torrent.id),
                object(),  # type: ignore[arg-type]
                session,
            )
            fetched = await indexers_api.get_indexer(
                created.id,
                object(),  # type: ignore[arg-type]
                session,
            )

        assert created.download_client_id == torrent.id
        assert fetched.download_client_id == torrent.id

    async def test_unpinned_by_default(
        self,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            created = await indexers_api.add_indexer(
                _torznab(),
                object(),  # type: ignore[arg-type]
                session,
            )

        assert created.download_client_id is None

    async def test_rejects_client_of_the_wrong_protocol(
        self,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            _torrent, usenet = await _seed_clients(session)
            with pytest.raises(ValidationError, match="torrent download client"):
                await indexers_api.add_indexer(
                    _torznab(download_client_id=usenet.id),
                    object(),  # type: ignore[arg-type]
                    session,
                )

    async def test_rejects_unknown_client(
        self,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            with pytest.raises(ValidationError, match="does not exist"):
                await indexers_api.add_indexer(
                    _torznab(download_client_id=4242),
                    object(),  # type: ignore[arg-type]
                    session,
                )

    async def test_update_sets_validates_and_clears_pin(
        self,
        sec_db: async_sessionmaker[AsyncSession],
    ) -> None:
        async with sec_db() as session:
            torrent, usenet = await _seed_clients(session)
            created = await indexers_api.add_indexer(
                _torznab(),
                object(),  # type: ignore[arg-type]
                session,
            )

            pinned = await indexers_api.update_indexer(
                created.id,
                IndexerUpdate(download_client_id=torrent.id),
                object(),  # type: ignore[arg-type]
                session,
            )
            assert pinned.download_client_id == torrent.id

            with pytest.raises(ValidationError, match="torrent download client"):
                await indexers_api.update_indexer(
                    created.id,
                    IndexerUpdate(download_client_id=usenet.id),
                    object(),  # type: ignore[arg-type]
                    session,
                )

            # An unrelated edit leaves the pin alone.
            renamed = await indexers_api.update_indexer(
                created.id,
                IndexerUpdate(name="Renamed Tracker"),
                object(),  # type: ignore[arg-type]
                session,
            )
            assert renamed.download_client_id == torrent.id

            cleared = await indexers_api.update_indexer(
                created.id,
                IndexerUpdate.model_validate({"download_client_id": None}),
                object(),  # type: ignore[arg-type]
                session,
            )

        assert cleared.download_client_id is None


@pytest.fixture
async def fk_db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """In-memory database with SQLite foreign keys enforced, as in production."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_deleting_the_client_unpins_the_indexer(
    fk_db: async_sessionmaker[AsyncSession],
) -> None:
    async with fk_db() as session:
        assert (await session.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
        torrent, _usenet = await _seed_clients(session)
        created = await indexers_api.add_indexer(
            _torznab(download_client_id=torrent.id),
            object(),  # type: ignore[arg-type]
            session,
        )
        await session.commit()

        await clients_api.delete_client(
            torrent.id,
            object(),  # type: ignore[arg-type]
            session,
        )
        await session.commit()
        session.expire_all()
        indexer = await session.get(IndexerConfig, created.id)

    assert indexer is not None
    assert indexer.download_client_id is None
