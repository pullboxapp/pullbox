"""API contracts for explicit Step 3 story-arc review decisions."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

if TYPE_CHECKING:
    from httpx import AsyncClient


def _csrf_header_for(client: AsyncClient) -> dict[str, str]:
    from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

    session_token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    csrf = AuthService.get_csrf_token_from_session(session_token) or ""
    return {"X-CSRF-Token": csrf}


@pytest.mark.asyncio
async def test_story_arc_decision_endpoint_persists_explicit_merge_selection(
    authenticated_client: AsyncClient,
    sec_db,  # type: ignore[no-untyped-def]
) -> None:
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
    from pullbox.models.story_arc import (
        ImportedStoryArcStatus,
        StoryArc,
        StoryArcResolutionState,
        StoryArcSourceKind,
    )
    from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/mylar.db",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.REVIEW,
        )
        existing = StoryArc(name="Knightfall")
        session.add_all([job, existing])
        await session.flush()
        staged = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key="mylar3:knightfall",
            source_ordinal=1,
            name="Knightfall",
            status=ImportedStoryArcStatus.DETECTED,
        )
        session.add(staged)
        await session.flush()
        entry = ImportedStoryArcEntry(
            imported_story_arc_id=staged.id,
            source_ordinal=1,
            reading_order=10,
            reading_order_raw="010",
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_series_name="Batman",
            source_issue_number_text="497",
        )
        session.add(entry)
        await session.commit()
        job_id = int(job.id)
        staged_id = int(staged.id)
        existing_id = int(existing.id)

    response = await authenticated_client.put(
        f"/api/v1/import/{job_id}/story-arcs/{staged_id}/decision",
        headers=_csrf_header_for(authenticated_client),
        json={"action": "select", "proposed_story_arc_id": existing_id},
    )

    assert response.status_code == 200
    assert response.json() == {
        "imported_story_arc_id": staged_id,
        "status": "ready",
        "selected_for_import": True,
        "proposed_story_arc_id": existing_id,
    }
    async with sec_db() as session:
        refreshed = await session.get(ImportedStoryArc, staged_id)
        entries = list(
            (
                await session.execute(
                    select(ImportedStoryArcEntry).where(
                        ImportedStoryArcEntry.imported_story_arc_id == staged_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert refreshed is not None
        assert refreshed.status == ImportedStoryArcStatus.READY
        assert refreshed.selected_for_import is True
        assert refreshed.proposed_story_arc_id == existing_id
        assert [item.selected_for_import for item in entries] == [True]


@pytest.mark.asyncio
async def test_story_arc_decision_endpoint_rejects_conflict_selection(
    authenticated_client: AsyncClient,
    sec_db,  # type: ignore[no-untyped-def]
) -> None:
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
    from pullbox.models.story_arc import (
        ImportedStoryArcStatus,
        StoryArcResolutionState,
        StoryArcSourceKind,
    )
    from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/import",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
        )
        session.add(job)
        await session.flush()
        staged = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.FOLDER,
            source_key="folder:conflict",
            source_ordinal=1,
            name="Identity Crisis",
            status=ImportedStoryArcStatus.NEEDS_REVIEW,
        )
        session.add(staged)
        await session.flush()
        session.add(
            ImportedStoryArcEntry(
                imported_story_arc_id=staged.id,
                source_ordinal=1,
                resolution_state=StoryArcResolutionState.CONFLICT,
                source_kind=StoryArcSourceKind.FOLDER,
                source_series_name="Identity Crisis",
                source_issue_number_text="1",
            )
        )
        await session.commit()
        job_id = int(job.id)
        staged_id = int(staged.id)

    response = await authenticated_client.put(
        f"/api/v1/import/{job_id}/story-arcs/{staged_id}/decision",
        headers=_csrf_header_for(authenticated_client),
        json={"action": "select"},
    )

    assert response.status_code == 409
    assert "conflict" in response.json()["detail"]
    async with sec_db() as session:
        refreshed = await session.get(ImportedStoryArc, staged_id)
        assert refreshed is not None
        assert refreshed.status == ImportedStoryArcStatus.NEEDS_REVIEW
        assert refreshed.selected_for_import is False
