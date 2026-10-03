"""Watch uniqueness, retained intent and migration parity on both disposable engines."""

import asyncio
import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, Table, func, select

from pullbox.models import LibraryRoot, Series, SeriesInterest, SeriesInterestState, User
from pullbox.models.whats_new import WhatsNewCacheKind, WhatsNewReleaseCache
from pullbox.schemas.whats_new import WhatsNewWatchRequest
from pullbox.services.series_interest import cancel_watch, save_watch
from pullbox.services.whats_new_actions import load_release_selection
from tests.ui.test_whats_new_ui_routes import _issue_summary


async def prepare(factory, tmp_path):
    tmp_path.mkdir(exist_ok=True)
    async with factory.begin() as session:
        root = LibraryRoot(
            name="Watch",
            path=str(tmp_path),
            allow_managed_writes=True,
            is_default_managed_destination=True,
        )
        user = User(username="watch-operator", password_hash="unused")
        session.add_all([root, user])
        release = _issue_summary()
        release["store_date"] = "2099-01-01"
        caches = [
            WhatsNewReleaseCache(
                cache_key=f"watch:{number}",
                cache_kind=WhatsNewCacheKind.UPCOMING,
                payload={"weeks": [{"store_date": "2099-01-01", "issues": [release]}]},
                fetched_at=datetime.now(UTC),
                last_successful_refresh_at=datetime.now(UTC),
            )
            for number in range(2)
        ]
        session.add_all(caches)
        await session.flush()
        contexts = [await load_release_selection(session, row.id, 1514020) for row in caches]
        return root.id, user.id, contexts


async def test_parallel_different_cache_requests_create_one_intent(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    root, user, contexts = await prepare(factory, tmp_path / "root")

    async def save(context):
        async with factory.begin() as session:
            interest = await save_watch(
                session,
                WhatsNewWatchRequest(selection=context.selection, library_root_id=root),
                user,
            )
            return interest.id

    ids = await asyncio.gather(*(save(context) for context in contexts))
    assert ids[0] == ids[1]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(SeriesInterest)) == 1
        assert await session.scalar(select(func.count()).select_from(Series)) == 0
        assert (await session.scalar(select(SeriesInterest))).next_known_release_date == date(
            2099, 1, 1
        )


async def test_root_deletion_preserves_watch_and_it_can_still_be_cancelled(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    root, user, contexts = await prepare(factory, tmp_path / "root")
    async with factory.begin() as session:
        interest = await save_watch(
            session, WhatsNewWatchRequest(selection=contexts[0].selection), user
        )
        interest_id = interest.id
    async with factory.begin() as session:
        await session.delete(await session.get(LibraryRoot, root))
        for context in contexts:
            await session.delete(
                await session.get(WhatsNewReleaseCache, context.selection.cache_id)
            )
    async with factory.begin() as session:
        interest = await session.get(SeriesInterest, interest_id)
        assert interest.target_library_root_id is None
        assert interest.state is SeriesInterestState.WATCHING
        cancelled = await cancel_watch(session, interest_id, user)
        assert cancelled.state is SeriesInterestState.CANCELLED


def revision(connection):
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic/versions/i6c7d8e9f012_add_series_interests.py"
    )
    spec = importlib.util.spec_from_file_location("watch_revision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


async def test_watch_migration_matches_model_and_retains_decisions(identity_probe_db, tmp_path):
    engine, factory, _ = identity_probe_db
    metadata = MetaData()
    for parent in ("library_roots", "series", "users"):
        Table(parent, metadata, Column("id", Integer, primary_key=True))
    SeriesInterest.__table__.to_metadata(metadata)
    async with engine.begin() as connection:
        await connection.run_sync(SeriesInterest.__table__.drop)
        await connection.run_sync(lambda conn: revision(conn).upgrade())
        differences = await connection.run_sync(
            lambda conn: compare_metadata(
                MigrationContext.configure(
                    conn,
                    opts={
                        "include_object": lambda obj, name, kind, reflected, other: (
                            name == "series_interests"
                            if kind == "table"
                            else getattr(obj, "table", None) is not None
                            and obj.table.name == "series_interests"
                        )
                    },
                ),
                metadata,
            )
        )
        assert differences == []
        await connection.run_sync(lambda conn: revision(conn).downgrade())
        await connection.run_sync(lambda conn: revision(conn).upgrade())
    _, user, contexts = await prepare(factory, tmp_path / "root")
    async with factory.begin() as session:
        await save_watch(session, WhatsNewWatchRequest(selection=contexts[0].selection), user)
    with pytest.raises(RuntimeError, match="retained Watch decisions"):
        async with engine.begin() as connection:
            await connection.run_sync(lambda conn: revision(conn).downgrade())
