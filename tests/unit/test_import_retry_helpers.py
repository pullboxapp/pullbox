"""Tests for import retry helper field mapping."""

from __future__ import annotations

from types import SimpleNamespace


def test_build_retry_import_request_copies_creation_fields(tmp_path) -> None:
    from pullbox.models.import_job import ImportFileHandlingMode, ImportSourceType
    from pullbox.schemas.import_layout import SourceLayoutSpecPayload
    from pullbox.services.import_retry_helpers import build_retry_import_request

    source_dir = tmp_path / "retry-source"
    source_dir.mkdir()
    explicit_file = source_dir / "Issue 001.cbz"
    explicit_file.write_text("fake comic")

    original = SimpleNamespace(
        source_path=str(source_dir),
        selected_file_paths=(str(explicit_file),),
        source_type=ImportSourceType.FILESYSTEM,
        target_library_root_id=7,
        monitored=True,
        mylar3_path_map={"container": "host"},
        cv_match_threshold=0.82,
        min_files_per_series=3,
        file_formats="CBZ, PDF",
        file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
        source_layout_snapshot={
            "schema_version": 1,
            "mode": "preset",
            "preset": "publisher_series",
            "series_path_template": "{Publisher}/{Series}",
            "issue_filename_template": None,
            "selected_cluster_id": None,
            "fallback_to_auto": True,
        },
        future_layout_requested=False,
        future_root_policy_snapshot=None,
        story_arc_import_requested=True,
        story_arc_materialization_requested=True,
    )

    request = build_retry_import_request(original)

    assert request.source_path == str(source_dir.resolve())
    assert request.file_paths == [str(explicit_file.resolve())]
    assert request.source_type == ImportSourceType.FILESYSTEM
    assert request.target_library_root_id == 7
    assert request.monitored is True
    assert request.mylar3_path_map == {"container": "host"}
    assert request.cv_match_threshold == 0.82
    assert request.min_files_per_series == 3
    assert request.file_formats == "cbz, pdf"
    assert request.file_handling_mode == ImportFileHandlingMode.MANAGED_COPY
    assert request.source_layout == SourceLayoutSpecPayload(
        mode="preset",
        preset="publisher_series",
        series_path_template="{Publisher}/{Series}",
    )
    assert request.future_layout_requested is False
    assert request.future_root_policy is None
    assert request.story_arc_import_requested is True
    assert request.story_arc_materialization_requested is True


def test_build_retry_import_request_omits_empty_selected_file_paths(tmp_path) -> None:
    from pullbox.models.import_job import ImportSourceType
    from pullbox.services.import_retry_helpers import build_retry_import_request

    source_dir = tmp_path / "retry-source"
    source_dir.mkdir()
    original = SimpleNamespace(
        source_path=str(source_dir),
        selected_file_paths=[],
        source_type=ImportSourceType.FILESYSTEM,
        target_library_root_id=None,
        monitored=False,
        mylar3_path_map=None,
        cv_match_threshold=0.7,
        min_files_per_series=1,
        file_formats=None,
        file_handling_mode="managed_copy",
        source_layout_snapshot=None,
        future_layout_requested=False,
        future_root_policy_snapshot=None,
    )

    request = build_retry_import_request(original)

    assert request.file_paths is None
    assert request.mylar3_path_map == {}
    assert request.story_arc_import_requested is False
    assert request.story_arc_materialization_requested is False


def test_copy_retry_import_settings_clones_runtime_policy_fields() -> None:
    from pullbox.services.import_retry_helpers import copy_retry_import_settings

    original = SimpleNamespace(
        move_to_library=False,
        transfer_method="copy",
        torrent_import_strategy="copy",
        effective_import_strategy="move",
        effective_transfer_method="hardlink",
        source_preserved=True,
        convert_to_preferred_format=True,
        update_embedded_comicinfo_from_match=True,
        ingest_policy_snapshot={"rename": True},
        search_on_add=True,
        file_handling_mode="managed_copy",
        source_layout_snapshot={"schema_version": 1, "mode": "auto"},
        future_layout_requested=False,
        future_root_policy_snapshot={"schema_version": 1, "series_path_template": "{Series}"},
        future_root_policy_applied_at=None,
        story_arc_import_requested=True,
        story_arc_materialization_requested=False,
    )
    retry = SimpleNamespace()

    copy_retry_import_settings(original, retry)

    assert retry.move_to_library is False
    assert retry.transfer_method == "copy"
    assert retry.torrent_import_strategy == "copy"
    assert retry.effective_import_strategy == "move"
    assert retry.effective_transfer_method == "hardlink"
    assert retry.source_preserved is True
    assert retry.convert_to_preferred_format is True
    assert retry.update_embedded_comicinfo_from_match is True
    assert retry.ingest_policy_snapshot == {"rename": True}
    assert retry.ingest_policy_snapshot is not original.ingest_policy_snapshot
    assert retry.search_on_add is True
    assert retry.file_handling_mode == "managed_copy"
    assert retry.source_layout_snapshot == {"schema_version": 1, "mode": "auto"}
    assert retry.source_layout_snapshot is not original.source_layout_snapshot
    assert retry.future_layout_requested is False
    assert retry.future_root_policy_snapshot == {
        "schema_version": 1,
        "series_path_template": "{Series}",
    }
    assert retry.future_root_policy_snapshot is not original.future_root_policy_snapshot
    assert retry.future_root_policy_applied_at is None
    assert retry.story_arc_import_requested is True
    assert retry.story_arc_materialization_requested is False
