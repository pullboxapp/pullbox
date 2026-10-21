"""Refresh cascades exact identities, never title-searches or fans out unnecessarily."""

import asyncio

import pytest

from pullbox.schemas.metadata_sources import MetadataFetch, SourceCapability, SourceStatus
from pullbox.services.metadata_assembly import MetadataAssemblyError
from pullbox.services.metadata_discovery import MetadataSourceError, MetadataSourceRegistry
from pullbox.services.metadata_refresh_snapshot import fetch_metadata_snapshot
from tests.unit.test_metadata_assembly import (
    CV,
    METRON,
    NOW,
    Domain,
    Kind,
    Source,
    assemble,
    identity,
)
from tests.unit.test_metadata_discovery import registration, row, runtime
from tests.unit.test_metadata_source_reads import ReadAdapter


def setup(*, metron_title="Secondary", cv_title="Primary", **options):
    adapters = {}
    for source, title in ((CV, cv_title), (METRON, metron_title)):
        adapter = ReadAdapter(
            result=MetadataFetch(status=SourceStatus.OK, data=row(source, title=title))
        )
        adapter.source = source
        adapters[source] = adapter
    registry = MetadataSourceRegistry(
        [runtime(CV, revision=1), runtime(METRON, revision=2)],
        factories={
            s: registration(a, capabilities=list(SourceCapability)) for s, a in adapters.items()
        },
        **options,
    )
    return registry, adapters


async def fetch(registry, **kwargs):
    return await fetch_metadata_snapshot(
        registry,
        Kind.SERIES,
        kwargs.pop("identities", [identity(CV), identity(METRON)]),
        now=NOW,
        requested_fields=kwargs.pop("fields", frozenset({"title"})),
        **kwargs,
    )


async def test_uses_only_exact_native_identity_and_stops_when_domains_satisfied():
    registry, adapters = setup()
    result = await fetch(registry)
    assert result.snapshot.values.title == "Primary"
    assert adapters[CV].calls == [("series", "42", None)]
    assert adapters[METRON].calls == []
    assert result.revisions == {CV: 1, METRON: 2}
    result = await fetch(registry, identities=[identity(METRON)])
    assert result.snapshot.values.title == "Secondary"
    assert len(adapters[CV].calls) == 1


async def test_separate_domain_priority_and_gap_fill_without_repeat_fetch():
    registry, adapters = setup()
    registry.runtime[METRON] = runtime(METRON, revision=2, domain_priorities={Domain.ARTWORK: 0})
    adapters[METRON].result.data.image_url = "https://metron.cloud/media/cover.jpg"
    adapters[METRON].result.data.description = "Description"
    result = await fetch(registry, fields=frozenset({"title", "description", "image_url"}))
    assert result.snapshot.values.title == "Primary"
    assert result.snapshot.values.description == "Description"
    assert result.snapshot.values.image_url == "https://metron.cloud/media/cover.jpg"
    assert all(len(a.calls) == 1 and a.closed == 1 for a in adapters.values())


async def test_fresh_catalog_evidence_is_reused_without_skipping_higher_priority():
    registry, adapters = setup()
    seed = row(METRON, title="Already fetched").model_copy(
        update={"description": "Catalog description"}
    )
    result = await fetch(
        registry,
        fields=frozenset({"title", "description"}),
        initial_candidates=[seed],
    )
    assert result.snapshot.values.title == "Primary"
    assert result.snapshot.values.description == "Catalog description"
    assert adapters[CV].calls == [("series", "42", None)]
    assert not adapters[METRON].calls
    assert {outcome.status for outcome in result.outcomes} == {SourceStatus.OK}


async def test_seeded_evidence_cannot_bypass_exact_identity_validation():
    registry, adapters = setup()
    with pytest.raises(MetadataAssemblyError, match="verified exact identity"):
        await fetch(registry, initial_candidates=[row(METRON, external_id="99")])
    assert not any(adapter.calls for adapter in adapters.values())


async def test_failure_keeps_typed_outcome_and_falls_back_without_changing_user_value():
    registry, adapters = setup()
    adapters[CV].error = MetadataSourceError(SourceStatus.RATE_LIMITED, 60)
    result = await fetch(registry)
    assert result.snapshot.values.title == "Secondary"
    failure = next(outcome for outcome in result.outcomes if outcome.source is CV)
    assert failure.status is SourceStatus.RATE_LIMITED and failure.retry_after_seconds == 60
    before = assemble([row(CV, title="Saved")])
    result = await fetch(registry, current=before.values, previous=before, replace_managed=True)
    assert result.snapshot.values.title == "Saved"


async def test_explicit_refresh_requests_replacements_for_inferred_metadata():
    from pullbox.schemas.metadata_snapshot import FieldOrigin, MetadataSnapshot, MetadataValues

    registry, adapters = setup()
    before = MetadataSnapshot(
        entity_kind=Kind.SERIES,
        identities=(identity(CV), identity(METRON)),
        values=MetadataValues(status="ended"),
        origins=(
            FieldOrigin(
                field="status", domain=Domain.CORE, observed_at=NOW, derivation="lifecycle"
            ),
        ),
    )
    adapters[CV].result.data.status = "continuing"
    result = await fetch(
        registry, fields=frozenset({"status"}), previous=before, replace_managed=True
    )
    assert result.snapshot.values.status == "continuing"
    assert adapters[CV].calls == [("series", "42", None)]
    assert not adapters[METRON].calls


@pytest.mark.parametrize("replace_managed", [True, False])
async def test_reset_nonempty_field_fetches_only_on_replacing_refresh(replace_managed):
    from pullbox.schemas.metadata_snapshot import FieldOrigin, MetadataSnapshot, MetadataValues

    registry, adapters = setup()
    before = MetadataSnapshot(
        entity_kind=Kind.SERIES,
        identities=(identity(CV), identity(METRON)),
        values=MetadataValues(title="Former manual correction"),
        origins=(
            FieldOrigin(
                field="title",
                domain=Domain.CORE,
                observed_at=NOW,
                provider_managed=True,
            ),
        ),
    )
    result = await fetch(
        registry,
        current=before.values,
        previous=before,
        replace_managed=replace_managed,
    )
    assert result.snapshot.values.title == (
        "Primary" if replace_managed else "Former manual correction"
    )
    assert adapters[CV].calls == ([("series", "42", None)] if replace_managed else [])
    assert not adapters[METRON].calls


async def test_background_already_populated_or_protected_fields_need_no_network():
    registry, adapters = setup()
    before = assemble([row(CV, title="Saved")])
    result = await fetch(registry, current=before.values, previous=before)
    assert result.snapshot.values.title == "Saved"
    assert all(not a.calls for a in adapters.values())

    result = await fetch(
        registry,
        current=before.values,
        previous=before,
        replace_managed=True,
        overrides=frozenset({"title"}),
    )
    assert result.snapshot.values.title == "Saved"
    assert all(not a.calls for a in adapters.values())


async def test_artwork_only_refresh_does_not_change_unrequested_descriptive_fields():
    registry, adapters = setup()
    before = assemble([row(CV, title="Keep title")])
    adapters[CV].result.data.image_url = "https://comicvine.gamespot.com/new.jpg"
    result = await fetch(
        registry,
        fields=frozenset({"image_url"}),
        current=before.values,
        previous=before,
        replace_managed=True,
    )
    assert result.snapshot.values.image_url == adapters[CV].result.data.image_url
    assert result.snapshot.values.title == "Keep title"


async def test_disabled_and_feature_gated_sources_never_construct_clients():
    registry, adapters = setup()
    registry.runtime[CV] = runtime(CV, enabled=False)
    registry.runtime[Source.GCD_API_V2] = runtime(Source.GCD_API_V2)
    result = await fetch(
        registry, identities=[identity(CV), identity(METRON), identity(Source.GCD_API_V2)]
    )
    assert result.snapshot.values.title == "Secondary"
    assert not adapters[CV].calls
    assert any(
        o.source is Source.GCD_API_V2 and o.status is SourceStatus.FEATURE_DISABLED
        for o in result.outcomes
    )


async def test_crosswalk_conflict_never_triggers_search_or_fallback():
    registry, adapters = setup()
    adapters[CV].result.data.cross_identities = [identity(METRON, identifier="99")]
    with pytest.raises(MetadataAssemblyError):
        await fetch(registry)
    assert not adapters[METRON].calls


async def test_wrong_native_response_never_falls_back_to_another_provider():
    registry, adapters = setup()
    adapters[CV].result.data.external_id = "99"
    with pytest.raises(MetadataAssemblyError):
        await fetch(registry)
    assert not adapters[METRON].calls


async def test_total_deadline_and_cancellation_are_bounded_and_close_reads():
    registry, adapters = setup(total_timeout=0.03)
    adapters[CV].wait = asyncio.Event()
    result = await fetch(registry)
    assert result.snapshot.values.title is None
    assert any(outcome.status is SourceStatus.TIMEOUT for outcome in result.outcomes)
    assert adapters[CV].closed == 1
    registry.total_timeout = 10
    task = asyncio.create_task(fetch(registry))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapters[CV].closed == 2


@pytest.mark.parametrize("fields", [frozenset(), frozenset({"file_path"})])
async def test_invalid_requested_fields_never_make_requests(fields):
    registry, adapters = setup()
    with pytest.raises(ValueError):
        await fetch(registry, fields=fields)
    assert all(not a.calls for a in adapters.values())
