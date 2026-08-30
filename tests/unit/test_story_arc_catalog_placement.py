"""One-time catalog placements remain independent of future synchronization."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import StoryArc, StoryArcPlacement
from pullbox.providers.base import IssueMetadata, SeriesMetadata
from pullbox.providers.story_arcs import StoryArcMetadata
from pullbox.services.story_arc_catalog import StoryArcCatalogService
from pullbox.services.story_arc_placement_integration import StoryArcPlacementPolicyInput


async def _create(session, tmp_path, *, skip=False):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    destination = tmp_path / "arc-files"
    destination.mkdir()
    source = canonical / "Original 001.cbz"
    source.write_bytes(b"canonical comic")
    root = LibraryRoot(name="Canonical", path=str(canonical))
    target_root = LibraryRoot(name="Arcs", path=str(destination))
    series = Series(comicvine_id=21, title="Canonical series", sort_title="Canonical series")
    issue = Issue(series=series, comicvine_id=11, issue_number=1)
    file = LibraryFile(
        issue=issue,
        library_root=root,
        file_path=str(source),
        file_name=source.name,
        file_size=len(source.read_bytes()),
        file_modified_at=datetime.now(UTC),
        file_format=FileFormat.CBZ,
    )
    session.add_all([file, target_root])
    await session.commit()
    provider = SimpleNamespace(
        get_story_arc=AsyncMock(
            return_value=StoryArcMetadata("31", "An Arc", ("11",), membership_complete=True)
        ),
        get_story_arc_issues=AsyncMock(
            return_value=[
                IssueMetadata(
                    "11", "21", 1, None, None, None, None, None, None, None, issue_number_text="1"
                )
            ]
        ),
        get_series=AsyncMock(
            return_value=SeriesMetadata(
                "21", "Canonical series", None, None, None, None, None, None, None, None, None
            )
        ),
    )
    service = StoryArcCatalogService(provider)
    arc = await service.add(
        session,
        await service.preview("31"),
        ordered_issue_provider_ids=["11"],
        skipped_issue_provider_ids=["11"] if skip else [],
        library_root_id=root.id,
        placement_policy=StoryArcPlacementPolicyInput(
            "copy",
            target_root.id,
            str(destination),
            file_template="{OriginalFilename}",
            synchronize=False,
        ),
    )
    arc_id = arc.id
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    return arc_id, source, destination, factory


async def test_add_records_initial_copy_even_with_future_sync_off(db_session, tmp_path):
    arc_id, source, destination, factory = await _create(db_session, tmp_path)
    arc = await db_session.get(StoryArc, arc_id)
    assert arc.sync_enabled is False
    marker = arc.diagnostics["catalog_initial_placements"]
    assert marker["pending"] == marker["total"] == 1
    assert not list(destination.iterdir())
    assert str(source) not in str(marker)
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

    result = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert (result.completed, result.pending, result.failed) == (1, 0, 0)
    async with factory() as session:
        arc = await session.get(StoryArc, arc_id)
        placement = await session.scalar(select(StoryArcPlacement))
        assert arc.sync_enabled is False
        assert placement is not None
        assert placement.ownership.value == "managed"
        assert arc.diagnostics["catalog_initial_placements"]["state"] == "complete"
    copied = list(destination.rglob("*.cbz"))
    assert len(copied) == 1 and copied[0].name == source.name
    assert copied[0].read_bytes() == source.read_bytes() == b"canonical comic"
    again = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert again.completed == 1 and len(list(destination.rglob("*.cbz"))) == 1


async def test_initial_copy_skips_explicitly_skipped_members(db_session, tmp_path):
    arc_id, source, destination, factory = await _create(db_session, tmp_path, skip=True)
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

    result = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert result.total == 0
    assert source.exists() and not list(destination.iterdir())


async def test_initial_copy_failure_is_visible_and_explicitly_retryable(
    db_session, tmp_path, monkeypatch
):
    arc_id, source, destination, factory = await _create(db_session, tmp_path)
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements
    from pullbox.services.story_arc_placement_integration import (
        StoryArcPlacementIntegrationError,
        StoryArcPlacementSyncService,
    )

    with monkeypatch.context() as context:
        context.setattr(
            StoryArcPlacementSyncService,
            "sync_membership",
            AsyncMock(
                side_effect=StoryArcPlacementIntegrationError(
                    "permission_denied", "private details"
                )
            ),
        )
        result = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert result.failed == 1 and result.completed == 0
    async with factory() as session:
        marker = (await session.get(StoryArc, arc_id)).diagnostics["catalog_initial_placements"]
        assert marker["items"][0]["error_code"] == "permission_denied"
        assert "private details" not in str(marker)
    retry = await run_catalog_initial_placements(arc_id, session_factory=factory, retry_failed=True)
    assert retry.failed == 0 and retry.completed == 1
    assert source.exists() and len(list(destination.rglob("*.cbz"))) == 1


@pytest.mark.parametrize("change", ["arc_revision", "file_identity"])
async def test_initial_copy_fails_closed_on_changed_review_context(db_session, tmp_path, change):
    arc_id, _source, destination, factory = await _create(db_session, tmp_path)
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

    async with factory() as session:
        if change == "arc_revision":
            arc = await session.get(StoryArc, arc_id)
            arc.revision += 1
        else:
            library_file = await session.scalar(select(LibraryFile))
            library_file.file_size += 1
        await session.commit()
    result = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert result.failed == 1 and result.completed == 0
    assert not list(destination.iterdir())


async def test_initial_copy_resumes_persisted_incomplete_work(db_session, tmp_path):
    arc_id, _source, destination, factory = await _create(db_session, tmp_path)
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

    async with factory() as session:
        arc = await session.get(StoryArc, arc_id)
        marker = dict(arc.diagnostics["catalog_initial_placements"])
        marker["items"][0]["state"] = "running"
        arc.diagnostics = {**arc.diagnostics, "catalog_initial_placements": marker}
        await session.commit()
    result = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert result.completed == 1 and result.pending == 0
    assert len(list(destination.rglob("*.cbz"))) == 1


async def test_cancelled_initial_copy_keeps_restart_work_and_source_intact(
    db_session, tmp_path, monkeypatch
):
    arc_id, source, destination, factory = await _create(db_session, tmp_path)
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements
    from pullbox.services.story_arc_placement_integration import StoryArcPlacementSyncService

    with monkeypatch.context() as context:
        context.setattr(
            StoryArcPlacementSyncService,
            "sync_membership",
            AsyncMock(side_effect=asyncio.CancelledError),
        )
        with pytest.raises(asyncio.CancelledError):
            await run_catalog_initial_placements(arc_id, session_factory=factory)
    async with factory() as session:
        marker = (await session.get(StoryArc, arc_id)).diagnostics["catalog_initial_placements"]
        assert marker["pending"] == 1 and marker["items"][0]["state"] == "running"
    assert source.exists() and not list(destination.iterdir())
    result = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert result.completed == 1


async def test_completed_copy_reconciles_after_progress_checkpoint_loss(db_session, tmp_path):
    arc_id, source, destination, factory = await _create(db_session, tmp_path)
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

    await run_catalog_initial_placements(arc_id, session_factory=factory)
    async with factory() as session:
        arc = await session.get(StoryArc, arc_id)
        marker = dict(arc.diagnostics["catalog_initial_placements"])
        marker["items"][0]["state"] = "running"
        arc.diagnostics = {**arc.diagnostics, "catalog_initial_placements": marker}
        await session.commit()
    result = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert result.completed == 1
    assert source.exists() and len(list(destination.rglob("*.cbz"))) == 1


async def test_concurrent_initial_copy_requests_are_idempotent(db_session, tmp_path):
    arc_id, _source, destination, factory = await _create(db_session, tmp_path)
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

    results = await asyncio.gather(
        *[run_catalog_initial_placements(arc_id, session_factory=factory) for _ in range(2)]
    )
    assert all(result.completed == 1 for result in results)
    assert len(list(destination.rglob("*.cbz"))) == 1


async def test_changed_member_state_never_copies_after_add(db_session, tmp_path):
    arc_id, source, destination, factory = await _create(db_session, tmp_path)
    from pullbox.models.story_arc import IssueStoryArc, StoryArcResolutionState
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

    async with factory() as session:
        member = await session.scalar(select(IssueStoryArc))
        member.resolution_state = StoryArcResolutionState.SKIPPED
        await session.commit()
    result = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert result.failed == 1
    assert source.exists() and not list(destination.iterdir())


async def test_initial_copy_uses_same_lowest_canonical_file_as_manual_sync(db_session, tmp_path):
    arc_id, source, destination, factory = await _create(db_session, tmp_path)
    from pullbox.services.story_arc_catalog_placement import (
        initialize_catalog_placements,
        run_catalog_initial_placements,
    )

    other = source.with_name("Other canonical.cbz")
    other.write_bytes(b"other comic")
    async with factory() as session:
        original = await session.scalar(select(LibraryFile))
        session.add(
            LibraryFile(
                issue_id=original.issue_id,
                library_root_id=original.library_root_id,
                file_path=str(other),
                file_name=other.name,
                file_size=len(other.read_bytes()),
                file_modified_at=datetime.now(UTC),
                file_format=FileFormat.CBZ,
            )
        )
        await session.flush()
        arc = await session.get(StoryArc, arc_id)
        await initialize_catalog_placements(session, arc)
        assert arc.diagnostics["catalog_initial_placements"]["total"] == 1
        await session.commit()
    result = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert result.completed == 1
    copied = list(destination.rglob("*.cbz"))
    assert len(copied) == 1 and copied[0].read_bytes() == source.read_bytes()


async def test_initial_copy_rejects_changed_lowest_file_selection(db_session, tmp_path):
    arc_id, source, destination, factory = await _create(db_session, tmp_path)
    from pullbox.services.story_arc_catalog_placement import run_catalog_initial_placements

    source.with_name("Other.cbz").write_bytes(b"different canonical comic")
    async with factory() as session:
        original = await session.scalar(select(LibraryFile))
        session.add(
            LibraryFile(
                id=0,
                issue_id=original.issue_id,
                library_root_id=original.library_root_id,
                file_path=str(source.with_name("Other.cbz")),
                file_name="Other.cbz",
                file_size=4,
                file_modified_at=datetime.now(UTC),
                file_format=FileFormat.CBZ,
            )
        )
        await session.commit()
    result = await run_catalog_initial_placements(arc_id, session_factory=factory)
    assert result.failed == 1 and result.completed == 0
    assert not list(destination.iterdir())
