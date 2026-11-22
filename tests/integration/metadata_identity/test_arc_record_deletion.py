"""Story Arc deletion must preserve original comics and retry evidence on both databases."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from pullbox.models.audit_log import AuditEventType, AuditLog
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc
from pullbox.models.user import User
from pullbox.services.story_arc_cleanup import enqueue_reviewed_cleanup, preview_cleanup
from pullbox.services.story_arc_deletion import (
    delete_reviewed_arc,
    preview_arc_deletion,
    review_arc_placement_paths,
)
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicyInput,
    StoryArcPlacementSyncService,
)
from pullbox.services.story_arc_service import StoryArcConflictError, StoryArcNotFoundError
from pullbox.utilities.executors.story_arc_cleanup import StoryArcCleanupExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobState, UtilityJob
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


async def test_failed_removal_receipt_survives_even_an_earlier_delete_confirmation(
    identity_probe_db: IdentityProbeDatabase,
) -> None:
    _, factory, _ = identity_probe_db
    async with factory.begin() as db:
        actor = User(username="operator", password_hash="unused")
        member = IssueStoryArc(
            story_arc=StoryArc(name="Interrupted"), sequence_number=1, source_ordinal=1
        )
        entry = StoryArcPlacement(
            issue_story_arc=member, placement_path="/arcs/001.cbz", ownership="managed", mode="copy"
        )
        db.add_all([actor, entry])
        await db.flush()
        arc_id, user_id, pid = member.story_arc_id, actor.id, entry.id
        token = (await preview_arc_deletion(db, arc_id, user_id))["token"]
        assert isinstance(token, str)
    async with factory.begin() as db:
        placement = await db.get(StoryArcPlacement, pid)
        placement.last_result = {
            "operation": "remove",
            "status": "failed",
            "trash_receipt": "receipt",
            "trash_root": "/trash",
        }
    async with factory.begin() as db:
        blocked = await preview_arc_deletion(db, arc_id, user_id)
        assert blocked["token"] is None and "removal" in str(blocked["blocker"]).lower()
        with pytest.raises(StoryArcConflictError, match="removal"):
            await delete_reviewed_arc(db, arc_id, user_id, token)
        assert (await db.get(StoryArcPlacement, pid)).last_result["trash_receipt"] == "receipt"


@pytest.mark.parametrize(
    ("mode", "trash_enabled", "outcome"),
    [
        (mode, trash, "success")
        for mode in ("copy", "hardlink", "symlink")
        for trash in (False, True)
    ]
    + [
        ("copy", False, action)
        for action in ("cancel_queued", "cancel_after_file", "changed_target", "expanded_arc")
    ]
    + [
        ("copy", False, "series_keep_both"),
        ("hardlink", False, "series_remove_copy"),
        ("copy", False, "series_both"),
        ("symlink", False, "series_both"),
        ("copy", False, "series_changed"),
        ("copy", False, "series_cancel"),
        ("copy", False, "series_peer"),
        ("copy", False, "series_failed_copy"),
        ("hardlink", False, "linked_original"),
        ("copy", False, "member_both"),
    ],
)
async def test_removing_a_reviewed_arc_representation_preserves_canonical_entry(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    mode: str,
    trash_enabled: bool,
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, _ = identity_probe_db
    canonical = tmp_path / "Comic.cbz"
    canonical.write_bytes(b"only canonical comic")
    canonical_inode = canonical.stat().st_ino
    dest = tmp_path / "Arcs"
    dest.mkdir()
    async with factory.begin() as db:
        if trash_enabled:
            trash = tmp_path / "trash"
            trash.mkdir()
            db.add(SystemConfig(key="utility_trash_folder", value=str(trash), value_type="string"))
        root = LibraryRoot(name="Library", path=str(tmp_path), enabled=True)
        series = Series(title="Comic", sort_title="comic", library_root=root)
        issue = Issue(series=series, issue_number=1)
        original = LibraryFile(
            library_root=root,
            issue=issue,
            file_path=str(canonical),
            file_name=canonical.name,
            file_size=canonical.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.fromtimestamp(canonical.stat().st_mtime, UTC),
        )
        arc = StoryArc(name="Crossover")
        member = IssueStoryArc(
            story_arc=arc,
            issue=issue,
            source_ordinal=1,
            sequence_number=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
        )
        db.add_all([original, member])
        await db.flush()
        arc_id, mid, root_id, revision, fid = arc.id, member.id, root.id, arc.revision, original.id
    svc = StoryArcPlacementSyncService()
    async with factory() as db:
        await svc.update_policy(
            db,
            arc_id,
            expected_revision=revision,
            proposal=StoryArcPlacementPolicyInput(
                mode=mode,
                target_library_root_id=root_id,
                destination_root=str(dest),
                symlink_style="relative" if mode == "symlink" else None,
            ),
        )
        result = await svc.sync_membership(db, arc_id, mid)
        assert result.placement
        path = Path(result.placement.placement_path)
        assert path.read_bytes() == canonical.read_bytes()
        assert path.is_symlink() == (mode == "symlink")
        assert (path.stat().st_ino == canonical_inode) == (mode != "copy")
        review = await review_arc_placement_paths(db, arc_id, 0)
        assert review["items"] == [
            dict(
                id=result.placement.id,
                path=str(path),
                mode=mode,
                ownership="managed",
                registered=False,
                selectable=True,
                interrupted=False,
            )
        ]
        actor = User(username="reviewer", password_hash="unused")
        other = tmp_path / "Externally managed.cbz"
        other.write_bytes(b"unselected")
        reference = StoryArcPlacement(issue_story_arc_id=mid, placement_path=str(other))
        db.add(reference)
        db.add(actor)
        await db.commit()
        ids = [] if outcome == "series_keep_both" else [result.placement.id]
        series_scope = outcome.startswith("series_")
        remove_original = outcome in {
            "member_both",
            "series_both",
            "series_changed",
            "series_cancel",
        }
        selection = (
            {"series_ids": [series.id], "series_file_ids": [fid] if remove_original else []}
            if series_scope
            else {"member_ids": [fid]}
            if remove_original
            else {}
        )
        with pytest.raises(StoryArcConflictError, match="proven managed"):
            await preview_cleanup(db, arc_id, actor.id, [reference.id])
        if outcome == "linked_original":
            with pytest.raises(StoryArcConflictError, match="hardlink"):
                await preview_cleanup(
                    db, arc_id, actor.id, ids, series_ids=[series.id], series_file_ids=[fid]
                )
            assert path.samefile(canonical) and canonical.read_bytes() == b"only canonical comic"
            return
        if outcome == "series_both":
            with pytest.raises(StoryArcConflictError, match="Review arc files"):
                await preview_cleanup(
                    db, arc_id, actor.id, [], series_ids=[series.id], series_file_ids=[fid]
                )
            assert path.exists() and canonical.exists()
        if outcome in {"series_peer", "series_failed_copy"}:
            selection["series_file_ids"] = [fid]
            remove_original = True
        plan = await preview_cleanup(db, arc_id, actor.id, ids, **selection)
        assert plan["paths"] == ([str(path)] if ids else []) + (
            [str(canonical)] if remove_original else []
        )
        assert plan["trash_enabled"] is trash_enabled
        if series_scope or remove_original:
            assert plan["arc_paths"] == ([str(path)] if ids else [])
            assert plan["library_paths"] == ([str(canonical)] if remove_original else [])
        mgr = JobQueueManager(factory)
        mgr.register_executor("story_arc_cleanup", StoryArcCleanupExecutor)
        with pytest.raises(StoryArcConflictError, match="changed"):
            await enqueue_reviewed_cleanup(
                db,
                mgr,
                arc_id,
                actor.id,
                actor.username,
                ids,
                (await preview_arc_deletion(db, arc_id, actor.id))["token"],
                **selection,
            )
        job = await enqueue_reviewed_cleanup(
            db, mgr, arc_id, actor.id, actor.username, ids, plan["token"], **selection
        )
        await db.commit()
        assert path.exists() and (await preview_arc_deletion(db, arc_id, actor.id))["blocker"]
        jid = job.id
        await db.commit()

        async def after_one(*args, **kwargs):
            async with factory.begin() as changed:
                if outcome in {"cancel_after_file", "series_cancel"}:
                    await mgr.cancel_job(changed, jid)
                elif outcome == "series_changed":
                    changed.add(Issue(series_id=series.id, issue_number=2))
                else:
                    changed.add(
                        IssueStoryArc(story_arc_id=arc_id, source_ordinal=9, sequence_number=9)
                    )
            return []

        if outcome == "cancel_queued":
            await mgr.cancel_job(db, jid)
            await db.commit()
        elif outcome == "changed_target":
            path.write_bytes(b"different bytes, must keep")
        elif outcome == "series_failed_copy":
            path.write_bytes(b"changed copy")
        elif outcome == "series_peer":
            db.add(
                StoryArcPlacement(
                    issue_story_arc=IssueStoryArc(
                        story_arc=StoryArc(name="Other arc"),
                        issue_id=issue.id,
                        source_ordinal=1,
                        sequence_number=1,
                    ),
                    library_file_id=fid,
                    placement_path=str(tmp_path / "Other arc.cbz"),
                    ownership="managed",
                    mode="copy",
                )
            )
            await db.commit()
        elif outcome in {"cancel_after_file", "expanded_arc", "series_cancel", "series_changed"}:
            monkeypatch.setattr(StoryArcCleanupExecutor, "after_item_commit", after_one)
        await mgr.dispatch_next()
        sid = series.id
        db.expire_all()
        if series_scope or remove_original:
            current_job = await db.get(UtilityJob, jid)
            if outcome in {"series_peer", "series_failed_copy"}:
                assert current_job.state == JobState.FAILED
                assert path.exists() and canonical.read_bytes() == b"only canonical comic"
                assert await db.get(Series, sid) and await db.get(StoryArc, arc_id)
            elif outcome in {"series_cancel", "series_changed"}:
                assert current_job.state == (
                    JobState.CANCELLED if outcome == "series_cancel" else JobState.FAILED
                )
                assert await db.get(Series, sid) and await db.get(StoryArc, arc_id)
                assert not path.exists() and canonical.read_bytes() == b"only canonical comic"
            else:
                assert current_job.state == JobState.COMPLETED, current_job.error_message
                assert not await db.get(StoryArc, arc_id)
                assert bool(await db.get(Series, sid)) != series_scope
                assert path.exists() == (not ids) and not path.is_symlink()
                assert canonical.exists() != remove_original
                if series_scope and ids:
                    audit = await db.scalar(
                        select(AuditLog.metadata_json).where(
                            AuditLog.event_type == AuditEventType.STORY_ARC_RECORD_DELETED
                        )
                    )
                    counts = json.loads(audit)
                    assert counts["removed_arc_files"] == 1
                    assert counts.get("removed_series_files", 0) == int(remove_original)
                if not remove_original:
                    assert canonical.read_bytes() == b"only canonical comic"
            assert other.read_bytes() == b"unselected"
            return
        assert (
            canonical.read_bytes() == b"only canonical comic"
            and other.read_bytes() == b"unselected"
        )
        if outcome != "success":
            expected = JobState.CANCELLED if outcome.startswith("cancel") else JobState.FAILED
            assert (await db.get(UtilityJob, jid)).state == expected
            assert await db.get(StoryArc, arc_id)
            if outcome == "cancel_queued":
                assert path.read_bytes() == canonical.read_bytes()
            elif outcome == "changed_target":
                assert path.read_bytes() == b"different bytes, must keep"
            else:
                assert not path.exists()
            return
        assert (await db.get(UtilityJob, jid)).state == JobState.COMPLETED
        assert await db.get(StoryArc, arc_id) is None
        if trash_enabled:
            [backup] = [p for p in trash.rglob("*") if not p.is_dir()]
            if mode == "symlink":
                assert backup.is_symlink()
            else:
                assert backup.read_bytes() == canonical.read_bytes()
                assert not backup.samefile(canonical)
        assert not path.exists() and not path.is_symlink()
        assert await db.get(LibraryFile, fid)
    assert (
        canonical.read_bytes() == b"only canonical comic"
        and canonical.stat().st_ino == canonical_inode
    )


async def test_delete_confirmation_is_single_use_and_preserves_import_evidence(
    identity_probe_db: IdentityProbeDatabase,
) -> None:
    _, factory, _ = identity_probe_db
    async with factory.begin() as db:
        actor, other = [User(username=name, password_hash="unused") for name in ("one", "two")]
        arc, protected = [StoryArc(name=name) for name in ("Removable", "Imported")]
        job = ImportJob(
            source_type=ImportSourceType.MYLAR3,
            source_path="/legacy",
            status=ImportJobStatus.COMPLETED,
        )
        db.add_all([actor, other, arc, protected, job])
        await db.flush()
        db.add(IssueStoryArc(story_arc_id=arc.id, sequence_number=1, source_ordinal=1))
        staged = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key="reading-order",
            source_ordinal=1,
            materialized_story_arc_id=protected.id,
        )
        db.add(staged)
        await db.flush()
        aid, bid, arc_id, protected_id, staged_id = (
            actor.id,
            other.id,
            arc.id,
            protected.id,
            staged.id,
        )
    async with factory.begin() as db:
        blocked = await preview_arc_deletion(db, protected_id, aid)
        assert "import" in str(blocked["blocker"]) and blocked["token"] is None
        preview = await preview_arc_deletion(db, arc_id, aid)
        token = preview["token"]
        assert isinstance(token, str) and preview["entries"] == 1 and preview["issues"] == 0
    with pytest.raises(StoryArcConflictError):
        async with factory.begin() as db:
            await delete_reviewed_arc(db, arc_id, bid, token)

    async def confirm() -> bool:
        try:
            async with factory.begin() as db:
                await delete_reviewed_arc(db, arc_id, aid, token)
            return True
        except StoryArcNotFoundError:
            return False

    assert sorted(await asyncio.gather(confirm(), confirm())) == [False, True]
    async with factory.begin() as db:
        assert (await db.get(ImportedStoryArc, staged_id)).materialized_story_arc_id == protected_id
        assert await db.get(StoryArc, protected_id)
        assert not await db.scalar(select(func.count(IssueStoryArc.id)))
        assert (
            await db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.event_type == AuditEventType.STORY_ARC_RECORD_DELETED
                )
            )
            == 1
        )
