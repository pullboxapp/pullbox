"""Persisted series edits keep their scope across both supported databases."""

import json
from datetime import UTC, datetime

import pytest

from pullbox.core.metadata_identity import MetadataEntityKind, MetadataSource
from pullbox.models import Series
from pullbox.schemas.series_metadata_editor import SeriesMetadataEditorReview
from pullbox.services.metadata_assembly import assemble_metadata
from pullbox.services.metadata_series_refresh_state import read_series_refresh_state
from pullbox.services.series_metadata_editor import (
    prepare_series_editor,
    run_series_editor_target,
    save_series_editor,
)
from tests.integration.metadata_identity.test_series_sidecar import (
    managed_series as _managed_series,
)
from tests.unit.test_metadata_discovery import row, runtime

managed_series = _managed_series


async def save(factory, series_id, review):
    async with factory() as session:
        prepared = await prepare_series_editor(session, series_id, review)
    async with factory() as session:
        plan = await save_series_editor(session, series_id, review, prepared.preview.review_key)
        await session.commit()
    return plan


async def test_selected_fields_sidecar_and_replay_leave_comic_and_identity_untouched(
    managed_series,
):
    factory, series_id, _, folder, comic = managed_series
    original_comic = comic.read_bytes(), comic.stat()
    sidecar = folder / "series.json"
    sidecar.write_text(json.dumps({"comicid": 42, "custom": {"description": "Keep note"}}))
    review = SeriesMetadataEditorReview.model_validate(
        {
            "edits": {
                "title": {"action": "set", "value": "Reviewed display title"},
                "description": {"action": "set", "value": "Reviewed summary"},
                "language": {"action": "set", "value": "en"},
            }
        }
    )
    plan = await save(factory, series_id, review)
    async with factory() as session:
        result = await run_series_editor_target(session, plan, 0)
    assert result["outcome"] == "saved", result
    content = json.loads(sidecar.read_bytes())
    assert content["custom"]["description"] == "Keep note"
    assert content["pullbox"]["snapshot"] == plan.snapshot.model_dump(mode="json")
    first = sidecar.read_bytes(), sidecar.stat()
    async with factory() as session:
        replay = await run_series_editor_target(session, plan, 0)
        series = await session.get(Series, series_id)
        assert series.path == str(folder)
        assert series.comicvine_id == 42
        assert series.title == "Reviewed display title"
    assert replay["outcome"] == "saved", replay
    assert (sidecar.read_bytes(), sidecar.stat()) == first
    assert (comic.read_bytes(), comic.stat()) == original_comic


async def test_changed_sidecar_is_not_overwritten_after_library_save(managed_series):
    factory, series_id, _, folder, comic = managed_series
    plan = await save(
        factory,
        series_id,
        SeriesMetadataEditorReview.model_validate(
            {
                "edits": {
                    "title": {"action": "set", "value": "Reviewed title"},
                }
            }
        ),
    )
    sidecar = folder / "series.json"
    sidecar.write_text('{"custom":"Created after approval"}')
    before = sidecar.read_bytes(), comic.read_bytes()
    async with factory() as session:
        result = await run_series_editor_target(session, plan, 0)
    assert result["outcome"] == "failed"
    assert (sidecar.read_bytes(), comic.read_bytes()) == before
    async with factory() as session:
        assert (await session.get(Series, series_id)).title == "Reviewed title"


async def test_clear_and_override_reset_survive_database_round_trip(managed_series):
    factory, series_id, _, folder, comic = managed_series
    original = comic.read_bytes(), comic.stat()
    await save(
        factory,
        series_id,
        SeriesMetadataEditorReview.model_validate(
            {
                "mode": "library_only",
                "edits": {
                    "description": {"action": "clear"},
                    "title": {"action": "set", "value": "My title"},
                },
            }
        ),
    )
    source = MetadataSource.COMICVINE_API
    candidate = row(source).model_copy(
        update={"title": "Provider title", "description": "Provider summary"}
    )

    async def assembled():
        async with factory() as session:
            state = await read_series_refresh_state(session, series_id, include_issues=False)
        return assemble_metadata(
            MetadataEntityKind.SERIES,
            state.series.identities,
            [candidate],
            [runtime(source).policy],
            now=datetime.now(UTC),
            current=state.series.values,
            previous=state.series.baseline,
            overrides=state.series.overrides,
            replace_managed=True,
        )

    protected = await assembled()
    assert protected.values.title == "My title"
    assert protected.values.description is None
    await save(
        factory,
        series_id,
        SeriesMetadataEditorReview.model_validate(
            {
                "mode": "library_only",
                "edits": {"description": {"action": "reset"}},
            }
        ),
    )
    refreshed = await assembled()
    assert refreshed.values.title == "My title"
    assert refreshed.values.description == "Provider summary"
    assert not (folder / "series.json").exists()
    assert (comic.read_bytes(), comic.stat()) == original


async def test_concurrent_library_change_rejects_original_approval(managed_series):
    factory, series_id, _, folder, _ = managed_series
    review = SeriesMetadataEditorReview.model_validate(
        {
            "mode": "library_only",
            "edits": {
                "title": {"action": "set", "value": "Stale correction"},
            },
        }
    )
    async with factory() as session:
        prepared = await prepare_series_editor(session, series_id, review)
    async with factory.begin() as session:
        (await session.get(Series, series_id)).title = "More recent correction"
    async with factory() as session:
        with pytest.raises(ValueError, match="changed"):
            await save_series_editor(session, series_id, review, prepared.preview.review_key)
        await session.rollback()
        assert (await session.get(Series, series_id)).title == "More recent correction"
    assert not (folder / "series.json").exists()
