"""Deletion impact must describe actual member files, not every file in a series."""

from datetime import UTC, datetime

import pytest

from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportJob, ImportSourceType
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
)
from pullbox.services.story_arc_member_review import member_files, sharing_arcs
from pullbox.services.story_arc_service import StoryArcNotFoundError
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


async def test_review_member_files_and_shared_arcs(
    identity_probe_db: IdentityProbeDatabase,
) -> None:
    _, factory, _ = identity_probe_db
    async with factory.begin() as db:
        root = LibraryRoot(name="Mylar", path="/legacy", allow_managed_writes=False)
        series = Series(title="X-Manowar", sort_title="x-manowar", library_root=root)
        issue = Issue(series=series, issue_number=50, issue_number_text="50-x")
        outside = Issue(series=series, issue_number=51)
        arc = StoryArc(name="Crossover")
        peers = [
            StoryArc(name=f"Other {i}", lifecycle=StoryArcLifecycle.ARCHIVED) for i in range(21)
        ]
        for owner in [arc, *peers]:
            db.add(
                IssueStoryArc(
                    story_arc=owner,
                    issue=issue,
                    sequence_number=1,
                    source_ordinal=1,
                    resolution_state=StoryArcResolutionState.RESOLVED,
                )
            )
        db.add(
            IssueStoryArc(
                story_arc=arc,
                issue=outside,
                sequence_number=2,
                source_ordinal=2,
                resolution_state=StoryArcResolutionState.MISSING,
            )
        )
        await db.flush()
        files = [
            LibraryFile(
                issue_id=None,
                library_root=root,
                file_path=f"/legacy/{i}.cbz",
                file_name=f"{i}.cbz",
                file_size=1,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(UTC),
                storage_mode=LibraryFileStorageMode.REFERENCED
                if not i
                else LibraryFileStorageMode.MANAGED,
            )
            for i in range(22)
        ]
        for i, book in enumerate(files):
            book.issue_id = issue.id if i < 21 else outside.id
        db.add_all(files)
        job = ImportJob(source_type=ImportSourceType.MYLAR3, source_path="/legacy/mylar.db")
        db.add(job)
        await db.flush()
        staged = ImportedSeries(import_job_id=job.id, raw_series_name="X-Manowar")
        db.add(staged)
        await db.flush()
        db.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=staged.id,
                file_path="/legacy/0.cbz",
                file_name="0.cbz",
                file_format="cbz",
                library_file_id=files[0].id,
            )
        )
        await db.flush()
        page = await member_files(db, arc.id, 0)
        assert [r["id"] for r in page["items"]] == [f.id for f in files[:20]]
        assert page["items"][0] == dict(
            id=files[0].id,
            series="X-Manowar",
            issue="50-X",
            path="/legacy/0.cbz",
            ownership="referenced",
            root_enabled=True,
            root_writable=False,
            import_linked=True,
            shared=21,
        )
        assert page["items"][1]["ownership"] == "managed" and not page["items"][1]["import_linked"]
        last = await member_files(db, arc.id, page["next_after"])
        assert [r["id"] for r in last["items"]] == [files[20].id] and last["next_after"] is None
        shared = await sharing_arcs(db, arc.id, files[0].id, 0)
        assert shared["items"] == [
            dict(id=a.id, name=a.name, lifecycle="archived") for a in peers[:20]
        ]
        tail = await sharing_arcs(db, arc.id, files[0].id, shared["next_after"])
        assert tail == dict(
            items=[dict(id=peers[-1].id, name="Other 20", lifecycle="archived")], next_after=None
        )
        with pytest.raises(StoryArcNotFoundError):
            await sharing_arcs(db, arc.id, files[-1].id, 0)
        with pytest.raises(StoryArcNotFoundError):
            await member_files(db, arc.id + 1000, 0)
