"""Manual native Follow-up uses reviewed local issues, never invented CV IDs."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.schemas.import_job import ImportJobCreate, RecoverOrphanRequest
from pullbox.services.import_native_issue_choices import native_issue_choice
from pullbox.services.import_review_scope import sign_review_scope
from tests.integration.test_compiled_sidecar_import import _comic, _sidecar
from tests.integration.test_metron_folder_import import _service, _setup


async def _follow_up(session, root, namespace, *, identity_base=0, admit=True):
    destination, series, issue = await _setup(
        session,
        root,
        number="13a",
        namespace=namespace,
        series_external_id=str(identity_base + 21),
        issue_external_id=str(identity_base + 101),
        default_destination=False,
    )
    folder = root / "source" / "Unsorted"
    _sidecar(folder, namespace, str(identity_base + 21))
    comic = _comic(folder, namespace, tagged=False)
    service = _service()
    request = ImportJobCreate(
        source_path=str(folder.parent),
        source_type="filesystem",
        target_library_root_id=destination.id,
    )
    if admit:
        job = await service.create_job(session, request)
    else:
        from pullbox.core.library_policy import load_library_ingest_policy
        from pullbox.models.import_job import ImportJob
        from pullbox.services.import_policy_snapshot import apply_ingest_policy_to_import_job

        job = ImportJob(
            source_path=request.source_path,
            source_type=request.source_type,
            target_library_root_id=destination.id,
        )
        apply_ingest_policy_to_import_job(job, await load_library_ingest_policy(session))
        session.add(job)
        await session.commit()
    await service.start_scan(session, job.id)
    file = (
        await session.scalars(select(ImportedFile).where(ImportedFile.import_job_id == job.id))
    ).one()
    parent = await session.get(ImportedSeries, file.import_series_id)
    assert parent.series_id == series.id and file.matched_issue_id is None
    job.status = ImportJobStatus.COMPLETED
    job.import_completed_at = datetime.now(UTC)
    parent.status = ImportSeriesStatus.NO_MATCH
    await session.commit()
    service._metadata_service.reset_mock()
    return service, job, parent, file, series, issue, comic


@pytest.mark.parametrize("namespace", [IdentityNamespace.METRON, IdentityNamespace.GCD])
async def test_native_follow_up_exposes_local_issue_choices_without_comicvine(
    db_session, tmp_path, namespace
):
    service, _, parent, file, _, issue, comic = await _follow_up(db_session, tmp_path, namespace)
    before = (deepcopy(file.diagnostics), comic.read_bytes())
    context = await service.get_orphan_recovery_context(db_session, parent.id)
    assert context.get("native_recovery") is True
    assert context["files_remaining"] == 1
    assert context["issue_options"] == []  # Native choices are paginated per file.
    assert issue.comicvine_id is None
    assert (file.diagnostics, comic.read_bytes()) == before
    assert service._metadata_service.mock_calls == []


async def _request(session, job, parent, file, series, issue, actor=42):
    choice = await native_issue_choice(session, job, parent, file, series, issue, actor)
    assert choice.eligible
    return RecoverOrphanRequest.model_validate(
        {
            "decisions": [
                {
                    "imported_file_id": file.id,
                    "action": "assign",
                    "library_issue_id": issue.id,
                    "review_token": await sign_review_scope(session, choice.scope),
                }
            ]
        }
    )


@pytest.mark.parametrize("namespace", [IdentityNamespace.METRON, IdentityNamespace.GCD])
async def test_reviewed_native_follow_up_imports_and_rolls_back(db_session, tmp_path, namespace):
    service, job, parent, file, series, issue, comic = await _follow_up(
        db_session, tmp_path, namespace
    )
    original, evidence = comic.read_bytes(), deepcopy(file.diagnostics["source_metadata"])
    request = await _request(db_session, job, parent, file, series, issue)
    result = await service.recover_orphan(db_session, parent.id, request, actor_id=42)
    await db_session.commit()
    await db_session.refresh(file)
    assert result["imported_count"] == 1 and result["files_remaining"] == 0
    assert file.status == ImportedFileStatus.IMPORTED and file.match_method == "library_review"
    assert file.diagnostics["source_metadata"] == evidence
    assert (await db_session.scalars(select(LibraryFile))).one().issue_id == issue.id
    assert comic.read_bytes() == original
    assert await service.rollback_import(db_session, job.id)
    assert not (await db_session.scalars(select(LibraryFile))).all()
    assert comic.read_bytes() == original
    assert service._metadata_service.mock_calls == []


@pytest.mark.parametrize(
    "change", ["actor", "token", "issue_link", "file_choice", "locked", "duplicate_decision"]
)
async def test_native_follow_up_rejects_stale_or_inapplicable_choices(db_session, tmp_path, change):
    service, job, parent, file, series, issue, comic = await _follow_up(
        db_session, tmp_path, IdentityNamespace.METRON
    )
    request = await _request(db_session, job, parent, file, series, issue)
    if change == "token":
        request.decisions[0].review_token = "invalid"
    elif change == "issue_link":
        (
            await db_session.scalars(select(IssueExternalIdentity))
        ).one().verification_state = IdentityVerificationState.STALE
    elif change == "file_choice":
        file.include_in_import = True
    elif change == "locked":
        file.status = ImportedFileStatus.SKIPPED
    elif change == "duplicate_decision":
        request.decisions.append(request.decisions[0].model_copy())
    await db_session.commit()
    before = (file.status, deepcopy(file.diagnostics), comic.read_bytes())
    with pytest.raises(ValidationError):
        await service.recover_orphan(
            db_session, parent.id, request, actor_id=43 if change == "actor" else 42
        )
    assert not (await db_session.scalars(select(LibraryFile))).all()
    assert (file.status, file.diagnostics, comic.read_bytes()) == before


async def test_native_follow_up_can_skip_all_without_assigning_a_file(db_session, tmp_path):
    service, _, parent, file, _, _, comic = await _follow_up(
        db_session, tmp_path, IdentityNamespace.GCD
    )
    original = comic.read_bytes()
    result = await service.recover_orphan(
        db_session,
        parent.id,
        RecoverOrphanRequest.model_validate(
            {"decisions": [{"imported_file_id": file.id, "action": "skip"}]}
        ),
        actor_id=42,
    )
    assert result["files_remaining"] == 0 and result["skipped_count"] == 1
    assert parent.status == ImportSeriesStatus.SKIPPED
    assert comic.read_bytes() == original
    assert not (await db_session.scalars(select(LibraryFile))).all()


async def test_native_follow_up_preserves_an_owned_copy(db_session, tmp_path):
    from pullbox.models.library import LibraryRoot
    from tests.unit.test_import_deferred_recovery import register

    service, job, parent, file, series, issue, comic = await _follow_up(
        db_session, tmp_path, IdentityNamespace.METRON
    )
    request = await _request(db_session, job, parent, file, series, issue)
    root = await db_session.get(LibraryRoot, job.target_library_root_id)
    library = await register(db_session, file, issue, root)
    library.file_path = str(tmp_path / "owned.cbz")
    await db_session.commit()
    original = comic.read_bytes()
    with pytest.raises(ValidationError, match="already owned"):
        await service.recover_orphan(db_session, parent.id, request, actor_id=42)
    assert (await db_session.scalars(select(LibraryFile))).one() is library
    assert library.file_path == str(tmp_path / "owned.cbz")
    assert file.status == ImportedFileStatus.NO_MATCH
    assert comic.read_bytes() == original


async def test_native_follow_up_refuses_changed_source_bytes(db_session, tmp_path):
    service, job, parent, file, series, issue, comic = await _follow_up(
        db_session, tmp_path, IdentityNamespace.GCD
    )
    request = await _request(db_session, job, parent, file, series, issue)
    comic.write_bytes(comic.read_bytes() + b"changed after preview")
    original = comic.read_bytes()
    result = await service.recover_orphan(db_session, parent.id, request, actor_id=42)
    assert result["failed_count"] == 1 and result["imported_count"] == 0
    assert not (await db_session.scalars(select(LibraryFile))).all()
    assert comic.read_bytes() == original


async def test_native_follow_up_does_not_pick_between_competing_copies(db_session, tmp_path):
    from tests.unit.test_import_deferred_recovery import add_file

    service, job, parent, file, series, issue, comic = await _follow_up(
        db_session, tmp_path, IdentityNamespace.METRON
    )
    other = await add_file(
        db_session,
        job,
        parent,
        file_path=str(tmp_path / "alternate.cbz"),
        comicvine_issue_id=None,
        diagnostics=deepcopy(file.diagnostics),
        source_signature=deepcopy(file.source_signature),
    )
    await db_session.commit()
    request = await _request(db_session, job, parent, file, series, issue)
    alternate = await _request(db_session, job, parent, other, series, issue)
    request.decisions.extend(alternate.decisions)
    original = comic.read_bytes()
    result = await service.recover_orphan(db_session, parent.id, request, actor_id=42)
    assert result["imported_count"] == 0 and result["files_remaining"] == 2
    assert file.status == other.status == ImportedFileStatus.CONFLICT
    assert not (await db_session.scalars(select(LibraryFile))).all()
    assert comic.read_bytes() == original


@pytest.mark.parametrize(
    "resolved", [ImportedFileStatus.ALREADY_OWNED, ImportedFileStatus.DUPLICATE_FILE]
)
async def test_native_follow_up_does_not_reopen_resolved_neighbors(db_session, tmp_path, resolved):
    from tests.unit.test_import_deferred_recovery import add_file

    service, job, parent, file, series, issue, _comic = await _follow_up(
        db_session, tmp_path, IdentityNamespace.METRON
    )
    neighbor = await add_file(db_session, job, parent, file_path=str(tmp_path / "resolved.cbz"))
    neighbor.status = resolved
    await db_session.commit()
    request = await _request(db_session, job, parent, file, series, issue)
    result = await service.recover_orphan(db_session, parent.id, request, actor_id=42)
    assert result["imported_count"] == 1
    assert result["files_remaining"] == 0
    assert parent.status == ImportSeriesStatus.IMPORTED
    assert neighbor.status == resolved
