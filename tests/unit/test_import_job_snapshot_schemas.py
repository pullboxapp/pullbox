"""Compatibility and validation tests for durable import-job snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from pullbox.models.import_job import ImportFileHandlingMode, ImportSourceType
from pullbox.schemas.import_job import FutureRootPolicyPayload, ImportJobCreate
from pullbox.schemas.import_layout import SourceLayoutSpecPayload

if TYPE_CHECKING:
    from pathlib import Path


def _future_policy() -> FutureRootPolicyPayload:
    return FutureRootPolicyPayload(
        series_path_template="{Publisher}/{Series} ({Year})",
        comic_file_template="{Series} {IssueTitle} Issue {Issue:03d}",
        annual_file_template="{Series} Annual Issue {Issue:03d}",
        non_standard_file_template="{Series} {Type} {Volume:02d} - {IssueTitle}",
        single_non_standard_file_template="{Series} {Type} - {IssueTitle}",
        replace_illegal_characters=True,
        colon_replacement="dash",
    )


def test_old_create_payload_defaults_to_compatible_durable_contract(tmp_path: Path) -> None:
    request = ImportJobCreate(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
    )

    assert request.file_handling_mode == ImportFileHandlingMode.MANAGED_COPY
    assert request.source_layout == SourceLayoutSpecPayload()
    assert request.future_layout_requested is False
    assert request.future_root_policy is None
    assert request.story_arc_import_requested is False
    assert request.story_arc_materialization_requested is False


def test_new_create_payload_is_typed_and_serializable(tmp_path: Path) -> None:
    request = ImportJobCreate(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        source_layout=SourceLayoutSpecPayload(
            mode="custom",
            series_path_template="{Publisher}/{Series} ({Year})",
            issue_filename_template="{Series} {IssueTitle} Issue {Issue:03d}",
            fallback_to_auto=False,
        ),
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        future_layout_requested=True,
        future_root_policy=_future_policy(),
        story_arc_import_requested=True,
        story_arc_materialization_requested=True,
    )

    dumped = request.model_dump(mode="json")
    assert dumped["file_handling_mode"] == "in_place"
    assert dumped["source_layout"]["schema_version"] == 1
    assert dumped["source_layout"]["mode"] == "custom"
    assert dumped["future_layout_requested"] is True
    assert dumped["future_root_policy"]["schema_version"] == 1
    assert dumped["story_arc_import_requested"] is True
    assert dumped["story_arc_materialization_requested"] is True


def test_create_payload_keeps_logical_story_arcs_independent_from_folder_materialization(
    tmp_path: Path,
) -> None:
    request = ImportJobCreate(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        story_arc_import_requested=True,
        story_arc_materialization_requested=False,
    )

    assert request.story_arc_import_requested is True
    assert request.story_arc_materialization_requested is False


def test_create_payload_rejects_story_arc_materialization_without_logical_import(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="Story arc materialization requires"):
        ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            story_arc_import_requested=False,
            story_arc_materialization_requested=True,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"future_layout_requested": True},
        {"future_root_policy": _future_policy()},
    ],
)
def test_create_payload_rejects_contradictory_future_policy(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="future"):
        ImportJobCreate(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            **overrides,
        )


def test_future_policy_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValidationError):
        FutureRootPolicyPayload(
            schema_version=2,
            series_path_template="{Series}",
            comic_file_template="{Series} {Issue}",
            annual_file_template="{Series} Annual {Issue}",
            non_standard_file_template="{Series} {Type}",
            single_non_standard_file_template="{Series} {Type}",
            replace_illegal_characters=True,
            colon_replacement="dash",
        )
