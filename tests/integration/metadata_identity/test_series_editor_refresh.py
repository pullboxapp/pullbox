"""Real source refresh preserves independent series editor lifecycle corrections."""

from datetime import date

import pytest

from pullbox.models.series import SeriesStatus
from pullbox.schemas.series_metadata_editor import SeriesMetadataEditorReview
from pullbox.services.metadata_series_adoption import adopt_source_series_bundle
from pullbox.services.metadata_series_refresh import refresh_series_from_sources
from pullbox.services.metadata_series_refresh_state import read_series_refresh_state
from tests.integration.metadata_identity.test_series_adoption import (
    bundle,
    configured_sources,  # noqa: F401
)
from tests.integration.metadata_identity.test_series_editor_contract import save
from tests.integration.metadata_identity.test_series_refresh import RefreshAdapter, refresh_registry


@pytest.mark.parametrize("manual_end", [2030, None])
async def test_lifecycle_refresh_keeps_manual_end_year_but_updates_derived_status(
    identity_probe_db, manual_end
):
    _, factory, _ = identity_probe_db
    data = bundle(numbers=("1",))
    data.series.year_start = 2010
    data.series.series_type = "standard"
    data.series.status = None
    data.issues[0].cover_date = date(2014, 1, 1)
    async with factory.begin() as session:
        adopted = await adopt_source_series_bundle(session, data)
        series_id = adopted.series.id
        assert adopted.series.status is SeriesStatus.ENDED
        assert (
            next(
                origin for origin in adopted.snapshot.origins if origin.field == "status"
            ).derivation
            == "lifecycle"
        )
    await save(
        factory,
        series_id,
        SeriesMetadataEditorReview.model_validate(
            {
                "mode": "library_only",
                "edits": {
                    "year_end": {"action": "clear"}
                    if manual_end is None
                    else {"action": "set", "value": manual_end},
                },
            }
        ),
    )
    data.issues[0].cover_date = date(2015, 1, 1) if manual_end is None else date.today()
    async with factory() as session:
        refreshed = await refresh_series_from_sources(
            session,
            series_id,
            registry=refresh_registry(RefreshAdapter(data, session=session)),
        )
        assert refreshed.year_end == manual_end
        assert refreshed.status is (
            SeriesStatus.ENDED if manual_end is None else SeriesStatus.CONTINUING
        )
        await session.commit()
    async with factory() as session:
        state = await read_series_refresh_state(session, series_id, include_issues=False)
        assert state.series.values.year_end == manual_end
        origin = next(
            origin for origin in state.series.baseline.origins if origin.field == "year_end"
        )
        assert origin.user_override and origin.derivation is None
