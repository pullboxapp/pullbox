"""Reader series resume must survive upgrades without inheriting another user's place."""

from datetime import UTC, datetime
from pathlib import Path
from runpy import run_path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Connection

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.reader import SeriesReaderBookmark
from pullbox.models.series import Series
from pullbox.models.user import User
from pullbox.services.reader_state_service import save_series_bookmark
from pullbox.services.reading_query_service import SeriesReadAction, load_series_read_action
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


def _migrate(conn: Connection, *, up: bool) -> None:
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic/versions/k8e9f0123456_add_series_reader_bookmarks.py"
    )
    with Operations.context(MigrationContext.configure(conn)):
        run_path(str(path))["upgrade" if up else "downgrade"]()


async def test_series_resume_is_private_and_does_not_substitute_for_a_lost_issue(
    identity_probe_db: IdentityProbeDatabase,
) -> None:
    engine, factory, _ = identity_probe_db
    table = SeriesReaderBookmark.__table__
    async with engine.begin() as conn:
        await conn.run_sync(table.drop)
        await conn.run_sync(_migrate, up=True)
        await conn.run_sync(_migrate, up=False)
        await conn.run_sync(_migrate, up=True)
    async with factory.begin() as session:
        root = LibraryRoot(name="reader", path="/reader")
        one, two = [User(username=name, password_hash="unused") for name in ("one", "two")]
        home, other = [Series(title=name, sort_title=name) for name in ("series", "other")]
        session.add_all([one, two, home, other, root])
        await session.flush()
        last, first, missing = [
            Issue(series_id=home.id, issue_number=n, status=IssueStatus.OWNED)
            for n in (2, -0.5, -1)
        ]
        session.add_all([last, first, missing])
        await session.flush()
        for issue in (first, last):
            name = f"{issue.id}.cbz"
            session.add(
                LibraryFile(
                    issue_id=issue.id,
                    library_root_id=root.id,
                    file_path=f"/reader/{name}",
                    file_name=name,
                    file_size=1,
                    file_modified_at=datetime.now(UTC),
                    file_format=FileFormat.CBZ,
                    match_confidence=MatchConfidence.MANUAL,
                )
            )
        await session.flush()
        u, v, a, b, i, j = one.id, two.id, home.id, other.id, first.id, last.id
        assert await load_series_read_action(session, user_id=u, series_id=a) == SeriesReadAction(
            False, i
        )
        assert await load_series_read_action(session, user_id=u, series_id=b) == SeriesReadAction(
            False, None
        )
        assert not await session.scalar(select(func.count()).select_from(table))
        for target in (i, j):
            assert await save_series_bookmark(session, user_id=u, series_id=a, issue_id=target)
        assert not await save_series_bookmark(session, user_id=u, series_id=b, issue_id=j)
    async with factory.begin() as session:
        assert await load_series_read_action(session, user_id=u, series_id=a) == SeriesReadAction(
            True, j
        )
        assert await load_series_read_action(session, user_id=v, series_id=a) == SeriesReadAction(
            False, i
        )
        assert await session.scalar(select(func.count()).select_from(table)) == 1
        moved = await session.get(Issue, j)
        assert moved
        moved.series_id = b
        await session.flush()
        assert await load_series_read_action(session, user_id=u, series_id=a) == SeriesReadAction(
            True, None
        )
        assert await load_series_read_action(session, user_id=u, series_id=b) == SeriesReadAction(
            False, j
        )
        await session.execute(delete(Issue).where(Issue.id == j))
    async with factory.begin() as session:
        assert await load_series_read_action(session, user_id=u, series_id=a) == SeriesReadAction(
            True, None
        )
    with pytest.raises(RuntimeError, match="private series bookmarks"):
        async with engine.begin() as conn:
            await conn.run_sync(_migrate, up=False)
    async with factory.begin() as session:
        assert await save_series_bookmark(session, user_id=v, series_id=a, issue_id=i)
        await session.execute(delete(User).where(User.id == u))
        assert await session.scalar(select(func.count()).select_from(table)) == 1
        await session.execute(delete(Series).where(Series.id == a))
        assert not await session.scalar(select(func.count()).select_from(table))
