"""Story Arc reader ordering must behave identically on both supported databases."""

from datetime import UTC, datetime

from sqlalchemy import delete, select

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
)
from pullbox.services.reader_arc_order import list_reader_arcs, load_arc_reader_order
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


async def test_reader_keeps_gaps_and_revalidates_the_current_entry(
    identity_probe_db: IdentityProbeDatabase,
) -> None:
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        series = Series(title="Crossovers", sort_title="crossovers")
        arc = StoryArc(name="Archived crossover", lifecycle=StoryArcLifecycle.ARCHIVED)
        session.add_all([arc, series])
        await session.flush()
        issues = [
            Issue(series_id=series.id, issue_number=i, status=IssueStatus.WANTED) for i in (1, 2)
        ]
        session.add_all(issues)
        await session.flush()
        session.add_all(
            [
                IssueStoryArc(story_arc_id=arc.id, sequence_number=i, source_ordinal=i)
                for i in range(1, 51)
            ]
        )
        chapters = [
            IssueStoryArc(
                story_arc_id=arc.id,
                issue_id=issue.id,
                sequence_number=100,
                source_ordinal=100,
                resolution_state=StoryArcResolutionState.RESOLVED,
            )
            for issue in issues
        ]
        session.add_all(chapters)
        await session.flush()
        arc_id, issue_id, entry_id = arc.id, issues[0].id, chapters[0].id
    async with factory.begin() as session:
        order = await load_arc_reader_order(session, arc_id, issue_id, entry_id)
        assert await list_reader_arcs(session, issue_id) == [
            (arc_id, entry_id, "Archived crossover")
        ]
        assert order is not None and (order.position, order.total) == (51, 52)
        assert order.next and order.next.gap == "Crossovers #2 has no readable library file."
        assert order.previous and order.previous.gap
        chapter = await session.get(IssueStoryArc, entry_id)
        assert chapter
        chapter.sequence_number = 0
        root = LibraryRoot(name="reader", path="/reader")
        epub = Issue(series_id=series.id, issue_number=3, status=IssueStatus.OWNED)
        session.add_all([root, epub])
        await session.flush()
        gap = (
            await session.scalars(
                select(IssueStoryArc).where(
                    IssueStoryArc.story_arc_id == arc_id, IssueStoryArc.sequence_number == 1
                )
            )
        ).one()
        gap.issue_id = epub.id
        gap.resolution_state = StoryArcResolutionState.RESOLVED
        for target_id, fmt in [
            (issues[0].id, FileFormat.CBZ),
            (issues[1].id, FileFormat.CBZ),
            (epub.id, FileFormat.EPUB),
        ]:
            issue = await session.get(Issue, target_id)
            assert issue
            issue.status = IssueStatus.OWNED
            name = f"{target_id}.{fmt.value}"
            session.add(
                LibraryFile(
                    issue_id=target_id,
                    library_root_id=root.id,
                    file_path=f"/reader/{name}",
                    file_name=name,
                    file_size=1,
                    file_format=fmt,
                    file_modified_at=datetime.now(UTC),
                    match_confidence=MatchConfidence.MANUAL,
                )
            )
    async with factory.begin() as session:
        reordered = await load_arc_reader_order(session, arc_id, issue_id, entry_id)
        assert reordered is not None and reordered.position == 1 and reordered.previous is None
        assert reordered.next and "EPUB" in (reordered.next.gap or "")
        assert reordered.next_available and reordered.next_available.entry_id == chapters[1].id
        far_end = await load_arc_reader_order(session, arc_id, issues[1].id, chapters[1].id)
        assert far_end and far_end.position == 52 and far_end.previous_available
        assert far_end.previous_available.entry_id == entry_id and far_end.next_available is None
        await session.execute(delete(IssueStoryArc).where(IssueStoryArc.id == entry_id))
    async with factory.begin() as session:
        assert await load_arc_reader_order(session, arc_id, issue_id, entry_id) is None
        assert await list_reader_arcs(session, issue_id) == []
