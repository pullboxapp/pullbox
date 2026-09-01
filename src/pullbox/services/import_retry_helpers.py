"""Helpers for creating fresh retry import jobs from historical runs."""

from __future__ import annotations

from typing import Any

from pullbox.models.import_job import ImportFileHandlingMode
from pullbox.schemas.import_job import ImportJobCreate
from pullbox.schemas.import_layout import SourceLayoutSpecPayload

_RETRY_RUNTIME_FIELDS = (
    "move_to_library",
    "transfer_method",
    "torrent_import_strategy",
    "effective_import_strategy",
    "effective_transfer_method",
    "source_preserved",
    "convert_to_preferred_format",
    "update_embedded_comicinfo_from_match",
    "search_on_add",
)


def build_retry_import_request(original: Any) -> ImportJobCreate:
    """Build the creation request for a fresh retry job."""
    source_layout_snapshot = getattr(original, "source_layout_snapshot", None) or {}
    future_root_policy_snapshot = getattr(original, "future_root_policy_snapshot", None)
    frozen_path_map = dict(original.mylar3_path_map or {})
    return ImportJobCreate(
        source_path=original.source_path,
        file_paths=list(original.selected_file_paths or []) or None,
        source_type=original.source_type,
        target_library_root_id=original.target_library_root_id,
        monitored=original.monitored,
        mylar3_path_map=frozen_path_map,
        mylar3_path_map_confirmed=bool(
            frozen_path_map or getattr(original, "mylar3_path_map_confirmed", False)
        ),
        cv_match_threshold=original.cv_match_threshold,
        min_files_per_series=original.min_files_per_series,
        file_formats=original.file_formats,
        file_handling_mode=getattr(
            original,
            "file_handling_mode",
            ImportFileHandlingMode.MANAGED_COPY,
        ),
        source_layout=SourceLayoutSpecPayload.model_validate(source_layout_snapshot),
        future_layout_requested=bool(getattr(original, "future_layout_requested", False)),
        future_root_policy=future_root_policy_snapshot,
        story_arc_import_requested=bool(getattr(original, "story_arc_import_requested", False)),
        story_arc_materialization_requested=bool(
            getattr(original, "story_arc_materialization_requested", False)
        ),
    )


def copy_retry_import_settings(original: Any, retry: Any) -> None:
    """Copy runtime import policy fields onto the fresh retry job."""
    for field_name in _RETRY_RUNTIME_FIELDS:
        setattr(retry, field_name, getattr(original, field_name))
    retry.ingest_policy_snapshot = dict(original.ingest_policy_snapshot or {})
    retry.file_handling_mode = getattr(
        original,
        "file_handling_mode",
        ImportFileHandlingMode.MANAGED_COPY,
    )
    retry.source_layout_snapshot = dict(getattr(original, "source_layout_snapshot", None) or {})
    retry.future_layout_requested = bool(getattr(original, "future_layout_requested", False))
    future_root_policy_snapshot = getattr(original, "future_root_policy_snapshot", None)
    retry.future_root_policy_snapshot = (
        dict(future_root_policy_snapshot) if future_root_policy_snapshot is not None else None
    )
    retry.future_root_policy_applied_at = None
    retry.story_arc_import_requested = bool(getattr(original, "story_arc_import_requested", False))
    retry.story_arc_materialization_requested = bool(
        getattr(original, "story_arc_materialization_requested", False)
    )
