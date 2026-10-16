"""A rolled-back conversion must remain eligible for its original archive format."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pullbox.models import LibraryFile
from pullbox.models.library import FileFormat
from pullbox.services.archive_metadata_binding import read_archive_metadata_binding
from pullbox.utilities.base_executor import ItemResult, JobRunSummary, ProcessedItem
from pullbox.utilities.executors.mass_convert_pipeline import MassConvertPipelineExecutor
from tests.integration.metadata_identity.test_archive_metadata_binding import seed
from tests.integration.metadata_identity.test_library_paired_conversion import registered_cb7
from tests.unit.test_pdf_metadata_writing import native_pdf, pdf_source


@pytest.mark.parametrize(
    "source_format",
    [FileFormat.CB7, FileFormat.CBZ, pytest.param(FileFormat.PDF, marks=native_pdf)],
)
@pytest.mark.parametrize("scope", ["manual", "folder", "library"])
async def test_mass_conversion_rollback_restores_the_registered_format(
    identity_probe_db, tmp_path, source_format, scope
):
    _, factory, _ = identity_probe_db
    if source_format is FileFormat.CB7:
        source, file_id, *_ = await registered_cb7(factory, tmp_path)
    else:
        file_id, _, _, _, source = await seed(factory, tmp_path)
    if source_format is FileFormat.PDF:
        archive = source
        source = pdf_source(source.with_suffix(".pdf"))
        archive.unlink()
    original_has_comicinfo = source_format is not FileFormat.PDF
    async with factory.begin() as session:
        file = await session.get(LibraryFile, file_id)
        file.file_path = str(source)
        file.file_name = source.name
        file.file_format = source_format
        file.file_size = source.stat().st_size
        file.file_modified_at = datetime.fromtimestamp(source.stat().st_mtime, UTC)
        file.has_comicinfo = original_has_comicinfo
    original = source.read_bytes()
    config = {
        "scope": scope,
        "file_paths": [str(source)],
        "scan_folder": str(source.parent),
        "steps": [1, 2, 4],
        "trash_folder": str(tmp_path / "trash"),
    }
    async with factory() as session:
        executor = MassConvertPipelineExecutor(session)
        context = await executor.build_job_context(session, config)
    item = next(item for item in context["items"] if item["library_file_id"] == file_id)
    item["id"] = "format-roundtrip"
    processed = executor.process_item(item, config, context)
    assert processed.result is ItemResult.COMPLETED, processed.error_message
    converted = Path(processed.after_state["path"])
    async with factory.begin() as session:
        await executor.apply_item_result(
            session, None, item, processed, config, context, JobRunSummary()
        )
        await session.flush()
        file = await session.get(LibraryFile, file_id)
        assert file.file_format is FileFormat.CBZ and file.has_comicinfo
    rollback_item = {
        "id": item["id"],
        "file_path": str(converted),
        "before_state": processed.before_state,
        "after_state": processed.after_state,
    }
    restored = executor.rollback_item(rollback_item, config)
    assert restored.result is ItemResult.COMPLETED, restored.error_message
    async with factory.begin() as session:
        await executor.apply_rollback_result(session, rollback_item, restored)
    async with factory() as session:
        file = await session.get(LibraryFile, file_id)
        assert file.file_path == str(source)
        assert file.file_format is source_format
        assert file.has_comicinfo is original_has_comicinfo
        binding = await read_archive_metadata_binding(
            session, file_id, allow_conversion_source=True
        )
        assert binding is not None
    assert source.read_bytes() == original
    if source_format is not FileFormat.CBZ:
        assert not converted.exists()


@pytest.mark.parametrize("saved_flag", [None, "false", 0])
async def test_legacy_conversion_journal_does_not_guess_original_comicinfo(
    identity_probe_db, tmp_path, saved_flag
):
    _, factory, _ = identity_probe_db
    file_id, _, _, _, source = await seed(factory, tmp_path)
    async with factory.begin() as session:
        file = await session.get(LibraryFile, file_id)
        file.has_comicinfo = True
    before_state = {"path": str(source)}
    if saved_flag is not None:
        before_state["has_comicinfo"] = saved_flag
    async with factory.begin() as session:
        await MassConvertPipelineExecutor.apply_rollback_result(
            session,
            {"file_path": str(source), "before_state": before_state},
            ProcessedItem(item_id="legacy", result=ItemResult.COMPLETED),
        )
    async with factory() as session:
        file = await session.get(LibraryFile, file_id)
        assert file.has_comicinfo
