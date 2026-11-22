"""Exercise the private bookmark migration and resume behavior on both databases."""

from datetime import UTC, datetime

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.engine import Connection

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.reader import StoryArcReaderBookmark
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState
from pullbox.models.user import User
from pullbox.services.reader_arc_bookmarks import load_arc_read_action, save_arc_bookmark
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


def _migrate(conn: Connection, *, up: bool) -> None:
    from pathlib import Path
    from runpy import run_path

    path = (
        Path(__file__).resolve().parents[3]
        / "alembic/versions/j7d8e9f01234_add_story_arc_reader_bookmarks.py"
    )
    with Operations.context(MigrationContext.configure(conn)):
        run_path(str(path))["upgrade" if up else "downgrade"]()


async def test_migrated_bookmarks_are_private_and_do_not_follow_replaced_entries(
    identity_probe_db: IdentityProbeDatabase,
) -> None:
    engine, factory, _ = identity_probe_db
    table = StoryArcReaderBookmark.__table__
    async with engine.begin() as conn:
        await conn.run_sync(table.drop)
        await conn.run_sync(_migrate, up=True)
        await conn.run_sync(_migrate, up=False)
        await conn.run_sync(_migrate, up=True)
        pk = await conn.run_sync(lambda c: inspect(c).get_pk_constraint(table.name))
        assert pk["constrained_columns"] == ["id"]
        constraints = await conn.run_sync(lambda c: inspect(c).get_unique_constraints(table.name))
        assert any(c["column_names"] == ["user_id", "story_arc_id"] for c in constraints)
    async with factory.begin() as session:
        users = [User(username=n, password_hash="unused") for n in ("reader-one", "reader-two")]
        arcs = [StoryArc(name=n) for n in ("Crossover A", "Crossover B")]
        series = Series(title="Example", sort_title="example")
        root = LibraryRoot(name="reader", path="/reader")
        session.add_all([series, root, *users, *arcs])
        await session.flush()
        issues = [
            Issue(series_id=series.id, issue_number=i, status=IssueStatus.OWNED) for i in (1, 2)
        ]
        session.add_all(issues)
        await session.flush()
        for issue in issues:
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
        entries = [
            [
                IssueStoryArc(
                    story_arc_id=arc.id,
                    issue_id=issue.id,
                    sequence_number=i,
                    source_ordinal=i,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
                for i, issue in enumerate(issues, 1)
            ]
            for arc in arcs
        ]
        session.add_all(entries[0] + entries[1])
        await session.flush()
        u, v = (user.id for user in users)
        a, b = (arc.id for arc in arcs)
        i, j = (issue.id for issue in issues)
        first, second = entries[0][0].id, entries[0][1].id
        other = entries[1][0].id
        assert (await load_arc_read_action(session, user_id=u, arc_id=a)).entry_id == first
        assert not await session.scalar(select(func.count()).select_from(table))
        for arc_id, entry_id, issue_id in ((a, first, i), (a, second, j), (b, other, i)):
            assert await save_arc_bookmark(
                session, user_id=u, arc_id=arc_id, entry_id=entry_id, issue_id=issue_id
            )
        assert not await save_arc_bookmark(
            session, user_id=u, arc_id=b, entry_id=second, issue_id=j
        )
    async with factory.begin() as session:
        own = await load_arc_read_action(session, user_id=u, arc_id=a)
        assert own.resume and own.entry_id == second
        assert (await load_arc_read_action(session, user_id=u, arc_id=b)).entry_id == other
        fresh = await load_arc_read_action(session, user_id=v, arc_id=a)
        assert not fresh.resume and fresh.entry_id == first
        assert await session.scalar(select(func.count()).select_from(table)) == 2
        chapter = await session.get(IssueStoryArc, second)
        assert chapter is not None
        chapter.sequence_number = 0
        await session.flush()
        assert (await load_arc_read_action(session, user_id=u, arc_id=a)).entry_id == second
        old_first = await session.get(IssueStoryArc, first)
        assert old_first is not None
        old_first.issue_id = None
        await session.flush()
        chapter.issue_id = (
            i  # This replacement has a readable file but is not the bookmarked comic.
        )
        await session.flush()
        changed = await load_arc_read_action(session, user_id=u, arc_id=a)
        assert changed.resume and changed.issue_id is None and "unavailable" in changed.notice
        await session.execute(delete(IssueStoryArc).where(IssueStoryArc.id == second))
    async with factory.begin() as session:
        lost = await load_arc_read_action(session, user_id=u, arc_id=a)
        assert lost.resume and lost.issue_id is None and "unavailable" in lost.notice
        assert (await load_arc_read_action(session, user_id=u, arc_id=b)).issue_id == i
    with pytest.raises(RuntimeError, match="private story arc bookmarks"):
        async with engine.begin() as conn:
            await conn.run_sync(_migrate, up=False)
    async with factory.begin() as session:
        await session.execute(delete(StoryArc).where(StoryArc.id == a))
        assert await session.scalar(select(func.count()).select_from(table)) == 1
        await session.execute(delete(User).where(User.id == u))
        assert await session.scalar(select(func.count()).select_from(table)) == 0
