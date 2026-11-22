"""An arc can release a reviewed file, not its canonical issue or collection."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.models.pending_match import PendingMatch
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState
from pullbox.services.library_removal import (
    prepare_removal,
    record_removal,
    recover_uncommitted_removal,
)
from pullbox.services.library_removal_cleanup import complete_removal
from pullbox.services.search_targets import wanted_issue_eligibility_filter
from pullbox.services.story_arc_member_removal import review_member_removal, stage_member_removal
from pullbox.services.story_arc_service import StoryArcConflictError
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


@pytest.mark.parametrize(
    "case",
    [
        "last",
        "alternate",
        "trash",
        "interrupt",
        "post_stage_failure",
        "changed",
        "new_peer",
        "lost_alternate",
        "referenced",
        "read_only",
        "import_source",
        "action_history",
        "intervention",
        "unmatched",
        "wrong_plan",
        "symlink",
    ],
)
async def test_member_removal_preserves_issue_intent(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, _ = identity_probe_db
    source = tmp_path / "member.cbz"
    source.write_bytes(b"member comic")
    spare = tmp_path / "another copy.cbz"
    unrelated = tmp_path / "unrelated.cbz"
    unrelated.write_bytes(b"another issue")
    async with factory.begin() as db:
        root = LibraryRoot(name="Comics", path=str(tmp_path))
        series = Series(title="Comic", sort_title="comic", library_root=root, monitored=True)
        issue = Issue(series=series, issue_number=1, status=IssueStatus.OWNED)
        other = Issue(series=series, issue_number=2, status=IssueStatus.OWNED)
        arc, peer = [StoryArc(name=name, monitored=True) for name in ("Reviewed arc", "Shared arc")]
        for owner in (arc, peer):
            db.add(
                IssueStoryArc(
                    story_arc=owner,
                    issue=issue,
                    sequence_number=1,
                    source_ordinal=1,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
        db.add(other)
        await db.flush()

        def book(path, issue_id):
            stat = path.stat()
            return LibraryFile(
                library_root=root,
                issue_id=issue_id,
                file_path=str(path),
                file_name=path.name,
                file_size=stat.st_size,
                file_modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                file_format=FileFormat.CBZ,
            )

        original = book(source, issue.id)
        db.add_all([original, book(unrelated, other.id)])
        if case in {"alternate", "lost_alternate"}:
            spare.write_bytes(b"alternate")
            db.add(book(spare, issue.id))
        await db.flush()
        arc_id, peer_id, issue_id, file_id, root_id = (
            arc.id,
            peer.id,
            issue.id,
            original.id,
            root.id,
        )

    async with factory.begin() as db:
        with pytest.raises(StoryArcConflictError, match="shared"):
            await review_member_removal(db, arc_id, file_id, shared_arcs=[])
        review = await review_member_removal(db, arc_id, file_id, shared_arcs=[peer_id])
    trash = tmp_path / "trash" / "member.cbz" if case == "trash" else None
    plan = prepare_removal(
        source,
        root_id=root_id,
        root_path=tmp_path,
        disposition="trash" if trash else "delete",
        trash_path=trash,
    )
    async with factory.begin() as db:
        await record_removal(db, plan)
    if case == "changed":
        source.write_bytes(b"replaced bytes")
    elif case == "lost_alternate":
        spare.unlink()
    elif case == "new_peer":
        async with factory.begin() as db:
            db.add(
                IssueStoryArc(
                    story_arc=StoryArc(name="Unreviewed arc"),
                    issue_id=issue_id,
                    sequence_number=1,
                    source_ordinal=1,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
    elif case == "symlink":
        source.unlink()
        source.symlink_to(unrelated)
    elif case == "wrong_plan":
        plan = prepare_removal(unrelated, root_id=root_id, root_path=tmp_path, disposition="delete")
        async with factory.begin() as db:
            await record_removal(db, plan)
    elif case in {
        "referenced",
        "read_only",
        "import_source",
        "action_history",
        "intervention",
        "unmatched",
    }:
        async with factory.begin() as db:
            if case == "referenced":
                (
                    await db.get(LibraryFile, file_id)
                ).storage_mode = LibraryFileStorageMode.REFERENCED
            elif case == "read_only":
                (await db.get(LibraryRoot, root_id)).allow_managed_writes = False
            elif case == "unmatched":
                (
                    await db.get(IssueStoryArc, review.member_id)
                ).resolution_state = StoryArcResolutionState.AMBIGUOUS
            elif case == "intervention":
                db.add(
                    PendingMatch(
                        issue_id=issue_id,
                        release_title="replacement",
                        download_url="https://example.invalid",
                        confidence="medium",
                    )
                )
            else:
                job = ImportJob(source_type=ImportSourceType.MYLAR3, source_path="/legacy/mylar.db")
                db.add(job)
                await db.flush()
                if case == "action_history":
                    db.add(
                        ImportJobAction(
                            import_job_id=job.id,
                            sequence_no=1,
                            phase="import",
                            action_type="library_file_registered",
                            payload={"destination_path": str(source)},
                        )
                    )
                else:
                    staged = ImportedSeries(import_job_id=job.id, raw_series_name="Comic")
                    db.add(staged)
                    await db.flush()
                    db.add(
                        ImportedFile(
                            import_job_id=job.id,
                            import_series_id=staged.id,
                            file_path=str(source),
                            file_name=source.name,
                            file_format="cbz",
                        )
                    )
    if case == "post_stage_failure":
        monkeypatch.setattr(
            "pullbox.services.story_arc_member_removal.matches", lambda *args: False
        )
        async with factory.begin() as db:
            with pytest.raises(StoryArcConflictError):
                await stage_member_removal(db, review, plan)
        async with factory() as fresh:
            assert await recover_uncommitted_removal(fresh, plan.operation_id) == "abandoned"
        assert source.read_bytes() == b"member comic"
    elif case not in {"last", "trash", "alternate", "interrupt"}:
        with pytest.raises(StoryArcConflictError):
            async with factory.begin() as db:
                await stage_member_removal(db, review, plan)
        assert source.exists() and not plan.stage.exists()
    elif case == "interrupt":
        async with factory() as db:
            await stage_member_removal(db, review, plan)
            assert not source.exists()
            await db.rollback()
        async with factory() as fresh:
            assert await recover_uncommitted_removal(fresh, plan.operation_id) == "abandoned"
        assert source.read_bytes() == b"member comic"
    else:
        async with factory.begin() as db:
            await stage_member_removal(db, review, plan)
        async with factory() as fresh:
            assert await complete_removal(fresh, plan.operation_id) == "complete"
        assert not source.exists()
        if trash:
            assert trash.read_bytes() == b"member comic"
    async with factory() as db:
        saved = await db.get(Issue, issue_id)
        assert saved and saved.series_id and await db.get(StoryArc, peer_id)
        assert (await db.get(LibraryFile, file_id) is None) == (
            case in {"last", "alternate", "trash"}
        )
        skipped = case in {"last", "trash"}
        assert saved.manual_skip is skipped
        assert saved.status == (IssueStatus.SKIPPED if skipped else IssueStatus.OWNED)
        assert not await db.scalar(
            select(Issue.id)
            .join(Series)
            .where(Issue.id == issue_id, wanted_issue_eligibility_filter())
        )
    assert unrelated.read_bytes() == b"another issue"
    if case == "alternate":
        assert spare.read_bytes() == b"alternate"
