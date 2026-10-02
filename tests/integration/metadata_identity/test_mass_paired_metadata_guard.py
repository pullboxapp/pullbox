"""The CI-only consumer must not publish competing paired metadata."""

from pathlib import Path
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree

from pullbox.config import get_settings
from pullbox.models import Issue
from pullbox.services.library_convert_service import convert_library_file
from pullbox.utilities.base_executor import ItemResult
from pullbox.utilities.executors.mass_convert_pipeline import MassConvertPipelineExecutor
from tests.integration.metadata_identity.test_library_paired_conversion import registered_cb7


@pytest.mark.parametrize("paired_enabled", [True, False])
@pytest.mark.parametrize("metron_member", ["MetronInfo.xml", "metadata/mEtRoNiNfO.xMl", None])
@pytest.mark.parametrize("scope", ["manual", "folder", "library"])
@pytest.mark.parametrize("steps", [[1, 2, 4], [1, 4]])
async def test_mass_comicinfo_step_preserves_existing_pair_until_coordinated(
    identity_probe_db, tmp_path, monkeypatch, paired_enabled, metron_member, scope, steps
):
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_CONVERSION_WRITER_ENABLED", "true")
    get_settings.cache_clear()
    try:
        _, factory, _ = identity_probe_db
        source, file_id, issue_id, *_ = await registered_cb7(factory, tmp_path)
        async with factory() as session:
            converted = await convert_library_file(
                session,
                source=source,
                trash_dir=tmp_path / "trash",
                trash_relative_path=source.name,
            )
        source = Path(converted.target_path)
        if metron_member != "MetronInfo.xml":
            with ZipFile(source) as archive:
                members = [(entry.filename, archive.read(entry)) for entry in archive.infolist()]
            with ZipFile(source, "w") as archive:
                for name, payload in members:
                    if name == "MetronInfo.xml":
                        if metron_member is None:
                            continue
                        name = metron_member
                    archive.writestr(name, payload)
        original = source.read_bytes()
        async with factory.begin() as session:
            issue = await session.get(Issue, issue_id)
            issue.issue_number_text = "50-o"
        monkeypatch.setenv(
            "PULLBOX_METADATA_PAIRED_CONVERSION_WRITER_ENABLED", str(paired_enabled).lower()
        )
        get_settings.cache_clear()
        config = {
            "scope": scope,
            "file_paths": [str(source)],
            "scan_folder": str(source.parent),
            "steps": steps,
            "trash_folder": str(tmp_path / "trash"),
        }
        async with factory() as session:
            executor = MassConvertPipelineExecutor(session)
            context = await executor.build_job_context(session, config)
        item = next(item for item in context["items"] if item["library_file_id"] == file_id)
        item["id"] = "preserve-existing-pair"
        processed = executor.process_item(item, config, context)
        if paired_enabled and metron_member is not None and 2 in steps:
            assert processed.result is ItemResult.FAILED, "CI-only rewrite published competing XML"
            assert "MetronInfo.xml" in processed.error_message
            assert source.read_bytes() == original
            assert not source.with_name(f"{source.stem}._mass_convert_.cbz").exists()
            assert not (tmp_path / "trash" / source.name).exists()
        else:
            assert processed.result is ItemResult.COMPLETED, processed.error_message
            with ZipFile(processed.after_state["path"]) as archive:
                ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
                assert ci.findtext("Number") == ("50-O" if 2 in steps else "50-X")
            restored = executor.rollback_item(
                {
                    "id": item["id"],
                    "file_path": str(source),
                    "before_state": processed.before_state,
                    "after_state": processed.after_state,
                },
                config,
            )
            assert restored.result is ItemResult.COMPLETED, restored.error_message
            assert source.read_bytes() == original
    finally:
        get_settings.cache_clear()
