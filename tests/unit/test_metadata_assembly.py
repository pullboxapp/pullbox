"""Authority cannot manufacture identity proof or erase user intent."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
)
from pullbox.core.metadata_identity import (
    MetadataEntityKind as Kind,
)
from pullbox.core.metadata_identity import (
    MetadataSource as Source,
)
from pullbox.schemas.metadata_snapshot import FieldOrigin, MetadataSnapshot, MetadataValues
from pullbox.schemas.metadata_sources import MetadataDomain as Domain
from pullbox.schemas.metadata_sources import ProviderIssueRead, ProviderStoryArcRead
from pullbox.services.metadata_assembly import MetadataAssemblyError, assemble_metadata
from tests.unit.test_metadata_discovery import row, runtime

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
OLDER = datetime(2026, 9, 27, 12, tzinfo=UTC)
CV, METRON = Source.COMICVINE_API, Source.METRON_API


def identity(source, kind=Kind.SERIES, identifier="42"):
    return ExternalIdentityRef(source.identity_namespace, kind, identifier)


def assemble(candidates, **kwargs):
    return assemble_metadata(
        Kind.SERIES,
        (identity(CV), identity(METRON)),
        candidates,
        kwargs.pop("policies", [runtime(CV).policy, runtime(METRON).policy]),
        now=NOW,
        **kwargs,
    )


def origin(snapshot, field):
    return next(item for item in snapshot.origins if item.field == field)


@pytest.mark.parametrize("count", [-1, 1000001, 1000000000])
def test_out_of_range_series_count_is_advisory_and_raw_evidence_is_preserved(count):
    candidate = row(METRON).model_copy(update={"issue_count": count})
    result = assemble([candidate])
    assert result.values.title == candidate.title
    assert result.values.issue_count is None
    assert result.diagnostics == ("metron_api:invalid_advisory_issue_count",)
    assert candidate.issue_count == count


@pytest.mark.parametrize("count", [None, 0, 1, 1000000])
def test_valid_advisory_series_count_is_preserved(count):
    candidate = row(METRON).model_copy(update={"issue_count": count})
    result = assemble([candidate])
    assert result.values.issue_count == count
    assert result.diagnostics == ()


def test_derived_values_are_refreshable_without_becoming_user_overrides():
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
    candidate = row(CV).model_copy(update={"status": "continuing"})
    result = assemble([candidate], previous=before, replace_managed=True)
    assert result.values.status == "continuing"
    assert origin(result, "status").source is CV
    assert origin(result, "status").derivation is None
    background = assemble([candidate], previous=before)
    assert background.values.status == "ended"
    assert origin(background, "status").derivation == "lifecycle"
    edited = assemble(
        [candidate],
        previous=before,
        current=MetadataValues(status="cancelled"),
        replace_managed=True,
    )
    assert edited.values.status == "cancelled" and origin(edited, "status").user_override


@pytest.mark.parametrize("attributes", [{"source": CV}, {"user_override": True}])
def test_derived_provenance_cannot_claim_provider_or_user_origin(attributes):
    with pytest.raises(ValidationError):
        MetadataSnapshot(
            entity_kind=Kind.SERIES,
            identities=(identity(CV),),
            values=MetadataValues(status="ended"),
            origins=(
                FieldOrigin(
                    field="status",
                    domain=Domain.CORE,
                    observed_at=NOW,
                    derivation="lifecycle",
                    **attributes,
                ),
            ),
        )


def test_domain_authority_gap_fill_and_serializable_provenance():
    cv = row(CV, title="Primary title").model_copy(
        update={"image_url": "https://comicvine.gamespot.com/a.jpg", "source_updated_at": OLDER}
    )
    metron = row(METRON, title="Other title").model_copy(
        update={"description": "Filled gap", "image_url": "https://metron.cloud/media/a.jpg"}
    )
    policies = [runtime(CV).policy, runtime(METRON, domain_priorities={Domain.ARTWORK: 0}).policy]
    snapshot = assemble([metron, cv], policies=policies)
    assert snapshot.values.title == "Primary title"
    assert snapshot.values.description == "Filled gap"
    assert snapshot.values.image_url == metron.image_url
    assert origin(snapshot, "title").source is CV
    assert origin(snapshot, "title").source_updated_at == OLDER
    assert origin(snapshot, "title").observed_at == NOW
    assert origin(snapshot, "image_url").domain is Domain.ARTWORK
    assert snapshot == MetadataSnapshot.model_validate_json(snapshot.model_dump_json())
    assert snapshot == assemble([cv, metron], policies=policies)


def test_background_fills_only_gaps_and_explicit_refresh_replaces_managed_values():
    before = assemble([row(CV, title="Original")])
    updated = row(CV, title="Updated").model_copy(update={"description": "New description"})
    background = assemble([updated], current=before.values, previous=before)
    assert background.values.title == "Original"
    assert background.values.description == "New description"
    assert origin(background, "title") == origin(before, "title")
    manual = assemble([updated], current=before.values, previous=before, replace_managed=True)
    assert manual.values.title == "Updated"


@pytest.mark.parametrize("title", ["My title", "", None])
def test_local_edit_including_clear_is_preserved_through_roundtrips(title):
    before = assemble([row(CV)])
    current = before.values.model_copy(update={"title": title})
    result = assemble(
        [row(CV, title="Replacement")], current=current, previous=before, replace_managed=True
    )
    assert result.values.title == title
    assert origin(result, "title").user_override
    again = assemble([row(CV)], current=result.values, previous=result, replace_managed=True)
    assert again.values.title == title
    assert origin(again, "title").user_override


def test_unknown_local_values_and_explicit_empty_overrides_are_not_provider_managed():
    result = assemble(
        [row(CV)],
        current=MetadataValues(title="Legacy title", description=""),
        overrides=frozenset({"description"}),
        replace_managed=True,
    )
    assert result.values.title == "Legacy title"
    assert result.values.description == ""
    assert origin(result, "description").user_override
    with pytest.raises(MetadataAssemblyError):
        assemble([], overrides=frozenset({"file_path"}))


def test_lower_priority_failure_fallback_cannot_replace_higher_priority_existing_fields():
    before = assemble([row(CV)])
    result = assemble(
        [row(METRON, title="Fallback")],
        current=before.values,
        previous=before,
        replace_managed=True,
    )
    assert result.values.title == before.values.title
    reordered = [runtime(CV, priority=90).policy, runtime(METRON, priority=1).policy]
    result = assemble(
        [row(METRON, title="New authority")],
        policies=reordered,
        current=before.values,
        previous=before,
        replace_managed=True,
    )
    assert result.values.title == "New authority"


def test_missing_provider_values_do_not_erase_and_zero_is_not_missing():
    before = assemble([row(CV).model_copy(update={"issue_count": 12, "description": "Saved"})])
    result = assemble(
        [row(CV).model_copy(update={"issue_count": 0, "description": "  "})],
        current=before.values,
        previous=before,
        replace_managed=True,
    )
    assert result.values.description == "Saved"
    assert result.values.issue_count == 0
    assert origin(result, "issue_count").domain is Domain.ISSUES


@pytest.mark.parametrize("source,identifier", [(METRON, "43"), (Source.GCD_LOCAL, "42")])
def test_unverified_native_identity_cannot_enrich_same_title(source, identifier):
    with pytest.raises(MetadataAssemblyError):
        assemble([row(source, external_id=identifier)])


def test_crosswalk_conflict_stops_all_domains_even_for_lower_priority_source():
    bad = row(METRON).model_copy(update={"cross_identities": [identity(CV, identifier="99")]})
    with pytest.raises(MetadataAssemblyError):
        assemble([row(CV), bad])


def test_new_crosswalk_is_observation_not_verified_snapshot_identity():
    observed = identity(Source.GCD_LOCAL, identifier="7")
    result = assemble([row(METRON).model_copy(update={"cross_identities": [observed]})])
    assert observed not in result.identities
    assert result.observed_identities == (observed,)


def test_disabled_and_unconfigured_candidate_policies_cannot_supply_values():
    for policies in ([], [runtime(METRON, enabled=False).policy]):
        with pytest.raises(MetadataAssemblyError):
            assemble([row(METRON)], policies=policies)


def test_issue_requires_independent_parent_proof_and_preserves_lettered_number():
    member = ProviderIssueRead(
        source=METRON,
        identity_namespace=METRON.identity_namespace,
        external_id="7",
        series_external_id="42",
        issue_number_text="50-x",
        cover_date=date(2026, 9, 1),
    )
    args = (Kind.ISSUE, [identity(METRON, Kind.ISSUE, "7")], [member], [runtime(METRON).policy])
    with pytest.raises(MetadataAssemblyError):
        assemble_metadata(*args, now=NOW)
    result = assemble_metadata(*args, now=NOW, parent_identities=[identity(METRON)])
    assert result.values.issue_number_text == "50-x"
    assert result.values.cover_date == date(2026, 9, 1)
    assert result == MetadataSnapshot.model_validate_json(result.model_dump_json())


def test_story_arc_core_uses_story_arc_authority_not_series_core_priority():
    arcs = [
        ProviderStoryArcRead(
            source=s, identity_namespace=s.identity_namespace, external_id="42", title=s.value
        )
        for s in (CV, METRON)
    ]
    result = assemble_metadata(
        Kind.STORY_ARC,
        [identity(s, Kind.STORY_ARC) for s in (CV, METRON)],
        arcs,
        [runtime(CV).policy, runtime(METRON, domain_priorities={Domain.STORY_ARCS: 0}).policy],
        now=NOW,
    )
    assert result.values.title == METRON.value


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "https://user:password@metron.cloud/a.jpg",
        "https://127.0.0.1/a.jpg",
        "https://metron.cloud/a?token=private",
    ],
)
def test_unsafe_artwork_is_diagnostic_not_snapshot_data(url):
    snapshot = assemble([row(METRON).model_copy(update={"image_url": url})])
    assert snapshot.values.image_url is None
    assert snapshot.diagnostics == ("metron_api:invalid_artwork_url",)
    assert url not in snapshot.model_dump_json()


def test_snapshot_rejects_other_entity_baseline_and_naive_clock():
    before = assemble([row(CV)])
    with pytest.raises(MetadataAssemblyError):
        assemble(
            [], previous=before.model_copy(update={"identities": (identity(CV, identifier="99"),)})
        )
    with pytest.raises(MetadataAssemblyError):
        assemble_metadata(Kind.SERIES, [identity(CV)], [], [], now=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        before.values.title = "Mutated"


def test_identity_kind_and_namespace_are_not_interchangeable():
    bad = row(METRON).model_copy(update={"identity_namespace": IdentityNamespace.COMICVINE})
    with pytest.raises(MetadataAssemblyError):
        assemble([bad])


@pytest.mark.parametrize(
    "change", ["duplicate_origin", "unknown_field", "wrong_domain", "wrong_kind"]
)
def test_saved_snapshot_rejects_inconsistent_provenance(change):
    saved = assemble([row(CV)]).model_dump(mode="json")
    if change == "duplicate_origin":
        saved["origins"].append(saved["origins"][0])
    elif change == "unknown_field":
        saved["origins"][0]["field"] = "path"
    elif change == "wrong_domain":
        saved["origins"][0]["domain"] = "artwork"
    else:
        saved["identities"][0]["entity_kind"] = "issue"
    with pytest.raises(ValidationError):
        MetadataSnapshot.model_validate(saved)


def test_untrusted_local_artwork_does_not_reach_canonical_outputs():
    with pytest.raises(MetadataAssemblyError):
        assemble([], current=MetadataValues(image_url="file:///private"))


def test_conflicting_issue_numbers_stop_assembly_despite_shared_verified_parent():
    candidates = [
        ProviderIssueRead(
            source=s,
            identity_namespace=s.identity_namespace,
            external_id="7",
            series_external_id="42",
            issue_number_text=number,
        )
        for s, number in ((CV, "50-x"), (METRON, "50-o"))
    ]
    with pytest.raises(MetadataAssemblyError):
        assemble_metadata(
            Kind.ISSUE,
            [identity(s, Kind.ISSUE, "7") for s in (CV, METRON)],
            candidates,
            [runtime(s).policy for s in (CV, METRON)],
            now=NOW,
            parent_identities=[identity(s) for s in (CV, METRON)],
        )


@pytest.mark.parametrize("field,value", [("publisher", "p" * 256), ("image_url", "u" * 501)])
def test_canonical_values_fit_database_text_columns_on_both_backends(field, value):
    with pytest.raises(ValidationError):
        MetadataValues.model_validate({field: value})
