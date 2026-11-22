"""Series impact includes the whole catalog, but only exact arc members establish scope."""

from datetime import UTC, datetime

import pytest

from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
)
from pullbox.services.story_arc_series_review import outside_issues, related_series, series_peers
from pullbox.services.story_arc_service import StoryArcNotFoundError
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


async def test_actual_series_scope_and_cross_arc_impact(identity_probe_db: IdentityProbeDatabase):
    _, factory, _ = identity_probe_db
    async with factory.begin() as db:
        root = LibraryRoot(name="Comics", path="/nonexistent/review")
        series, guessed = [
            Series(title="Same name", sort_title="same", year_start=y, library_root=root)
            for y in (1990, 2010)
        ]
        books = [Issue(series=series, issue_number=n) for n in range(1, 23)]
        books[1].issue_number_text = "2-A"
        wrong = Issue(series=guessed, issue_number=1)
        arc = StoryArc(name="Review")
        peer = StoryArc(name="Archived crossover", lifecycle=StoryArcLifecycle.ARCHIVED)

        def link(parent, issue, seq, state=StoryArcResolutionState.RESOLVED):
            db.add(
                IssueStoryArc(
                    story_arc=parent,
                    issue=issue,
                    sequence_number=seq,
                    source_ordinal=seq,
                    resolution_state=state,
                )
            )

        db.add_all(books)
        link(arc, books[0], 1)
        link(arc, None, 2, StoryArcResolutionState.MISSING)
        link(arc, wrong, 3, StoryArcResolutionState.MISSING)
        link(peer, books[0], 1)
        link(peer, books[1], 2)
        link(peer, books[2], 3, StoryArcResolutionState.MISSING)
        await db.flush()
        for n, book in enumerate([books[0], books[1], books[1], wrong]):
            db.add(
                LibraryFile(
                    library_root=root,
                    issue_id=book.id,
                    file_name=f"{n}.cbz",
                    file_path=f"/nonexistent/review/{n}.cbz",
                    file_size=1,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(UTC),
                )
            )
        await db.flush()
        assert await related_series(db, arc.id, 0) == {
            "items": [
                dict(
                    id=series.id,
                    title="Same name",
                    year=1990,
                    members=1,
                    outside=21,
                    files=3,
                    other_arcs=1,
                )
            ],
            "next_after": None,
        }
        first = await outside_issues(db, arc.id, series.id, 0)
        assert [item["id"] for item in first["items"]] == [b.id for b in books[1:21]]
        assert first["items"][0]["number"] == "2-A"
        tail = await outside_issues(db, arc.id, series.id, first["next_after"])
        assert [item["id"] for item in tail["items"]] == [books[21].id] and not tail["next_after"]
        assert await series_peers(db, arc.id, series.id, 0) == {
            "items": [dict(id=peer.id, name=peer.name, lifecycle="archived", linked=3)],
            "next_after": None,
        }
        for read in (outside_issues, series_peers):
            with pytest.raises(StoryArcNotFoundError):
                await read(db, arc.id, guessed.id, 0)
        with pytest.raises(StoryArcNotFoundError):
            await related_series(db, arc.id + 1000, 0)
        assert not db.new and not db.dirty and not db.deleted


async def test_related_series_and_peer_cursors(identity_probe_db: IdentityProbeDatabase) -> None:
    _, factory, _ = identity_probe_db
    async with factory.begin() as db:
        root = LibraryRoot(name="Library", path="/review")
        arc = StoryArc(name="Crossover")
        parents = [
            Series(title=f"Series {i}", sort_title=str(i), library_root=root) for i in range(21)
        ]
        books = [Issue(series=parent, issue_number=1) for parent in parents]
        peers = [StoryArc(name=f"Peer {i}") for i in range(21)]
        for n, book in enumerate(books, start=1):
            db.add(
                IssueStoryArc(
                    story_arc=arc,
                    issue=book,
                    sequence_number=n,
                    source_ordinal=n,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
        for peer in peers:
            db.add(
                IssueStoryArc(
                    story_arc=peer,
                    issue=books[0],
                    sequence_number=1,
                    source_ordinal=1,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
        await db.flush()
        for read, args, expected in (
            (related_series, (arc.id,), parents),
            (series_peers, (arc.id, parents[0].id), peers),
        ):
            ids = sorted(record.id for record in expected)
            first = await read(db, *args, 0)
            assert [row["id"] for row in first["items"]] == ids[:20]
            tail = await read(db, *args, first["next_after"])
            assert [row["id"] for row in tail["items"]] == ids[20:] and tail["next_after"] is None
        assert await outside_issues(db, arc.id, parents[0].id, 0) == {
            "items": [],
            "next_after": None,
        }
