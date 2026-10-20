"""Explicit series corrections must not leave conflicting known JSON values."""

import json
from datetime import UTC, datetime

import pytest

from pullbox.core.metadata_identity import MetadataEntityKind
from pullbox.core.series_sidecar import render_series_sidecar
from pullbox.schemas.metadata_snapshot import MetadataSnapshot, MetadataValues


@pytest.mark.parametrize(
    "field,aliases,value",
    [
        ("description", ("description", "summary"), "Corrected description"),
        ("sort_title", ("sort_title",), "Atmos, Planet"),
        ("year_end", ("year_end",), 2026),
        ("volume", ("volume",), "2"),
        ("language", ("language",), "en"),
        ("archive_format", ("archive_format",), "Limited Series"),
    ],
)
@pytest.mark.parametrize("clear", [False, True])
def test_selected_correction_reconciles_recognized_legacy_layers(field, aliases, value, clear):
    values = MetadataValues.model_validate(
        {"title": "Planet Atmos", field: None if clear else value}
    )
    snapshot = MetadataSnapshot(entity_kind=MetadataEntityKind.SERIES, identities=(), values=values)
    previous = {
        **{key.upper(): "old" for key in aliases},
        "metadata": {key: "different" for key in aliases},
        "custom": {"description": "My note must remain"},
        "format": "cbz",
    }
    result = json.loads(
        render_series_sidecar(
            snapshot,
            (),
            json.dumps(previous).encode(),
            updated_at=datetime.now(UTC),
            edited_fields=frozenset({field}),
        )
    )
    for key in aliases:
        assert result[key.upper()] == (None if clear else value)
        assert result["metadata"][key] == (None if clear else value)
    assert result["custom"] == previous["custom"]
    assert result["format"] == "cbz"
    assert result["pullbox"]["snapshot"]["values"][field] == (None if clear else value)


def test_unselected_extension_values_are_not_rewritten_by_ordinary_export():
    snapshot = MetadataSnapshot(
        entity_kind=MetadataEntityKind.SERIES,
        identities=(),
        values=MetadataValues(title="Series", description="Canonical summary"),
    )
    previous = {"description": "Preserved existing value", "volume": "Personal volume"}
    result = json.loads(
        render_series_sidecar(
            snapshot,
            (),
            json.dumps(previous).encode(),
            updated_at=datetime.now(UTC),
        )
    )
    assert result["description"] == previous["description"]
    assert result["volume"] == previous["volume"]
