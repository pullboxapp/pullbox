"""An entire parent catalog is removed only after an exact, still-current review."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from pullbox.models.import_job import ImportedSeries, ImportJob, ImportSourceType
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState
from pullbox.models.user import User
from pullbox.services.story_arc_cleanup import enqueue_reviewed_cleanup, preview_cleanup
from pullbox.services.story_arc_service import StoryArcConflictError
from pullbox.utilities.executors.story_arc_cleanup import StoryArcCleanupExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobState, UtilityJob
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


@pytest.mark.parametrize(
    "case",
    [
        "confirmed",
        "cancel",
        "outside_changed",
        "peer_changed",
        "peer_journal",
        "new_import",
        "both",
        "injected_failure",
    ],
)
async def test_reviewed_series_record_disposition(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, _ = identity_probe_db
    mgr = JobQueueManager(factory)
    mgr.register_executor("story_arc_cleanup", StoryArcCleanupExecutor)
    path = tmp_path / "chapter.cbz"
    path.write_bytes(b"original bytes")
    async with factory.begin() as db:
        keep, remove = [Series(title=n, sort_title=n) for n in ("Keep", "Remove")]
        outside = Issue(series=remove, issue_number=2)
        arc, peer = [StoryArc(name=n) for n in ("Crossover", "Survives")]
        actor = User(username="tester", password_hash="unused")
        db.add(actor)
        for order, chapter, pos in (
            (arc, Issue(series=keep, issue_number=1), 1),
            (arc, Issue(series=remove, issue_number=1), 2),
            (peer, outside, 1),
        ):
            db.add(
                IssueStoryArc(
                    story_arc=order,
                    issue=chapter,
                    sequence_number=pos,
                    source_ordinal=pos,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
        book = LibraryFile(
            issue=outside,
            file_path=str(path),
            file_name=path.name,
            library_root=LibraryRoot(name="test", path=str(tmp_path)),
            file_size=path.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
        )
        db.add(book)
        await db.flush()
        aid, pid, uid, sid, kid, fid = arc.id, peer.id, actor.id, remove.id, keep.id, book.id
    async with factory.begin() as db:
        plan = await preview_cleanup(db, aid, uid, [], series_ids=[sid])
        assert plan["record_series"] == [
            {"id": sid, "title": "Remove", "issues": 2, "outside": 1, "files": 1}
        ]
        assert plan["shared_arcs"] == [{"id": pid, "name": "Survives", "linked": 1}]
        with pytest.raises(StoryArcConflictError, match="confirmed member"):
            await preview_cleanup(db, aid, uid, [], series_ids=[kid + sid + 100])
        with pytest.raises(StoryArcConflictError, match="separately"):
            await preview_cleanup(db, aid, uid, [], member_ids=[fid], series_ids=[sid])
        with pytest.raises(StoryArcConflictError, match="shared"):
            await enqueue_reviewed_cleanup(
                db, mgr, aid, uid, "tester", [], plan["token"], series_ids=[sid]
            )
        with pytest.raises(StoryArcConflictError, match="changed"):
            await enqueue_reviewed_cleanup(
                db, mgr, aid, uid, "tester", [], plan["token"], series_ids=[kid]
            )
        selected = [kid, sid] if case in {"both", "injected_failure"} else [sid]
        if len(selected) > 1:
            plan = await preview_cleanup(db, aid, uid, [], series_ids=selected)
        job = await enqueue_reviewed_cleanup(
            db, mgr, aid, uid, "tester", [], plan["token"], series_ids=selected, allow_shared=True
        )
        jid = job.id
    async with factory.begin() as db:
        if case == "cancel":
            await mgr.cancel_job(db, jid)
        elif case == "outside_changed":
            db.add(Issue(series_id=sid, issue_number=3))
        elif case == "peer_changed":
            other = await db.get(StoryArc, pid)
            other.revision += 1
        elif case == "peer_journal":
            other = await db.get(StoryArc, pid)
            other.diagnostics = {"pending_member_cleanup": {"operation": "retained"}}
        elif case == "new_import":
            source = ImportJob(source_path="/fixture", source_type=ImportSourceType.MYLAR3)
            db.add(source)
            await db.flush()
            db.add(ImportedSeries(import_job_id=source.id, series_id=sid, raw_series_name="Remove"))
    if case == "injected_failure":
        from pullbox.services import story_arc_series_cleanup as cleanup

        detach = cleanup.detach_series_arc_entries

        async def fail_on_second(db, parent):
            await detach(db, parent)
            if parent.id == max(selected):
                raise StoryArcConflictError("Lost authority after staging records")

        monkeypatch.setattr(cleanup, "detach_series_arc_entries", fail_on_second)
    await mgr.dispatch_next()
    async with factory() as db:
        job = await db.get(UtilityJob, jid)
        assert (await db.get(Series, kid) is None) == (case == "both")
        assert await db.get(StoryArc, pid)
        entry = await db.scalar(select(IssueStoryArc).where(IssueStoryArc.story_arc_id == pid))
        if case in {"confirmed", "both"}:
            assert job.state == JobState.COMPLETED, job.error_message
            assert await db.get(Series, sid) is None and await db.get(StoryArc, aid) is None
            assert (
                entry.issue_id is None and entry.resolution_state == StoryArcResolutionState.MISSING
            )
            assert (await db.get(LibraryFile, fid)).issue_id is None
        else:
            assert job.state == (JobState.CANCELLED if case == "cancel" else JobState.FAILED)
            assert await db.get(Series, sid) and await db.get(StoryArc, aid)
            assert entry.issue_id is not None
    assert path.read_bytes() == b"original bytes"
