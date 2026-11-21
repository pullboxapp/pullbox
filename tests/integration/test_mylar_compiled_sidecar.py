"""Mylar extras can use compiled series evidence without inheriting issue identity."""

import json
from pathlib import Path
from unittest.mock import call

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.schemas.import_job import ConfirmImportRequest
from pullbox.services.import_native_issue_choices import (
    assign_native_issue,
    native_issue_choice,
    native_review_context,
)
from pullbox.services.import_review_scope import sign_review_scope
from tests.integration.test_compiled_sidecar_import import _comic, _sidecar
from tests.integration.test_mylar_native_extras import seed_mylar_native_extra


async def sidecar_mylar_fixture(
    session,
    root,
    namespace="metron",
    in_place=False,
    tagged=True,
    same_name=False,
    *,
    identity_base=0,
    admit=True,
    issue_namespace=None,
):
    values = await seed_mylar_native_extra(
        session,
        root,
        namespace,
        in_place,
        same_name=same_name,
        identity_base=identity_base,
        admit=admit,
    )
    *_, originals, extra = values
    folder = extra.parent
    _comic(
        folder,
        IdentityNamespace(issue_namespace or namespace),
        tagged=tagged,
        number="3",
        identifier=str(identity_base + 101),
    ).replace(extra)
    _sidecar(folder, IdentityNamespace(namespace), str(identity_base + 21))
    if issue_namespace is not None:
        issue = values[3]
        link = await session.scalar(
            select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == issue.id)
        )
        if issue_namespace == "comicvine":
            issue.comicvine_id = identity_base + 101
            await session.delete(link)
        else:
            link.identity_namespace = IdentityNamespace(issue_namespace)
        await session.commit()
    originals[extra] = extra.read_bytes()
    originals[folder / "series.json"] = (folder / "series.json").read_bytes()
    return values


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("issue_namespace", ["metron", "gcd", "comicvine"])
@pytest.mark.parametrize("in_place", [False, True])
async def test_mylar_sidecar_and_exact_issue_import_without_changing_recorded_ownership(
    db_session, tmp_path, namespace, issue_namespace, in_place
):
    (
        job,
        service,
        native,
        issue,
        legacy,
        tracked_issue,
        originals,
        extra,
    ) = await sidecar_mylar_fixture(
        db_session, tmp_path, namespace, in_place, issue_namespace=issue_namespace
    )
    await service.start_scan(db_session, job.id)
    files = {
        Path(row.file_path): row for row in (await db_session.scalars(select(ImportedFile))).all()
    }
    file = files[extra]
    assert file.status == ImportedFileStatus.MATCHED
    assert file.matched_issue_id == issue.id
    parent = await db_session.get(ImportedSeries, file.import_series_id)
    assert parent.series_id == native.id
    assert parent.cv_match_method == f"{namespace}_id"
    assert file.diagnostics["source_metadata"]["metron_import"]["series_sidecar"]["source"] == (
        "series.json"
    )
    tracked = files[extra.parent / "Old Harbor 001.cbz"]
    assert tracked.matched_issue_id == tracked_issue.id
    recorded = await db_session.get(ImportedSeries, tracked.import_series_id)
    assert recorded.series_id == legacy.id and recorded.cv_id == 900
    unknown = files[extra.parent / "Unidentified 099.cbz"]
    assert unknown.status == ImportedFileStatus.NO_MATCH and unknown.matched_issue_id is None
    assert not unknown.include_in_import
    assert job.series_found == 2 and job.scan_total_files == 3
    for row in (file, tracked):
        await service.update_file_selection(db_session, job.id, row.id, include_in_import=True)
    await service.confirm_import(
        db_session, job.id, ConfirmImportRequest(series_ids=[parent.id, recorded.id])
    )
    await service.run_import(db_session, job.id)
    assert {row.issue_id for row in (await db_session.scalars(select(LibraryFile))).all()} == {
        issue.id,
        tracked_issue.id,
    }
    assert await service.rollback_import(db_session, job.id)
    assert await db_session.scalar(select(LibraryFile.id)) is None
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert all(
        item == call._provider.cache_metrics() for item in service._metadata_service.mock_calls
    )


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("same_name", [False, True])
async def test_series_only_mylar_sidecar_requires_explicit_eligible_issue_choice(
    db_session, tmp_path, namespace, same_name
):
    job, service, _native, issue, _legacy, _tracked, originals, extra = await sidecar_mylar_fixture(
        db_session, tmp_path, namespace, tagged=False, same_name=same_name
    )
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(select(ImportedFile).where(ImportedFile.file_path == str(extra)))
    assert file.status == ImportedFileStatus.NO_MATCH and not file.include_in_import
    job, parent, file, series, _ = await native_review_context(db_session, job.id, file.id)
    choice = await native_issue_choice(db_session, job, parent, file, series, issue, actor_id=1)
    assert choice.eligible and choice.scope["selection"] is not None
    assert file.matched_issue_id is None
    await assign_native_issue(
        db_session,
        job.id,
        file.id,
        issue.id,
        await sign_review_scope(db_session, choice.scope),
        actor_id=1,
    )
    await service.confirm_import(db_session, job.id, ConfirmImportRequest(series_ids=[parent.id]))
    await service.run_import(db_session, job.id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.IMPORTED
    assert (await db_session.scalars(select(LibraryFile))).one().issue_id == issue.id
    assert await service.rollback_import(db_session, job.id)
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert all(
        item == call._provider.cache_metrics() for item in service._metadata_service.mock_calls
    )


@pytest.mark.parametrize("defect", ["version", "native_conflict", "legacy_conflict"])
async def test_mylar_compiled_sidecar_conflicts_never_become_ready(db_session, tmp_path, defect):
    (
        job,
        service,
        _native,
        _issue,
        _legacy,
        _tracked,
        originals,
        extra,
    ) = await sidecar_mylar_fixture(db_session, tmp_path)
    path = extra.parent / "series.json"
    payload = json.loads(path.read_bytes())
    if defect == "version":
        payload["pullbox"]["schema_version"] = 2
    elif defect == "native_conflict":
        payload["pullbox"]["snapshot"]["identities"].append(
            {"namespace": "gcd", "entity_kind": "series", "external_id": "999"}
        )
    else:
        payload["comicid"] = 999
    path.write_text(json.dumps(payload))
    originals[path] = path.read_bytes()
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(select(ImportedFile).where(ImportedFile.file_path == str(extra)))
    assert file.matched_issue_id is None and not file.include_in_import
    assert file.status in {ImportedFileStatus.NO_MATCH, ImportedFileStatus.CONFLICT}
    assert "series_sidecar" in file.diagnostics["source_metadata"]
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert all(
        item == call._provider.cache_metrics() for item in service._metadata_service.mock_calls
    )


async def test_mylar_folder_owner_does_not_override_agreeing_sidecar_cross_identity(
    db_session, tmp_path
):
    job, service, native, issue, _legacy, _tracked, originals, extra = await sidecar_mylar_fixture(
        db_session, tmp_path
    )
    native.comicvine_id = 222
    await db_session.commit()
    path = extra.parent / "series.json"
    payload = json.loads(path.read_bytes())
    payload["comicid"] = 222
    payload["pullbox"]["snapshot"]["identities"].append(
        {"namespace": "comicvine", "entity_kind": "series", "external_id": "222"}
    )
    path.write_text(json.dumps(payload))
    originals[path] = path.read_bytes()
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(select(ImportedFile).where(ImportedFile.file_path == str(extra)))
    assert file.status == ImportedFileStatus.MATCHED and file.matched_issue_id == issue.id
    assert file.diagnostics["source_metadata"]["identity_conflicts"] == [
        {"field": "comicvine_series_id", "mylar3": 900, "sidecar": 222}
    ]
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert all(
        item == call._provider.cache_metrics() for item in service._metadata_service.mock_calls
    )
