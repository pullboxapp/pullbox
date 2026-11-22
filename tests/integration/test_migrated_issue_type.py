"""The migrated issue type is readable, writable and filterable without losing links."""

import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from pullbox.models.issue import Issue, IssueStatus, IssueType
from tests.integration import test_database_migration_target as targets
from tests.integration import test_postgres_transfer_stage as stages

server = targets.server


async def exercise(url):
    targets._migrate(url, "upgrade", "o2c3456789ab")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for statement in (
                "INSERT INTO series(id,title,sort_title,status,issue_count,monitored) "
                "VALUES(71,'Fixture','fixture','ENDED',3,true)",
                "INSERT INTO issues(id,series_id,issue_number,status,issue_type) VALUES "
                "(83,71,3,'OWNED','issue'),(84,71,4,'WANTED','one_shot'),(85,71,5,'OWNED','TPB')",
                "INSERT INTO story_arcs(id,name) VALUES(55,'Arc')",
                "INSERT INTO issue_story_arcs(story_arc_id,issue_id,sequence_number,source_ordinal)"
                " VALUES(55,83,1,0)",
            ):
                await conn.execute(text(statement))
        targets._migrate(url, "upgrade", "head")
        async with AsyncSession(engine) as session:
            assert list(await session.scalars(select(Issue.issue_type).order_by(Issue.id))) == [
                IssueType.ISSUE,
                IssueType.ONE_SHOT,
                IssueType.TPB,
            ]
            assert (
                await session.scalar(select(Issue.id).where(Issue.issue_type == IssueType.ONE_SHOT))
                == 84
            )
            assert await session.scalar(text("SELECT issue_id FROM issue_story_arcs")) == 83
            session.add(
                Issue(
                    series_id=71,
                    issue_number=6,
                    status=IssueStatus.OWNED,
                    issue_type=IssueType.ANNUAL,
                )
            )
            await session.execute(
                text("INSERT INTO issues(series_id,issue_number,status) VALUES(71,7,'WANTED')")
            )
            await session.commit()
            assert (
                await session.scalars(
                    select(Issue.issue_type)
                    .where(Issue.issue_number >= 6)
                    .order_by(Issue.issue_number)
                )
            ).all() == [IssueType.ANNUAL, IssueType.ISSUE]
    finally:
        await engine.dispose()


async def test_existing_and_new_postgres_issues(server):
    _, url, _ = stages.target(server)
    await exercise(url)


async def test_existing_and_new_sqlite_issues(tmp_path):
    db = tmp_path / "issues.db"
    await exercise(f"sqlite+aiosqlite:///{db}")
    with closing(sqlite3.connect(db)) as conn:
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    ("custom", "reason"),
    [
        (
            "UPDATE issues SET issue_type='unrecognized' WHERE id=84",
            "Unrecognized issue classification",
        ),
        (
            "CREATE TRIGGER user_issue_trigger AFTER UPDATE ON issues BEGIN SELECT 1; END",
            "custom SQLite issue schema",
        ),
        ("CREATE INDEX user_issue_expr ON issues(lower(issue_type))", "custom SQLite issue schema"),
    ],
)
def test_unsafe_rebuild_preserves_previous_rows_and_schema(tmp_path, custom, reason):
    from pullbox.services.database_migration_preflight import inspect_migration_backup

    db = tmp_path / "old.db"
    url = f"sqlite+aiosqlite:///{db}"
    targets._migrate(url, "upgrade", "o2c3456789ab")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO series(id,title,sort_title,status,issue_count,monitored) "
            "VALUES(71,'Fixture','fixture','ENDED',2,true)"
        )
        conn.execute(
            "INSERT INTO issues(id,series_id,issue_number,status) VALUES "
            "(83,71,3,'OWNED'),(84,71,4,'OWNED')"
        )
        conn.execute(custom)
    if "unrecognized" in custom:
        report = inspect_migration_backup(db, configured_url="sqlite:///:memory:")
        assert "issue_classification" in report.blockers
    with closing(sqlite3.connect(db)) as conn:
        schema = list(conn.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"))
        rows = list(conn.execute("SELECT * FROM issues ORDER BY id"))
    targets._migrate(url, "upgrade", "head", expected_error=reason)
    with closing(sqlite3.connect(db)) as conn:
        assert (
            list(conn.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"))
            == schema
        )
        assert list(conn.execute("SELECT * FROM issues ORDER BY id")) == rows
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "o2c3456789ab",
        )
