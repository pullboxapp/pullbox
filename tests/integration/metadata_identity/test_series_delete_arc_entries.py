"""Deleting a series preserves accurate, reviewable entries in surviving reading orders."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from pullbox.core.exceptions import ValidationError
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcResolutionState,
)
from pullbox.models.story_arc_sync import StoryArcSyncWork
from pullbox.services.series_service import SeriesService
from pullbox.services.story_arc_series_detachment import reserve_series_arc_work
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


@pytest.mark.parametrize(
    "case", ["delete", "rollback", "placement", "sync", "crosslink", "bulk", "changed"]
)
async def test_series_delete_retains_surviving_reading_order(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    _, factory, _ = identity_probe_db
    client_cancel = AsyncMock()
    monkeypatch.setattr("pullbox.services.series_service._cancel_download_on_client", client_cancel)
    monkeypatch.setattr("pullbox.services.series_service.purge_series_cover_cache", AsyncMock())
    root_path = tmp_path / "library"
    root_path.mkdir()
    folder = root_path / "Example"
    folder.mkdir()
    path = folder / "50-X.cbz"
    path.write_bytes(b"original")
    copy = tmp_path / "separate arc copy.cbz"
    copy.write_bytes(b"copy")
    async with factory.begin() as db:
        root = LibraryRoot(name="Temp", path=str(root_path))
        series = Series(title="Example", sort_title="example", path=str(folder), library_root=root)
        other = Series(title="Other", sort_title="other", library_root=root)
        book = Issue(series=series, issue_number=50, issue_number_text="50-X", title="Part one")
        skipped = Issue(series=series, issue_number=51)
        kept = Issue(series=other, issue_number=1)
        arc = StoryArc(name="Reading order", revision=3)
        peer = StoryArc(name="Archived", lifecycle=StoryArcLifecycle.ARCHIVED, revision=5)
        untouched = StoryArc(name="Unrelated", revision=9)

        def member(a, issue, pos, **extra):
            return IssueStoryArc(
                story_arc=a,
                issue=issue,
                sequence_number=pos,
                source_ordinal=pos,
                sync_eligible=True,
                resolution_state=extra.pop("state", StoryArcResolutionState.RESOLVED),
                **extra,
            )

        m = member(arc, book, 1)
        skip = member(arc, skipped, 2, state=StoryArcResolutionState.SKIPPED)
        third = member(arc, kept, 3)
        p = member(
            peer,
            book,
            4,
            source_series_name="Provider title",
            source_issue_number_text="50-x",
            source_issue_title="Provider subtitle",
        )
        db.add_all([m, skip, third, p, member(untouched, kept, 1)])
        await db.flush()
        f = LibraryFile(
            library_root=root,
            issue_id=book.id,
            file_path=str(path),
            file_name=path.name,
            file_format=FileFormat.CBZ,
            file_size=path.stat().st_size,
            file_modified_at=datetime.now(UTC),
        )
        db.add(f)
        await db.flush()
        if case in {"placement", "sync", "crosslink", "bulk"}:
            db.add(
                DownloadHistory(
                    issue_id=book.id,
                    title="Test",
                    download_url="https://example.invalid",
                    external_id="test",
                    download_client=DownloadClientType.SABNZBD,
                    state=DownloadState.DOWNLOADING,
                )
            )
            if case != "sync":
                db.add(
                    StoryArcPlacement(
                        issue_story_arc_id=third.id if case == "crosslink" else m.id,
                        library_file_id=f.id,
                        placement_path=str(copy),
                        mode=StoryArcPlacementMode.COPY,
                        ownership=StoryArcPlacementOwnership.MANAGED,
                    )
                )
            else:
                db.add(
                    StoryArcSyncWork(
                        issue_story_arc_id=m.id,
                        library_file_id=f.id,
                        desired_generation="test",
                        source_signature_hash="test",
                        source_file_path=str(path),
                        source_file_size=f.file_size,
                        source_file_modified_at=f.file_modified_at,
                        story_arc_revision=3,
                        membership_sequence=1,
                        policy_schema_version=1,
                    )
                )
        sid, iid = series.id, book.id
        independent_folder = root_path / "Independent"
        first_id = other.id
        if case == "bulk":
            independent_folder.mkdir()
            (independent_folder / "untouched.cbz").write_bytes(b"independent")
            other.path = str(independent_folder)
        ids = (m.id, skip.id, third.id, p.id)
        arcs = (arc.id, peer.id, untouched.id)
    blocked = case in {"placement", "sync", "crosslink", "bulk", "changed"}
    if case == "changed":
        resolve = SeriesService.build_delete_target

        async def newly_protected(db, series):
            target = await resolve(db, series)
            db.add(
                StoryArcPlacement(
                    issue_story_arc_id=ids[0],
                    placement_path=str(copy),
                    mode=StoryArcPlacementMode.COPY,
                    ownership=StoryArcPlacementOwnership.MANAGED,
                )
            )
            await db.flush()
            return target

        monkeypatch.setattr(SeriesService, "build_delete_target", staticmethod(newly_protected))
    async with factory() as db:
        if blocked:
            with pytest.raises(ValidationError, match="Reading order"):
                if case == "bulk":
                    from pullbox.api.v1.series import bulk_delete_series
                    from pullbox.schemas.series import SeriesBulkDelete

                    await bulk_delete_series(
                        SeriesBulkDelete(series_ids=[first_id, sid], delete_folder=True), None, db
                    )
                else:
                    await SeriesService.delete(db, sid, delete_files=True, delete_folder=True)
            await db.rollback()
        else:
            await SeriesService.delete(db, sid)
            if case == "rollback":
                await db.rollback()
            else:
                await db.commit()
    async with factory() as db:
        m, skip, third, p = [await db.get(IssueStoryArc, i) for i in ids]
        revisions = [(await db.get(StoryArc, i)).revision for i in arcs]
        assert [e.sequence_number for e in (m, skip, third, p)] == [1, 2, 3, 4]
        assert third.issue_id and third.sync_eligible
        if case == "delete":
            assert m.issue_id is None and m.resolution_state == StoryArcResolutionState.MISSING
            assert m.source_issue_number_text == "50-X" and m.source_series_name == "Example"
            assert m.source_issue_title == "Part one" and not m.sync_eligible
            assert (
                skip.issue_id is None and skip.resolution_state == StoryArcResolutionState.SKIPPED
            )
            assert not skip.sync_eligible
            assert (p.source_series_name, p.source_issue_number_text, p.source_issue_title) == (
                "Provider title",
                "50-x",
                "Provider subtitle",
            )
            assert revisions == [4, 6, 9] and not await db.get(Series, sid)
        else:
            assert await db.get(Series, sid)
            assert m.issue_id == iid and m.resolution_state == StoryArcResolutionState.RESOLVED
            assert revisions == [3, 5, 9]
    assert path.read_bytes() == b"original" and copy.read_bytes() == b"copy"
    client_cancel.assert_not_awaited()
    if case == "bulk":
        assert (independent_folder / "untouched.cbz").read_bytes() == b"independent"


async def test_series_deletion_reservation_excludes_new_arc_links_until_rollback(
    identity_probe_db: IdentityProbeDatabase,
) -> None:
    engine, factory, _ = identity_probe_db
    async with factory.begin() as db:
        series = Series(title="Reserved", sort_title="reserved")
        issue = Issue(series=series, issue_number=1)
        arc = StoryArc(name="Other order")
        db.add_all([issue, arc])
        await db.flush()
        sid, iid, aid = series.id, issue.id, arc.id

    def entry() -> IssueStoryArc:
        return IssueStoryArc(story_arc_id=aid, issue_id=iid, sequence_number=1, source_ordinal=1)

    async with factory() as deleting, factory() as concurrent:
        await reserve_series_arc_work(deleting, [sid])
        if engine.dialect.name == "postgresql":
            await concurrent.execute(text("SET LOCAL lock_timeout = '200ms'"))
        else:
            await concurrent.execute(text("PRAGMA busy_timeout=200"))
        concurrent.add(entry())
        with pytest.raises(DBAPIError, match=r"(?i)lock"):
            await concurrent.flush()
        await concurrent.rollback()
        await deleting.rollback()
    async with factory.begin() as db:
        db.add(entry())
