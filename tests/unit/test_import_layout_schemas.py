"""Tests for layout-preview API schemas."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from pullbox.core.library_layout import ImportLayoutMode
from pullbox.models.import_job import ImportSourceType
from pullbox.schemas.import_layout import LayoutPreviewRequest, SourceLayoutSpecPayload

if TYPE_CHECKING:
    from pathlib import Path


def test_layout_preview_request_defaults_to_auto_filesystem(tmp_path: Path) -> None:
    request = LayoutPreviewRequest(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
    )

    assert request.source_path == str(tmp_path.resolve())
    assert request.layout == SourceLayoutSpecPayload()
    assert request.layout.mode == ImportLayoutMode.AUTO
    assert request.layout.to_core().to_dict()["schema_version"] == 1


def test_layout_preview_request_accepts_normalized_custom_source_layout(
    tmp_path: Path,
) -> None:
    request = LayoutPreviewRequest(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        layout=SourceLayoutSpecPayload(
            mode=ImportLayoutMode.CUSTOM,
            series_path_template="{Publisher}/{Series} ({Year})",
            issue_filename_template="Issue {Issue:03d} - {IssueTitle}",
            fallback_to_auto=False,
        ),
    )

    assert request.layout.to_core().series_path_template == ("{Publisher}/{Series} ({Year})")
    assert request.layout.model_dump(mode="json") == {
        "schema_version": 1,
        "mode": "custom",
        "preset": None,
        "series_path_template": "{Publisher}/{Series} ({Year})",
        "issue_filename_template": "Issue {Issue:03d} - {IssueTitle}",
        "selected_cluster_id": None,
        "fallback_to_auto": False,
    }


@pytest.mark.parametrize(
    "layout",
    [
        {"mode": "preset", "preset": "missing"},
        {"mode": "custom", "series_path_template": "/{Series}"},
        {"mode": "custom", "series_path_template": "{Unknown}"},
        {"schema_version": 2, "mode": "auto"},
    ],
)
def test_layout_preview_request_rejects_invalid_layout(
    tmp_path: Path,
    layout: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        LayoutPreviewRequest(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            layout=layout,  # type: ignore[arg-type]
        )


def test_layout_preview_request_rejects_file_and_mylar_sources(tmp_path: Path) -> None:
    source_file = tmp_path / "mylar.db"
    source_file.write_bytes(b"fixture")

    with pytest.raises(ValidationError, match="directory"):
        LayoutPreviewRequest(
            source_path=str(source_file),
            source_type=ImportSourceType.FILESYSTEM,
        )
    with pytest.raises(ValidationError, match="filesystem"):
        LayoutPreviewRequest(
            source_path=str(tmp_path),
            source_type=ImportSourceType.MYLAR3,
        )
