"""Private story arc resume targets; page progress belongs to the issue."""

from dataclasses import dataclass

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models.reader import StoryArcReaderBookmark
from pullbox.models.story_arc import IssueStoryArc, StoryArcResolutionState
from pullbox.services.reading_query_service import load_reader_issue_access


@dataclass(frozen=True, slots=True)
class ArcReadAction:
    resume: bool
    issue_id: int | None = None
    entry_id: int | None = None
    notice: str = ""


async def save_arc_bookmark(
    session: AsyncSession, *, user_id: int, arc_id: int, entry_id: int, issue_id: int
) -> bool:
    """Validate membership at write time, in the settled-page transaction."""
    member = IssueStoryArc
    bookmark = StoryArcReaderBookmark
    values = select(literal(user_id), member.story_arc_id, member.id, member.issue_id).where(
        member.id == entry_id,
        member.story_arc_id == arc_id,
        member.issue_id == issue_id,
        member.resolution_state == StoryArcResolutionState.RESOLVED,
    )
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    statement = insert(bookmark).from_select(
        ["user_id", "story_arc_id", "entry_id", "issue_id"], values
    )
    result = await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[bookmark.user_id, bookmark.story_arc_id],
            set_={
                "entry_id": statement.excluded.entry_id,
                "issue_id": statement.excluded.issue_id,
                "updated_at": func.now(),
            },
        ).returning(bookmark.entry_id)
    )
    return result is not None


async def load_arc_read_action(
    session: AsyncSession, *, user_id: int, arc_id: int
) -> ArcReadAction:
    member = IssueStoryArc
    bookmark = (
        await session.execute(
            select(StoryArcReaderBookmark.entry_id, StoryArcReaderBookmark.issue_id).where(
                StoryArcReaderBookmark.user_id == user_id,
                StoryArcReaderBookmark.story_arc_id == arc_id,
            )
        )
    ).one_or_none()
    query = select(member.id, member.issue_id, member.resolution_state).where(
        member.story_arc_id == arc_id
    )
    if bookmark is None:
        query = query.order_by(member.sequence_number, member.source_ordinal, member.id)
    else:
        query = query.where(member.id == bookmark.entry_id, member.issue_id == bookmark.issue_id)
    entry = (await session.execute(query.limit(1))).one_or_none()
    resume = bookmark is not None
    if entry is None and not resume:
        return ArcReadAction(False)
    if (
        entry is not None
        and entry.issue_id
        and entry.resolution_state == StoryArcResolutionState.RESOLVED
    ):
        access = await load_reader_issue_access(session, issue_id=entry.issue_id)
        if access and access.readable:
            return ArcReadAction(resume, entry.issue_id, entry.id)
    notice = (
        "Your bookmarked chapter is unavailable." if resume else "The first chapter is unavailable."
    )
    return ArcReadAction(resume, notice=f"{notice} Choose another issue in the reading order.")
