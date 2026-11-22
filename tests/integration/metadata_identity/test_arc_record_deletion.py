"""Record-only Story Arc deletion must preserve import evidence on both databases."""

import asyncio

import pytest
from sqlalchemy import func, select

from pullbox.models.audit_log import AuditEventType, AuditLog
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcSourceKind
from pullbox.models.story_arc_import import ImportedStoryArc
from pullbox.models.user import User
from pullbox.services.story_arc_deletion import delete_reviewed_arc, preview_arc_deletion
from pullbox.services.story_arc_service import StoryArcConflictError, StoryArcNotFoundError
from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


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
