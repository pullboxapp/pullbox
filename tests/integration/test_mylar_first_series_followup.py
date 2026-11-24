"""A new Mylar series can keep its good files without accepting disputed ownership."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from pullbox.core.events import EventBus
from pullbox.core.exceptions import ValidationError
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.models.series import Series
from pullbox.schemas.import_job import (
    ConfirmImportRequest,
    ImportJobCreate,
    OrphanRecoveryDecision,
    RecoverOrphanRequest,
)
from pullbox.services.import_service import ImportService
from pullbox.services.metadata_service import MetadataService
from pullbox.services.series_service import SeriesService
from scripts.mylar3_import_fixture import create_minimal_cbz, create_mylar3_db
from tests.integration.test_metron_folder_import import _archive, _setup


async def import_first_mylar_series(
    session, root, namespace, in_place, layout, *, base=0, admit=True, title="Old Harbor"
):
    """Change a verified library identity between Mylar review and actual placement."""
    destination, _, foreign = await _setup(
        session,
        root,
        namespace=IdentityNamespace(namespace),
        issue_external_id=str(base + 101),
        series_external_id=str(base + 21),
        default_destination=False,
    )
    folder = root / "source" / f"{title} (2020)"
    disputed = folder / f"{title} 001.cbz"
    _archive(
        disputed, primary=namespace, series_id=str(base + 22), issue_id=str(base + 102), number="1"
    )
    issues = [
        {
            "ComicID": str(base + 900),
            "IssueID": str(base + 901),
            "Issue_Number": "1",
            "Location": disputed.name,
        }
    ]
    if layout != "only":
        create_minimal_cbz(folder / "Unknown.cbz")
        neighbor = folder / f"{title} 002.cbz"
        if layout == "unmatched":
            _archive(
                neighbor,
                primary=namespace,
                series_id=str(base + 22),
                issue_id=str(base + 102),
                number="2",
            )
        else:
            create_minimal_cbz(neighbor)
        issues.append(
            {
                "ComicID": str(base + 900),
                "IssueID": str(base + 902),
                "Issue_Number": "2",
                "Location": neighbor.name,
            }
        )
    if in_place:
        session.add(
            LibraryRoot(
                name="Mylar source",
                path=str(folder.parent),
                enabled=True,
                allow_managed_writes=False,
                allow_referenced_registrations=True,
            )
        )
    await session.commit()
    db = root / "mylar.db"
    create_mylar3_db(
        db,
        series=[
            {
                "ComicID": str(base + 900),
                "ComicName": title,
                "ComicYear": "2020",
                "ComicLocation": str(folder),
                "Total": len(issues),
            }
        ],
        issues=issues,
    )
    originals = {p: p.read_bytes() for p in [db, *folder.glob("*.cbz")]}
    provider = MagicMock()
    provider.get_series_cached = AsyncMock(return_value=None)
    provider.search_series = AsyncMock(side_effect=AssertionError("Unexpected search"))
    provider.get_series = provider.get_issues_for_series = AsyncMock(
        side_effect=AssertionError("Unexpected catalog request")
    )
    metadata = MetadataService(provider, root)
    events = EventBus()
    service = ImportService(
        series_service=SeriesService(metadata_service=metadata, event_bus=events),
        metadata_service=metadata,
        event_bus=events,
    )
    request = ImportJobCreate(
        source_type="mylar3",
        source_path=str(db),
        mylar3_path_map_confirmed=True,
        file_handling_mode="in_place" if in_place else "managed_copy",
        target_library_root_id=None if in_place else destination.id,
    )
    if admit:
        job = await service.create_job(session, request)
    else:
        from pullbox.core.library_policy import load_library_ingest_policy
        from pullbox.models.import_job import ImportJob
        from pullbox.services.import_policy_snapshot import apply_ingest_policy_to_import_job

        job = ImportJob(**request.model_dump(exclude_unset=True))
        apply_ingest_policy_to_import_job(job, await load_library_ingest_policy(session))
        session.add(job)
        await session.commit()
    await service.start_scan(session, job.id)
    group = await session.scalar(
        select(ImportedSeries).where(
            ImportedSeries.import_job_id == job.id, ImportedSeries.cv_id == base + 900
        )
    )
    assert group.status == ImportSeriesStatus.MATCHED and group.series_id is None
    files = {
        r.file_name: r
        for r in (
            await session.scalars(
                select(ImportedFile).where(ImportedFile.import_series_id == group.id)
            )
        ).all()
    }
    for record in issues:
        row = files[record["Location"]]
        assert row.status == ImportedFileStatus.MATCHED
        await service.update_file_selection(session, job.id, row.id, include_in_import=True)
    await service.confirm_import(session, job.id, ConfirmImportRequest(series_ids=[group.id]))
    link = await session.scalar(
        select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == foreign.id)
    )
    link.external_id = str(base + 102)
    await session.commit()
    # Catalog hydration is a separate background job, not part of placement.
    with patch("pullbox.services.import_job_execution._schedule_catalog_hydration"):
        await service.run_import(session, job.id)
    await session.commit()
    await session.refresh(group)
    await session.refresh(files[disputed.name])
    provider.search_series.assert_not_awaited()
    provider.get_series.assert_not_awaited()
    return service, job, group, files, foreign, originals


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("in_place", [False, True])
@pytest.mark.parametrize("layout", ["only", "mixed", "unmatched"])
async def test_first_series_placement_conflict(db_session, tmp_path, namespace, in_place, layout):
    s = db_session
    service, job, group, files, foreign, originals = await import_first_mylar_series(
        s, tmp_path, namespace, in_place, layout
    )
    file = files["Old Harbor 001.cbz"]
    assert file.status == ImportedFileStatus.NO_MATCH, file.error_message
    assert not file.include_in_import and file.matched_issue_id is None
    assert group.status == ImportSeriesStatus.IMPORTED and group.series_id is not None
    assert job.total_files_failed == job.series_failed == 0
    recorded = await s.scalar(
        select(Issue).where(Issue.series_id == group.series_id, Issue.comicvine_id == 901)
    )
    assert recorded is not None
    artifacts = (await s.scalars(select(LibraryFile))).all()
    assert len(artifacts) == int(layout == "mixed")
    if layout == "mixed":
        assert (await s.get(Issue, artifacts[0].issue_id)).comicvine_id == 902
    protected = {Path(r.file_path): Path(r.file_path).read_bytes() for r in artifacts}
    context = await service.get_orphan_recovery_context(s, group.id)
    assert context["mylar_conflicts_only"]
    assert len(context["files"]) == (2 if layout == "unmatched" else 1)
    row = next(r for r in context["files"] if r["file_name"] == file.file_name)
    assert row["recorded_issue_id"] == recorded.id
    assert row["provider_issues"] == [{"provider": namespace, "library_issue_id": foreign.id}]

    def decision(file_id, action="skip"):
        return RecoverOrphanRequest(
            decisions=[
                OrphanRecoveryDecision(
                    imported_file_id=file_id,
                    action=action,
                    issue_cv_id=901 if action == "assign" else None,
                )
            ]
        )

    with pytest.raises(ValidationError):
        await service.recover_orphan(s, group.id, decision(file.id, "assign"))
    with pytest.raises(ValidationError, match="already identified"):
        await service.assign_cv_to_orphan(s, group.id, 999)
    with pytest.raises(ValidationError, match="skip only"):
        await service.dismiss_orphan(s, group.id)
    if layout != "only":
        with pytest.raises(ValidationError):
            await service.recover_orphan(s, group.id, decision(files["Unknown.cbz"].id))
    result = await service.recover_orphan(s, group.id, decision(file.id))
    if layout == "unmatched":
        assert result["files_remaining"] == 2
        result = await service.recover_orphan(s, group.id, decision(files["Old Harbor 002.cbz"].id))
    await s.commit()
    assert result["files_remaining"] == int(layout != "only")
    visible, _ = await service.get_orphaned_series(s, job_id=job.id)
    assert (group.id in {i.id for i in visible}) == (layout != "only")
    if layout != "only":
        ordinary = await service.get_orphan_recovery_context(s, group.id)
        assert not ordinary.get("mylar_conflicts_only")
        assert [r["file_name"] for r in ordinary["files"] if not r["decision_locked"]] == [
            "Unknown.cbz"
        ]
        with pytest.raises(ValidationError):
            await service.recover_orphan(s, group.id, decision(file.id))
        if layout == "mixed":
            with pytest.raises(ValidationError, match="no longer awaiting"):
                await service.recover_orphan(s, group.id, decision(files["Old Harbor 002.cbz"].id))
        completed = await service.recover_orphan(s, group.id, decision(files["Unknown.cbz"].id))
        assert (
            completed["status"] == ImportSeriesStatus.IMPORTED and completed["files_remaining"] == 0
        )
        await s.commit()
    assert all(p.read_bytes() == b for p, b in protected.items())
    assert await service.rollback_import(s, job.id)
    assert not await s.scalar(select(Series.id).where(Series.comicvine_id == 900))
    assert await s.get(Issue, foreign.id) is not None
    assert all(p.read_bytes() == b for p, b in originals.items())
