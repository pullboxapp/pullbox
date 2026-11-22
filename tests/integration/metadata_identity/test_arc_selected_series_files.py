"""Series deletion grants no physical authority except for the signed paths."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState
from pullbox.models.user import User
from pullbox.services import story_arc_member_cleanup as receipts
from pullbox.services.story_arc_cleanup import enqueue_reviewed_cleanup, preview_cleanup
from pullbox.services.story_arc_deletion import preview_arc_deletion
from pullbox.services.story_arc_service import StoryArcConflictError
from pullbox.utilities.executors.story_arc_cleanup import StoryArcCleanupExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobState, UtilityJob
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


@pytest.mark.parametrize(
    "case",
    [
        "delete",
        "trash",
        "cancel",
        "changed_file",
        "changed_peer",
        "added_issue",
        "journal",
        "nested_root",
        "same_issue",
    ],
)
async def test_only_selected_series_paths_are_removed(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, _ = identity_probe_db
    manager = JobQueueManager(factory)
    manager.register_executor("story_arc_cleanup", StoryArcCleanupExecutor)
    paths = [tmp_path / f"comic-{i}.cbz" for i in range(4)]
    if case == "nested_root":
        sub = tmp_path / "Read only"
        sub.mkdir()
        paths[0] = sub / paths[0].name
    loose = tmp_path / "not-in-pullbox.cbz"
    loose.write_bytes(b"untouched")
    async with factory.begin() as db:
        root = LibraryRoot(name="Library", path=str(tmp_path))
        parent, keep = [Series(title=t, sort_title=t) for t in ("Selected", "Survives")]
        issues = [
            Issue(series=s, issue_number=n, status=IssueStatus.OWNED)
            for s, n in ((parent, 2), (parent, 1), (parent, 3), (keep, 1))
        ]
        arc, peer = [StoryArc(name=n) for n in ("Delete arc", "Other order")]
        actor = User(username="operator", password_hash="unused")
        db.add(actor)
        for owner, issue, seq in ((arc, issues[1], 1), (arc, issues[3], 2), (peer, issues[0], 1)):
            db.add(
                IssueStoryArc(
                    story_arc=owner,
                    issue=issue,
                    source_ordinal=seq,
                    sequence_number=seq,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
        books = []
        for i, (issue, path) in enumerate(zip(issues, paths, strict=True)):
            path.write_bytes(path.name.encode())
            book = LibraryFile(
                issue=issues[0] if case == "same_issue" and i in (1, 2) else issue,
                library_root=root,
                file_path=str(path),
                file_name=path.name,
                file_format=FileFormat.CBZ,
                file_size=path.stat().st_size,
                file_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
                storage_mode=LibraryFileStorageMode.REFERENCED
                if i == 2
                else LibraryFileStorageMode.MANAGED,
            )
            db.add(book)
            books.append(book)
        if case == "trash":
            (tmp_path / "Trash").mkdir()
            db.add(
                SystemConfig(
                    key="utility_trash_folder", value_type="string", value=str(tmp_path / "Trash")
                )
            )
        await db.flush()
        ids = [b.id for b in books]
        aid, pid, sid, uid, iid = arc.id, peer.id, parent.id, actor.id, issues[0].id
    async with factory.begin() as db:
        args = {"series_ids": [sid], "series_file_ids": ids[:2]}
        plan = await preview_cleanup(db, aid, uid, [], **args)
        assert plan["paths"] == [str(p) for p in paths[:2]]
        assert plan["record_series"][0]["files"] == 3 and plan["shared_arcs"][0]["id"] == pid
        assert plan["trash_enabled"] == (case == "trash")
        for invalid in ids[2:]:
            with pytest.raises(StoryArcConflictError):
                await preview_cleanup(db, aid, uid, [], series_ids=[sid], series_file_ids=[invalid])
        with pytest.raises(StoryArcConflictError, match="shared"):
            await enqueue_reviewed_cleanup(
                db, manager, aid, uid, "operator", [], plan["token"], **args
            )
        job = await enqueue_reviewed_cleanup(
            db, manager, aid, uid, "operator", [], plan["token"], allow_shared=True, **args
        )
        jid = job.id
    if case == "changed_file":
        paths[0].write_bytes(b"replacement bytes")
    if case == "nested_root":
        async with factory.begin() as db:
            db.add(
                LibraryRoot(
                    name="Protected subtree", path=str(paths[0].parent), allow_managed_writes=False
                )
            )
    if case == "journal":
        real = receipts.complete_removal

        async def interrupt(*args):
            raise ValidationError("Trash unavailable")

        monkeypatch.setattr(receipts, "complete_removal", interrupt)
    if case in {"cancel", "changed_peer", "added_issue"}:

        async def interrupt_after_first(*args, **kwargs):
            if paths[0].exists() or not paths[1].exists():
                return []
            async with factory.begin() as db:
                if case == "cancel":
                    await manager.cancel_job(db, jid)
                elif case == "changed_peer":
                    other = await db.get(StoryArc, pid)
                    other.revision += 1
                else:
                    db.add(Issue(series_id=sid, issue_number=4))
            return []

        monkeypatch.setattr(StoryArcCleanupExecutor, "after_item_commit", interrupt_after_first)
    await manager.dispatch_next()
    async with factory.begin() as db:
        job = await db.get(UtilityJob, jid)
        assert await db.get(StoryArc, pid)
        if case in {"delete", "trash", "same_issue"}:
            assert job.state == JobState.COMPLETED, job.error_message
            assert not await db.get(StoryArc, aid) and not await db.get(Series, sid)
            assert not paths[0].exists() and not paths[1].exists()
            assert (
                await db.scalar(select(IssueStoryArc).where(IssueStoryArc.story_arc_id == pid))
            ).issue_id is None
        else:
            assert job.state == (JobState.CANCELLED if case == "cancel" else JobState.FAILED)
            assert await db.get(StoryArc, aid) and await db.get(Series, sid)
            assert paths[1].read_bytes() == paths[1].name.encode()
            if case == "changed_file":
                assert paths[0].read_bytes() == b"replacement bytes"
            elif case == "nested_root":
                assert paths[0].read_bytes() == paths[0].name.encode()
            else:
                assert not paths[0].exists() and (await db.get(Issue, iid)).manual_skip
            if case == "journal":
                marker = (await preview_arc_deletion(db, aid, uid))["pending_member_cleanup"]
                operation = UUID(marker["operation"])
                await receipts.enqueue_member_recovery(db, manager, aid, operation, "operator")
    if case == "journal":
        monkeypatch.setattr(receipts, "complete_removal", real)
        await manager.dispatch_next()
        async with factory.begin() as db:
            assert (await preview_arc_deletion(db, aid, uid))["token"]
            assert await db.get(Series, sid)
    assert [p.read_bytes() for p in paths[2:]] == [p.name.encode() for p in paths[2:]]
    assert loose.read_bytes() == b"untouched"
    if case == "trash":
        assert sorted(p.read_bytes() for p in (tmp_path / "Trash").rglob("*.cbz")) == [
            p.name.encode() for p in paths[:2]
        ]
