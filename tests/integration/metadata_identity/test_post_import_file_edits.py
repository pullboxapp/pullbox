"""Completed import history permits edits; rollback preserves their library owners."""

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import pytest

from pullbox.models import Issue, LibraryFile, Series
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
)
from pullbox.models.library import FileFormat
from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError
from pullbox.services.archive_metadata_finalization import finalize_archive_publication
from pullbox.services.archive_metadata_publication import inspect_archive_publication
from pullbox.services.import_job_actions import rollback_action
from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata
from pullbox.services.library_convert_service import convert_library_file
from tests.integration.metadata_identity.test_import_archive_publication import (
    finish,
    owned,
    publish,
    rollback,
)


async def noop(*args):
    pass


async def test_native_conversion_preserves_imported_owner_and_converted_path(
    identity_probe_db, tmp_path
):
    from pullbox.core.library_file_ownership import build_managed_placement_signature
    from tests.unit.test_nonzip_metadata_writing import nonzip_archive

    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, source, plan, ids, _):
        native = path.with_suffix(".cb7")
        with ZipFile(path) as archive:
            nonzip_archive(native, [(n, archive.read(n)) for n in archive.namelist()])
        path.unlink()
        signature = build_managed_placement_signature(native)
        before = native.read_bytes()
        async with factory.begin() as session:
            file = await session.get(LibraryFile, plan.target.binding.library_file_id)
            file.file_path, file.file_name, file.file_format = (
                str(native),
                native.name,
                FileFormat.CB7,
            )
            file.file_size = native.stat().st_size
            file.file_modified_at = datetime.fromtimestamp(native.stat().st_mtime, UTC)
            file.source_signature = signature
            action = await session.get(ImportJobAction, ids[2])
            action.payload = {
                **action.payload,
                "destination_path": str(native),
                "destination_signature": signature,
            }
        async with factory() as session:
            result = await convert_library_file(
                session,
                source=native,
                trash_dir=tmp_path / "trash",
                trash_relative_path=native.name,
                require_paired_metadata=True,
                operation_id=uuid4(),
            )
        assert path.exists() and not native.exists()
        from pathlib import Path

        assert Path(result.original_trash_path).read_bytes() == before
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"
            assert {"ComicInfo.xml", "MetronInfo.xml"} <= set(archive.namelist())
        output = path.read_bytes()
        assert await rollback(factory, ids[2]) is ImportJobActionStatus.ROLLBACK_FAILED
        assert path.read_bytes() == output and source.exists()
        async with factory() as session:
            assert (
                await session.get(LibraryFile, plan.target.binding.library_file_id)
            ).file_path == str(path)


async def test_noop_does_not_disable_original_import_rollback(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, source, plan, ids, _):
        await finish(factory, await publish(factory, plan))
        issue_id = plan.target.binding.metadata.issues[0].local_id
        async with factory() as session:
            reviewed = await prepare_file_metadata(session, issue_id)
        assert reviewed.preview.unchanged
        assert (
            await write_file_metadata(
                factory,
                issue_id,
                reviewed.preview.review_key,
                uuid4(),
                limit=1000000,
                check_control=noop,
                progress=noop,
            )
            == "unchanged"
        )
        async with factory() as session:
            assert "post_import_edit" not in (await session.get(ImportedFile, ids[1])).diagnostics
        assert await rollback(factory, ids[2]) is ImportJobActionStatus.ROLLED_BACK
        assert not path.exists() and source.exists()


@pytest.mark.parametrize("old_state", ["pending", "failed"])
async def test_explicit_write_supersedes_old_enrichment_work(
    identity_probe_db, tmp_path, old_state
):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        async with factory.begin() as session:
            imported = await session.get(ImportedFile, ids[1])
            details = dict(imported.diagnostics["comicinfo_enrichment"], status=old_state)
            imported.diagnostics = {**imported.diagnostics, "comicinfo_enrichment": details}
        issue_id = plan.target.binding.metadata.issues[0].local_id
        async with factory() as session:
            reviewed = await prepare_file_metadata(session, issue_id)
        assert (
            await write_file_metadata(
                factory,
                issue_id,
                reviewed.preview.review_key,
                uuid4(),
                limit=1000000,
                check_control=noop,
                progress=noop,
            )
            == "written"
        )
        async with factory() as session:
            imported = await session.get(ImportedFile, ids[1])
            assert imported.diagnostics["comicinfo_enrichment"]["status"] == "superseded"
            assert imported.diagnostics["post_import_edit"]
        assert await rollback(factory, ids[2]) is ImportJobActionStatus.ROLLBACK_FAILED
        assert path.exists()


@pytest.mark.parametrize("kind", ["metadata", "conversion"])
async def test_unfinished_publication_preserves_parent_then_recovers(
    identity_probe_db, tmp_path, kind
):
    from pullbox.core.file_safety import (
        get_archive_size_limit_bytes,
        is_dangerous_file_blocking_enabled,
    )
    from pullbox.services import library_conversion_files as files
    from pullbox.services import library_conversion_recovery as recovery
    from tests.integration.metadata_identity.test_archive_metadata_publication import record

    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, source, plan, ids, _):
        file_id = plan.target.binding.library_file_id
        if kind == "metadata":
            receipt = await record(factory, plan.model_copy(update={"import_owner": None}))
            from pullbox.services.archive_metadata_publication import publish_archive_publication

            async with factory.begin() as session:
                receipt = await publish_archive_publication(session, receipt.operation_id)
        else:
            operation = uuid4()
            async with factory() as session:
                binding = await recovery.read_conversion_binding(session, path)
                limit = await get_archive_size_limit_bytes(session)
                block = await is_dangerous_file_blocking_enabled(session)
            # A same-path repack has the same crash window as native conversion.
            async with (
                files.prepare_conversion(
                    path,
                    tmp_path / "trash" / path.name,
                    binding,
                    metadata_state=plan.target.binding.metadata,
                    max_uncompressed_bytes=limit,
                    metadata_policy_limit=limit,
                    block_dangerous=block,
                ) as conversion,
                factory() as session,
            ):
                await recovery.record_conversion(session, conversion, operation)
                await session.commit()
                await recovery.publish_conversion(session, operation)
                await session.commit()
        async with factory.begin() as session:
            parent = ImportJobAction(
                import_job_id=ids[0],
                sequence_no=0,
                phase="import",
                action_type="series_folder_renamed",
                payload={
                    "series_id": plan.target.binding.metadata.series.local_id,
                    "new_folder_path": str(path.parent),
                    "old_folder_path": str(tmp_path / "old-folder"),
                },
            )
            session.add(parent)
            await session.flush()
            await rollback_action(
                session,
                action_id=parent.id,
                action_type=parent.action_type,
                payload=deepcopy(parent.payload),
                delete_series=AsyncMock(),
            )
            assert parent.status is ImportJobActionStatus.ROLLBACK_FAILED
            assert path.exists(), "Rollback moved a folder while its file publication was active"
        async with factory.begin() as session:
            (await session.get(ImportJob, ids[0])).status = ImportJobStatus.ROLLING_BACK
        if kind == "metadata":
            inspection = await inspect_archive_publication(receipt)
            async with factory.begin() as session:
                await finalize_archive_publication(session, receipt, inspection)
        else:
            async with factory() as session:
                assert await recovery.recover_conversion(session, operation) == "complete"
        async with factory() as session:
            assert (await session.get(ImportedFile, ids[1])).diagnostics["post_import_edit"]
            assert await session.get(LibraryFile, file_id)
        assert await rollback(factory, ids[2]) is ImportJobActionStatus.ROLLBACK_FAILED
        assert path.exists() and source.exists()


@pytest.mark.parametrize("workflow", ["issue", "editor", "series", "conversion"])
async def test_completed_import_allows_edit_and_preserves_later_rollback(
    identity_probe_db, tmp_path, workflow
):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, source, plan, ids, signature):
        # Import-time enrichment remains reversible; the later explicit edit does not.
        await finish(factory, await publish(factory, plan))
        issue_id = plan.target.binding.metadata.issues[0].local_id
        series_id = plan.target.binding.metadata.series.local_id
        original_source = source.read_bytes()
        options = {}
        if workflow == "series":
            async with factory.begin() as session:
                (await session.get(Series, series_id)).sort_title = "Updated sort title"
            options["propagation_fields"] = frozenset({"sort_title"})
        elif workflow == "editor":
            options["editor"] = IssueMetadataEditorReview.model_validate(
                {"edits": {"title": {"source": "custom", "value": "User title"}}}
            )
        else:
            async with factory.begin() as session:
                (await session.get(Issue, issue_id)).description = "Updated library summary"
        operation = uuid4()
        if workflow == "conversion":
            async with factory() as session:
                await convert_library_file(
                    session,
                    source=path,
                    trash_dir=tmp_path / "trash",
                    trash_relative_path=path.name,
                    operation_id=operation,
                    require_paired_metadata=True,
                    repack_cbz=True,
                )
        else:
            async with factory() as session:
                review = await prepare_file_metadata(session, issue_id, **options)
            assert review.preview.ready
            assert (
                await write_file_metadata(
                    factory,
                    issue_id,
                    review.preview.review_key,
                    operation,
                    limit=1000000,
                    check_control=noop,
                    progress=noop,
                    **options,
                )
                == "written"
            )
            before_retry = path.read_bytes(), path.stat()
            assert (
                await write_file_metadata(
                    factory,
                    issue_id,
                    review.preview.review_key,
                    operation,
                    limit=1000000,
                    check_control=noop,
                    progress=noop,
                    **options,
                )
                == "recovered"
            )
            assert (path.read_bytes(), path.stat()) == before_retry
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"
        async with factory() as session:
            imported = await session.get(ImportedFile, ids[1])
            assert imported.diagnostics["post_import_edit"]["operation_id"] == str(operation)
            assert imported.diagnostics["keep"] == {"evidence": True}
            action = await session.get(ImportJobAction, ids[2])
            assert action.payload["destination_signature"] == signature
        edited = path.read_bytes(), path.stat()
        assert await rollback(factory, ids[2]) is ImportJobActionStatus.ROLLBACK_FAILED
        assert (path.read_bytes(), path.stat()) == edited
        assert source.read_bytes() == original_source
        async with factory.begin() as session:
            action = await session.get(ImportJobAction, ids[2])
            assert "edited after import" in action.error_message
            assert await session.get(LibraryFile, plan.target.binding.library_file_id)
            assert await session.get(Issue, issue_id)
            parent = ImportJobAction(
                import_job_id=ids[0],
                sequence_no=0,
                phase="import",
                action_type="series_folder_renamed",
                payload={
                    "series_id": series_id,
                    "new_folder_path": str(path.parent),
                    "old_folder_path": str(tmp_path / "old-folder"),
                },
            )
            session.add(parent)
            await session.flush()
            await rollback_action(
                session,
                action_id=parent.id,
                action_type=parent.action_type,
                payload=deepcopy(parent.payload),
                delete_series=AsyncMock(),
            )
            assert parent.status is ImportJobActionStatus.ROLLBACK_FAILED
            assert "edited after import" in parent.error_message
        assert path.is_file() and not (tmp_path / "old-folder").exists()


@pytest.mark.parametrize(
    "status",
    [
        ImportJobStatus.COMPLETED,
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
        ImportJobStatus.ROLLED_BACK,
    ],
)
async def test_terminal_history_does_not_block_preview(identity_probe_db, tmp_path, status):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        async with factory.begin() as session:
            (await session.get(ImportJob, ids[0])).status = status
        before = path.read_bytes()
        async with factory() as session:
            assert (
                await prepare_file_metadata(
                    session, plan.target.binding.metadata.issues[0].local_id
                )
            ).preview.ready
            assert "post_import_edit" not in (await session.get(ImportedFile, ids[1])).diagnostics
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    "status,control",
    [
        (ImportJobStatus.IMPORTING, ImportControlRequest.NONE),
        (ImportJobStatus.ROLLING_BACK, ImportControlRequest.NONE),
        (ImportJobStatus.PAUSED, ImportControlRequest.NONE),
        (ImportJobStatus.COMPLETED, ImportControlRequest.CANCEL),
    ],
)
async def test_unfinished_owner_still_defers_edit(identity_probe_db, tmp_path, status, control):
    _, factory, _ = identity_probe_db
    async with owned(factory, tmp_path) as (path, _, plan, ids, _):
        async with factory.begin() as session:
            job = await session.get(ImportJob, ids[0])
            job.status, job.control_request = status, control
        before = path.read_bytes()
        async with factory() as session:
            with pytest.raises(ArchiveMetadataBindingError, match="import_operation_active"):
                await prepare_file_metadata(
                    session, plan.target.binding.metadata.issues[0].local_id
                )
        assert path.read_bytes() == before
