"""An explicitly written pair must remain protected with automatic writing off."""

from uuid import uuid4
from zipfile import ZipFile

from defusedxml import ElementTree

from pullbox.config import get_settings
from pullbox.models import Issue
from pullbox.services.issue_file_metadata import write_file_metadata
from pullbox.utilities.base_executor import ItemResult
from pullbox.utilities.executors.mass_convert_pipeline import MassConvertPipelineExecutor
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_issue_file_metadata import noop, preview


async def test_explicitly_written_pair_stays_protected_with_automatic_writing_off(
    identity_probe_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_IMPORT_WRITER_ENABLED", "false")
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_CONVERSION_WRITER_ENABLED", "false")
    get_settings.cache_clear()
    try:
        _, factory, _ = identity_probe_db
        async with prepared(factory, tmp_path) as (path, _, plan):
            issue_id = plan.target.binding.metadata.issues[0].local_id
            reviewed = await preview(factory, issue_id)
            outcome = await write_file_metadata(
                factory,
                issue_id,
                reviewed.preview.review_key,
                uuid4(),
                limit=1000000,
                check_control=noop,
                progress=noop,
            )
            assert outcome == "written"
            with ZipFile(path) as archive:
                for name in ("ComicInfo.xml", "MetronInfo.xml"):
                    assert ElementTree.fromstring(archive.read(name)).findtext("Number") == "50-X"
            original = path.read_bytes()
            async with factory.begin() as session:
                issue = await session.get(Issue, issue_id)
                issue.issue_number_text = "50-o"
            config = {
                "scope": "manual",
                "file_paths": [str(path)],
                "steps": [1, 2, 4],
                "trash_folder": str(tmp_path / "trash"),
            }
            async with factory() as session:
                executor = MassConvertPipelineExecutor(session)
                context = await executor.build_job_context(session, config)
            assert len(context["items"]) == 1
            item = context["items"][0]
            item["id"] = "explicit-pair-guard"
            processed = executor.process_item(item, config, context)
            assert processed.result is ItemResult.FAILED, "Automatic flag bypassed pair protection"
            assert "MetronInfo.xml" in processed.error_message
            assert path.read_bytes() == original
            assert not path.with_name(f"{path.stem}._mass_convert_.cbz").exists()
    finally:
        get_settings.cache_clear()
