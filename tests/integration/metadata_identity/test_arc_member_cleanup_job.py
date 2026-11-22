"""Only explicitly selected original comics may enter background arc cleanup."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState
from pullbox.models.user import User
from pullbox.services import story_arc_member_cleanup as owner
from pullbox.services.story_arc_cleanup import enqueue_reviewed_cleanup, preview_cleanup
from pullbox.services.story_arc_deletion import delete_reviewed_arc, preview_arc_deletion
from pullbox.services.story_arc_service import StoryArcConflictError
from pullbox.utilities.executors.story_arc_cleanup import StoryArcCleanupExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobState, UtilityJob, UtilityJobItem
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


@pytest.mark.parametrize(
    "case", ["trash", "both", "one", "cancel", "changed", "new_peer", "finish_receipt"]
)
async def test_job_removes_only_signed_member_selection(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, _ = identity_probe_db
    mgr = JobQueueManager(factory)
    mgr.register_executor("story_arc_cleanup", StoryArcCleanupExecutor)
    paths = [tmp_path / name for name in ("first.cbz", "alternate.cbz", "unrelated.cbz")]
    async with factory.begin() as db:
        root = LibraryRoot(name="Test", path=str(tmp_path))
        series = Series(title="Comic", sort_title="comic", library_root=root)
        issue, unrelated = [
            Issue(series=series, issue_number=n, status=IssueStatus.OWNED) for n in (1, 2)
        ]
        arc, peer = [StoryArc(name=n) for n in ("Remove", "Surviving arc")]
        actor = User(username="tester", password_hash="unused")
        db.add_all([actor, unrelated])
        for reading_list in [arc, peer]:
            db.add(
                IssueStoryArc(
                    story_arc=reading_list,
                    issue=issue,
                    sequence_number=1,
                    source_ordinal=1,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
        await db.flush()
        files = []
        for i, path in enumerate(paths):
            path.write_bytes(path.name.encode())
            book = LibraryFile(
                file_path=str(path),
                file_name=path.name,
                library_root=root,
                file_size=path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                issue_id=(issue.id if i < 2 else unrelated.id),
            )
            files.append(book)
            db.add(book)
        if case == "trash":
            (tmp_path / "Trash").mkdir()
            db.add(
                SystemConfig(
                    key="utility_trash_folder", value_type="string", value=str(tmp_path / "Trash")
                )
            )
        await db.flush()
        aid, pid, uid, iid = arc.id, peer.id, actor.id, issue.id
        selected = [f.id for f in files[: 1 if case == "one" else 2]]
    async with factory.begin() as db:
        arc_only = (await preview_arc_deletion(db, aid, uid))["token"]
        plan = await preview_cleanup(db, aid, uid, [], member_ids=selected)
        assert plan["paths"] == [str(p) for p in paths[: len(selected)]]
        assert plan["shared_arcs"] == [{"id": pid, "name": "Surviving arc"}]
        assert plan["skipped_issues"] == int(case != "one")
        assert all(p.is_file() for p in paths)
        with pytest.raises(StoryArcConflictError, match="shared"):
            await enqueue_reviewed_cleanup(
                db, mgr, aid, uid, "tester", [], plan["token"], member_ids=selected
            )
        job = await enqueue_reviewed_cleanup(
            db, mgr, aid, uid, "tester", [], plan["token"], member_ids=selected, allow_shared=True
        )
        jid = job.id
    if case == "finish_receipt":

        async def interrupted(*args):
            async with factory() as reader:
                active = await preview_arc_deletion(reader, aid, uid)
                assert active["pending_member_cleanup"] is None
                assert active["active_cleanup"] and not active["token"]
            raise ValidationError("Trash became unavailable")

        with monkeypatch.context() as failure:
            failure.setattr(owner, "complete_removal", interrupted)
            await mgr.dispatch_next()
        async with factory.begin() as db:
            assert (await db.get(UtilityJob, jid)).state == JobState.FAILED
            failed_item = await db.scalar(
                select(UtilityJobItem).where(
                    UtilityJobItem.job_id == jid, UtilityJobItem.item_index == 0
                )
            )
            assert "Trash became unavailable" in (failed_item.error_message or "")
            info = await preview_arc_deletion(db, aid, uid)
            assert not info["token"] and info["pending_member_cleanup"]["path"] == str(paths[0])
            with pytest.raises(StoryArcConflictError, match="cleanup"):
                await delete_reviewed_arc(db, aid, uid, arc_only)
            fixing = await owner.enqueue_member_recovery(
                db, mgr, aid, UUID(info["pending_member_cleanup"]["operation"]), "tester"
            )
            fix_id = fixing.id
        paths[0].write_bytes(b"new public file")
        await mgr.dispatch_next()
        async with factory() as db:
            assert (await db.get(UtilityJob, fix_id)).state == JobState.COMPLETED
            preview = await preview_arc_deletion(db, aid, uid)
            assert preview["pending_member_cleanup"] is None and preview["token"]
        assert paths[0].read_bytes() == b"new public file" and paths[1].exists()
        return
    if case == "cancel":

        async def cancel(*args, **kwargs):
            async with factory.begin() as db:
                await mgr.cancel_job(db, jid)
            return []

        monkeypatch.setattr(StoryArcCleanupExecutor, "after_item_commit", cancel)
    elif case == "changed":
        paths[1].write_bytes(b"changed since review")
    elif case == "new_peer":
        async with factory.begin() as db:
            db.add(
                IssueStoryArc(
                    story_arc=StoryArc(name="Unreviewed"),
                    issue_id=iid,
                    sequence_number=1,
                    source_ordinal=1,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
    await mgr.dispatch_next()
    async with factory() as db:
        assert await db.get(StoryArc, pid)
        saved = await db.get(Issue, iid)
        if case in {"changed", "new_peer"}:
            assert (await db.get(UtilityJob, jid)).state == JobState.FAILED
            assert await db.get(StoryArc, aid)
            assert all(p.is_file() for p in paths) and saved.status == IssueStatus.OWNED
        elif case == "cancel":
            assert (await db.get(UtilityJob, jid)).state == JobState.CANCELLED
            assert await db.get(StoryArc, aid)
            assert not paths[0].exists() and paths[1].is_file() and not saved.manual_skip
        else:
            assert (await db.get(UtilityJob, jid)).state == JobState.COMPLETED
            assert not await db.get(StoryArc, aid)
            assert not paths[0].exists() and paths[1].exists() == (case == "one")
            assert saved.manual_skip == (case != "one")
            assert saved.status == (IssueStatus.OWNED if case == "one" else IssueStatus.SKIPPED)
        assert paths[2].read_bytes() == b"unrelated.cbz"
    if case == "trash":
        assert sorted(p.read_bytes() for p in (tmp_path / "Trash").rglob("*.cbz")) == [
            b"alternate.cbz",
            b"first.cbz",
        ]
