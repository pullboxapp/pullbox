"""Provider arc adoption preserves the canonical library and user decisions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import IssueCatalogState, Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcResolutionState
from pullbox.models.story_arc_sync import StoryArcSyncWork
from pullbox.providers.base import IssueMetadata, SeriesMetadata
from pullbox.providers.story_arcs import StoryArcMetadata


def _issue(cv_id="11", series_id="21", number="1"):
    return IssueMetadata(
        provider_id=cv_id,
        series_provider_id=series_id,
        issue_number=float(number.rstrip("AU")),
        issue_number_text=number,
        title="Remote title",
        description="Remote description",
        release_date="2020-01-01",
        store_date=None,
        cover_url=None,
        page_count=32,
        comicvine_url=None,
    )


def _series(cv_id="21"):
    return SeriesMetadata(
        provider_id=cv_id,
        title=f"Series {cv_id}",
        sort_title=None,
        year_start=2020,
        year_end=None,
        status=None,
        publisher="DC",
        description=None,
        cover_url=None,
        issue_count=99,
        comicvine_url=None,
    )


def _provider(issues=None, *, complete=True):
    issues = issues if issues is not None else [_issue(), _issue("12", "22", "1000000")]
    metadata = StoryArcMetadata(
        provider_id="31",
        title="Remote Arc",
        description="Remote arc description",
        publisher="DC",
        cover_url=None,
        comicvine_url=None,
        issue_provider_ids=tuple(issue.provider_id for issue in issues),
        declared_issue_count=None,
        membership_complete=complete,
        order_basis="response_order",
        warnings=(),
    )
    return SimpleNamespace(
        get_story_arc=AsyncMock(return_value=metadata),
        get_story_arc_issues=AsyncMock(return_value=issues),
        get_series=AsyncMock(side_effect=lambda value: _series(value)),
    )


async def _root(session, tmp_path):
    root = LibraryRoot(name="Canonical", path=str(tmp_path), enabled=True)
    session.add(root)
    await session.flush()
    return root


def _root_policy(root_id, template):
    from pullbox.models.library import LibraryRootPolicy, LibraryRootPolicySource

    return LibraryRootPolicy(
        library_root_id=root_id,
        series_path_template=template,
        comic_file_template="{Series} {Issue}",
        annual_file_template="{Series} Annual {Issue}",
        non_standard_file_template="{Series} {Issue}",
        single_non_standard_file_template="{Series}",
        replace_illegal_characters=True,
        colon_replacement="dash",
        source=LibraryRootPolicySource.MANUAL,
    )


async def test_add_reuses_existing_canonical_state_and_targets_only_new_members(
    db_session, tmp_path
):
    from pullbox.services.story_arc_catalog import StoryArcCatalogService

    root = await _root(db_session, tmp_path)
    existing = Series(
        comicvine_id=21,
        title="User title",
        sort_title="User title",
        monitored=True,
        path="/existing/custom",
        issue_catalog_state=IssueCatalogState.COMPLETE,
        issue_count=300,
        metadata_source="user",
    )
    issue = Issue(
        comicvine_id=11,
        series=existing,
        issue_number=1,
        title="User issue",
        status=IssueStatus.OWNED,
        manual_skip=True,
    )
    db_session.add(issue)
    await db_session.flush()
    provider = _provider()
    service = StoryArcCatalogService(provider)
    preview = await service.preview("31", known_series_provider_ids={"21"})
    arc = await service.add(
        db_session,
        preview,
        ordered_issue_provider_ids=["12", "11"],
        library_root_id=root.id,
        monitored=True,
        search_missing=True,
    )
    assert existing.monitored is True
    assert (existing.title, existing.path, existing.issue_count) == (
        "User title",
        "/existing/custom",
        300,
    )
    assert existing.issue_catalog_state is IssueCatalogState.COMPLETE
    assert issue.title == "User issue" and issue.status is IssueStatus.OWNED and issue.manual_skip
    new_series = await db_session.scalar(select(Series).where(Series.comicvine_id == 22))
    assert (
        new_series.monitored is False
        and new_series.issue_catalog_state is IssueCatalogState.PARTIAL
    )
    assert new_series.library_root_id == root.id and new_series.path.startswith(str(tmp_path))
    assert not list(tmp_path.iterdir())
    members = list(
        (
            await db_session.scalars(select(IssueStoryArc).order_by(IssueStoryArc.sequence_number))
        ).all()
    )
    assert [(m.source_issue_id, m.source_ordinal) for m in members] == [("12", 2), ("11", 1)]
    assert members[0].source_issue_number_text == "1000000"
    assert arc.monitored and arc.search_missing
    provider.get_series.assert_awaited_once_with("22")
    assert await db_session.scalar(select(func.count(Issue.id))) == 2


@pytest.mark.parametrize("mode", ["partial", "hydration", "order", "conflict", "policy"])
async def test_add_fails_closed_and_leaves_no_arc_or_partial_catalog(db_session, tmp_path, mode):
    from pullbox.services.story_arc_catalog import StoryArcCatalogError, StoryArcCatalogService
    from pullbox.services.story_arc_placement_integration import StoryArcPlacementPolicyInput

    root = await _root(db_session, tmp_path)
    provider = _provider(complete=mode != "partial")
    if mode == "hydration":
        provider.get_story_arc_issues.return_value = [_issue()]
    if mode == "conflict":
        series = Series(comicvine_id=22, title="Existing", sort_title="Existing")
        db_session.add(Issue(series=series, comicvine_id=99, issue_number=1000000))
        await db_session.flush()
    original_series_count = await db_session.scalar(select(func.count(Series.id)))
    service = StoryArcCatalogService(provider)
    with pytest.raises(StoryArcCatalogError):
        preview = await service.preview("31")
        await service.add(
            db_session,
            preview,
            ordered_issue_provider_ids=["11"] if mode == "order" else ["11", "12"],
            library_root_id=root.id,
            placement_policy=StoryArcPlacementPolicyInput("move", None, None)
            if mode == "policy"
            else None,
        )
    await db_session.flush()
    assert await db_session.scalar(select(func.count(StoryArc.id))) == 0
    assert await db_session.scalar(select(func.count(Series.id))) == original_series_count


async def test_refresh_preserves_user_edits_and_appends_pending_without_removing_members(
    db_session, tmp_path
):
    from pullbox.services.story_arc_catalog import StoryArcCatalogService

    root = await _root(db_session, tmp_path)
    provider = _provider()
    service = StoryArcCatalogService(provider)
    arc = await service.add(
        db_session,
        await service.preview("31"),
        ordered_issue_provider_ids=["12", "11"],
        library_root_id=root.id,
    )
    arc.name = "User arc title"
    arc.description = "User description"
    arc.monitored = True
    arc.diagnostics = {**arc.diagnostics, "user_evidence": {"keep": True}}
    provider = _provider([_issue("13", "23", "1AU"), _issue("11")])
    service = StoryArcCatalogService(provider)
    preview = await service.preview("31")
    delta = await service.preview_refresh(db_session, arc.id, preview)
    assert delta.added_issue_provider_ids == ("13",)
    assert delta.removed_issue_provider_ids == ("12",)
    result = await service.refresh(db_session, arc.id, preview, expected_revision=arc.revision)
    members = list(
        (
            await db_session.scalars(select(IssueStoryArc).order_by(IssueStoryArc.sequence_number))
        ).all()
    )
    assert [m.source_issue_id for m in members] == ["12", "11", "13"]
    assert members[-1].resolution_state is StoryArcResolutionState.PENDING
    assert (
        members[-1].sync_eligible is False
        and members[-1].evidence["catalog_review_required"] is True
    )
    assert (arc.name, arc.description, arc.monitored) == (
        "User arc title",
        "User description",
        True,
    )
    assert arc.diagnostics["user_evidence"] == {"keep": True}
    assert result.removed_issue_provider_ids == ("12",)
    assert len(result.added_membership_ids) == 1
    again = await service.refresh(db_session, arc.id, preview, expected_revision=arc.revision)
    assert again.added_membership_ids == ()


async def test_snapshot_tampering_and_wrong_arc_refresh_are_rejected(db_session, tmp_path):
    from pullbox.services.story_arc_catalog import StoryArcCatalogError, StoryArcCatalogService

    root = await _root(db_session, tmp_path)
    service = StoryArcCatalogService(_provider())
    preview = await service.preview("31")
    with pytest.raises(StoryArcCatalogError, match="snapshot"):
        await service.add(
            db_session,
            replace(preview, issues=preview.issues[:1]),
            ordered_issue_provider_ids=["11", "12"],
            library_root_id=root.id,
        )


async def test_add_and_outbox_are_rolled_back_by_caller_without_file_mutation(db_session, tmp_path):
    from pullbox.services.story_arc_catalog import StoryArcCatalogService
    from pullbox.services.story_arc_placement_integration import StoryArcPlacementPolicyInput

    root = await _root(db_session, tmp_path)
    source = tmp_path / "User existing.cbz"
    source.write_bytes(b"existing comic bytes")
    original = source.stat()
    series = Series(comicvine_id=21, title="Existing", sort_title="Existing")
    issue = Issue(comicvine_id=11, series=series, issue_number=1, status=IssueStatus.OWNED)
    library_file = LibraryFile(
        issue=issue,
        library_root_id=root.id,
        file_path=str(source),
        file_name=source.name,
        file_format=FileFormat.CBZ,
        file_size=20,
        file_modified_at=datetime.now(UTC),
    )
    db_session.add(library_file)
    await db_session.commit()
    service = StoryArcCatalogService(_provider([_issue()]))
    await service.add(
        db_session,
        await service.preview("31"),
        ordered_issue_provider_ids=["11"],
        library_root_id=root.id,
        placement_policy=StoryArcPlacementPolicyInput(
            "copy", root.id, str(tmp_path), synchronize=True
        ),
    )
    assert await db_session.scalar(select(func.count(StoryArcSyncWork.id))) == 1
    await db_session.rollback()
    assert await db_session.scalar(select(func.count(StoryArc.id))) == 0
    assert await db_session.scalar(select(func.count(StoryArcSyncWork.id))) == 0
    assert await db_session.scalar(select(func.count(LibraryFile.id))) == 1
    assert list(tmp_path.iterdir()) == [source]
    assert source.read_bytes() == b"existing comic bytes"
    assert (source.stat().st_mtime_ns, source.stat().st_mode) == (
        original.st_mtime_ns,
        original.st_mode,
    )


async def test_refresh_reuses_a_user_added_member_and_preserves_their_order(db_session, tmp_path):
    from pullbox.services.story_arc_catalog import StoryArcCatalogService
    from pullbox.services.story_arc_service import StoryArcService

    root = await _root(db_session, tmp_path)
    service = StoryArcCatalogService(_provider([_issue()]))
    arc = await service.add(
        db_session,
        await service.preview("31"),
        ordered_issue_provider_ids=["11"],
        library_root_id=root.id,
    )
    series = await db_session.scalar(select(Series).where(Series.comicvine_id == 21))
    issue = Issue(comicvine_id=12, series=series, issue_number=2)
    db_session.add(issue)
    await db_session.flush()
    member = await StoryArcService().add_membership(
        db_session, arc.id, issue_id=issue.id, sequence_number=17
    )
    service = StoryArcCatalogService(_provider([_issue("12", "21", "2"), _issue()]))
    preview = await service.preview("31")
    result = await service.refresh(db_session, arc.id, preview, expected_revision=arc.revision)
    assert result.added_membership_ids == ()
    assert member.sequence_number == 17
    assert await db_session.scalar(select(func.count(IssueStoryArc.id))) == 2


async def test_refresh_rejects_existing_exact_identity_drift(db_session, tmp_path):
    from pullbox.services.story_arc_catalog import StoryArcCatalogError, StoryArcCatalogService

    root = await _root(db_session, tmp_path)
    service = StoryArcCatalogService(_provider([_issue()]))
    arc = await service.add(
        db_session,
        await service.preview("31"),
        ordered_issue_provider_ids=["11"],
        library_root_id=root.id,
    )
    previous_diagnostics = dict(arc.diagnostics)
    service = StoryArcCatalogService(_provider([_issue("11", "99", "3")]))
    with pytest.raises(StoryArcCatalogError, match="identity"):
        await service.refresh(
            db_session, arc.id, await service.preview("31"), expected_revision=arc.revision
        )
    await db_session.refresh(arc)
    assert arc.diagnostics == previous_diagnostics


async def test_explicit_member_skip_does_not_change_canonical_status(db_session, tmp_path):
    from pullbox.services.story_arc_catalog import StoryArcCatalogService

    root = await _root(db_session, tmp_path)
    series = Series(comicvine_id=21, title="Existing", sort_title="Existing", monitored=True)
    issue = Issue(comicvine_id=11, series=series, issue_number=1, status=IssueStatus.WANTED)
    db_session.add(issue)
    await db_session.flush()
    service = StoryArcCatalogService(_provider([_issue()]))
    arc = await service.add(
        db_session,
        await service.preview("31"),
        ordered_issue_provider_ids=["11"],
        skipped_issue_provider_ids=["11"],
        library_root_id=root.id,
        monitored=True,
        search_missing=True,
    )
    member = await db_session.scalar(
        select(IssueStoryArc).where(IssueStoryArc.story_arc_id == arc.id)
    )
    assert member.resolution_state is StoryArcResolutionState.SKIPPED
    assert not member.sync_eligible and issue.status is IssueStatus.WANTED and series.monitored


async def test_stale_refresh_and_already_added_arc_fail_without_changes(db_session, tmp_path):
    from pullbox.services.story_arc_catalog import StoryArcCatalogError, StoryArcCatalogService

    root = await _root(db_session, tmp_path)
    service = StoryArcCatalogService(_provider())
    preview = await service.preview("31")
    arc = await service.add(
        db_session, preview, ordered_issue_provider_ids=["11", "12"], library_root_id=root.id
    )
    assert await service.find_existing(db_session, ["31", "32"]) == {"31": arc.id}
    with pytest.raises(StoryArcCatalogError) as error:
        await service.add(
            db_session, preview, ordered_issue_provider_ids=["11", "12"], library_root_id=root.id
        )
    assert error.value.code == "already_added"
    with pytest.raises(StoryArcCatalogError) as error:
        await service.refresh(db_session, arc.id, preview, expected_revision=arc.revision - 1)
    assert error.value.code == "revision_conflict"
    assert await db_session.scalar(select(func.count(StoryArc.id))) == 1


@pytest.mark.parametrize(
    "case", ["members", "parents", "duplicate", "wrong_parent", "provider_failure"]
)
async def test_preview_fails_closed_before_persistence(case, monkeypatch):
    from pullbox.services.story_arc_catalog import StoryArcCatalogError, StoryArcCatalogService

    provider = _provider()
    if case == "members":
        monkeypatch.setattr("pullbox.services.story_arc_catalog.MAX_CATALOG_MEMBERS", 1)
    elif case == "parents":
        monkeypatch.setattr("pullbox.services.story_arc_catalog.MAX_CATALOG_PARENTS", 1)
    elif case == "duplicate":
        provider.get_story_arc.return_value = replace(
            provider.get_story_arc.return_value, issue_provider_ids=("11", "11")
        )
    elif case == "wrong_parent":
        provider.get_series.side_effect = lambda _: _series("999")
    else:
        provider.get_story_arc_issues.side_effect = RuntimeError("Provider unavailable")
    with pytest.raises(StoryArcCatalogError if case != "provider_failure" else RuntimeError):
        await StoryArcCatalogService(provider).preview("31")


async def test_existing_external_arc_identity_is_already_added(db_session):
    from pullbox.models.story_arc import StoryArcExternalIdentity
    from pullbox.services.story_arc_catalog import StoryArcCatalogService
    from pullbox.services.story_arc_service import StoryArcService

    arc = await StoryArcService().create(db_session, name="Imported arc")
    db_session.add(
        StoryArcExternalIdentity(
            story_arc_id=arc.id, source="comicvine", namespace="story_arc", external_id="31"
        )
    )
    await db_session.flush()
    assert await StoryArcCatalogService(_provider()).find_existing(db_session, ["31"]) == {
        "31": arc.id
    }


async def test_canonical_root_naming_and_arc_placement_root_remain_independent(
    db_session, tmp_path
):
    from pullbox.services.story_arc_catalog import StoryArcCatalogService
    from pullbox.services.story_arc_placement_integration import StoryArcPlacementPolicyInput

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    placement = tmp_path / "arcs"
    placement.mkdir()
    root = await _root(db_session, canonical)
    placement_root = LibraryRoot(name="Arcs", path=str(placement), enabled=True)
    db_session.add(placement_root)
    db_session.add(_root_policy(root.id, "{Publisher}/{Series} ({Year})"))
    await db_session.flush()
    service = StoryArcCatalogService(_provider([_issue()]))
    arc = await service.add(
        db_session,
        await service.preview("31"),
        ordered_issue_provider_ids=["11"],
        library_root_id=root.id,
        placement_policy=StoryArcPlacementPolicyInput("copy", placement_root.id, str(placement)),
    )
    series = await db_session.scalar(select(Series).where(Series.comicvine_id == 21))
    assert series.path == str(canonical / "DC" / "Series 21 (2020)")
    assert series.library_root_id == root.id and arc.target_library_root_id == placement_root.id
    assert not list(canonical.iterdir()) and not list(placement.iterdir())


async def test_canonical_parent_symlink_escape_fails_without_partial_rows(db_session, tmp_path):
    from pullbox.services.story_arc_catalog import StoryArcCatalogError, StoryArcCatalogService

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (canonical / "DC").symlink_to(outside, target_is_directory=True)
    root = await _root(db_session, canonical)
    db_session.add(_root_policy(root.id, "{Publisher}/{Series}"))
    await db_session.flush()
    service = StoryArcCatalogService(_provider([_issue()]))
    with pytest.raises(StoryArcCatalogError) as error:
        await service.add(
            db_session,
            await service.preview("31"),
            ordered_issue_provider_ids=["11"],
            library_root_id=root.id,
        )
    assert error.value.code == "canonical_path_unsafe"
    assert await db_session.scalar(select(func.count(Series.id))) == 0
    assert await db_session.scalar(select(func.count(StoryArc.id))) == 0
    assert not list(outside.iterdir())


async def test_refresh_rollback_keeps_old_membership_and_diagnostics(db_session, tmp_path):
    from pullbox.services.story_arc_catalog import StoryArcCatalogService

    root = await _root(db_session, tmp_path)
    service = StoryArcCatalogService(_provider([_issue()]))
    arc = await service.add(
        db_session,
        await service.preview("31"),
        ordered_issue_provider_ids=["11"],
        library_root_id=root.id,
    )
    arc_id = arc.id
    original = dict(arc.diagnostics)
    await db_session.commit()
    service = StoryArcCatalogService(_provider())
    await service.refresh(
        db_session, arc.id, await service.preview("31"), expected_revision=arc.revision
    )
    await db_session.rollback()
    arc = await db_session.get(StoryArc, arc_id)
    assert arc.diagnostics == original
    assert await db_session.scalar(select(func.count(IssueStoryArc.id))) == 1
    assert await db_session.scalar(select(func.count(Issue.id))) == 1


@pytest.mark.parametrize(
    "parent_title,parent_type,issue_title,expected_issue_type",
    [
        ("Batman Annual", "annual", "An ordinary title", "annual"),
        ("Batman Omnibus", "omnibus", "An ordinary title", "omnibus"),
        ("Batman One-Shot", "one_shot", "An ordinary title", "one_shot"),
        ("Batman", "standard", "Batman Annual #1", "annual"),
    ],
)
async def test_new_canonical_types_use_existing_metadata_classifiers(
    db_session, tmp_path, parent_title, parent_type, issue_title, expected_issue_type
):
    from pullbox.services.story_arc_catalog import StoryArcCatalogService

    root = await _root(db_session, tmp_path)
    provider = _provider([replace(_issue(), title=issue_title)])
    provider.get_series.side_effect = lambda value: replace(_series(value), title=parent_title)
    service = StoryArcCatalogService(provider)
    await service.add(
        db_session,
        await service.preview("31"),
        ordered_issue_provider_ids=["11"],
        library_root_id=root.id,
    )
    issue = await db_session.scalar(select(Issue))
    parent = await db_session.scalar(select(Series))
    assert parent.series_type.value == parent_type
    assert issue.issue_type.value == expected_issue_type


async def test_targeted_new_issue_inherits_existing_parent_type_without_reclassifying_user_rows(
    db_session, tmp_path
):
    from pullbox.models.issue import IssueType
    from pullbox.models.series import SeriesType
    from pullbox.services.story_arc_catalog import StoryArcCatalogService

    root = await _root(db_session, tmp_path)
    parent = Series(
        comicvine_id=21,
        title="User annual series",
        sort_title="User annual series",
        series_type=SeriesType.ANNUAL,
    )
    existing = Issue(comicvine_id=11, series=parent, issue_number=1, issue_type=IssueType.SPECIAL)
    db_session.add(existing)
    await db_session.flush()
    service = StoryArcCatalogService(_provider([_issue(), _issue("12", "21", "2")]))
    await service.add(
        db_session,
        await service.preview("31"),
        ordered_issue_provider_ids=["11", "12"],
        library_root_id=root.id,
    )
    new = await db_session.scalar(select(Issue).where(Issue.comicvine_id == 12))
    assert new.issue_type is IssueType.ANNUAL
    assert existing.issue_type is IssueType.SPECIAL and parent.series_type is SeriesType.ANNUAL


async def test_imported_provider_arc_refresh_requires_explicit_new_parent_root(
    db_session, tmp_path
):
    from pullbox.models.story_arc import StoryArcSourceKind
    from pullbox.services.story_arc_catalog import StoryArcCatalogError, StoryArcCatalogService
    from pullbox.services.story_arc_service import StoryArcService

    root = await _root(db_session, tmp_path)
    parent = Series(
        comicvine_id=21,
        title="Mylar title",
        sort_title="Mylar title",
        path="/existing/mylar",
        monitored=True,
    )
    issue = Issue(comicvine_id=11, series=parent, issue_number=1)
    db_session.add(issue)
    await db_session.flush()
    arc = await StoryArcService().create(
        db_session, name="Mylar arc", source_kind=StoryArcSourceKind.MYLAR3
    )
    arc.comicvine_id = 31
    arc.diagnostics = {"import_evidence": True}
    original_member = await StoryArcService().add_membership(
        db_session,
        arc.id,
        issue_id=issue.id,
        sequence_number=19,
        source_kind=StoryArcSourceKind.MYLAR3,
    )
    service = StoryArcCatalogService(_provider())
    preview = await service.preview("31")
    with pytest.raises(StoryArcCatalogError) as error:
        await service.refresh(db_session, arc.id, preview, expected_revision=arc.revision)
    assert error.value.code == "canonical_root_required"
    result = await service.refresh(
        db_session, arc.id, preview, expected_revision=arc.revision, library_root_id=root.id
    )
    assert len(result.added_membership_ids) == 1
    assert arc.diagnostics["provider_catalog"]["canonical_library_root_id"] == root.id
    assert arc.diagnostics["import_evidence"] is True and arc.name == "Mylar arc"
    assert original_member.sequence_number == 19
    assert parent.path == "/existing/mylar" and parent.monitored
    appended = await db_session.get(IssueStoryArc, result.added_membership_ids[0])
    assert (
        appended.sequence_number == 20
        and appended.resolution_state is StoryArcResolutionState.PENDING
    )
