"""Passive field evidence is explicit, bounded and cannot masquerade as a provider."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from pullbox.core.metadata_identity import ExternalIdentityRef, IdentityNamespace
from pullbox.core.metadata_identity import MetadataEntityKind as Kind
from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.schemas.metadata_snapshot import (
    FieldOrigin,
    MetadataSnapshot,
    MetadataValues,
    PassiveReleaseOrigin,
)
from pullbox.schemas.metadata_sources import MetadataDomain
from pullbox.services.metadata_locg_enrichment import (
    IssueReleaseFacts,
    SeriesReleaseFacts,
    enrich_issue_snapshot,
    enrich_series_snapshot,
)

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
IDENTITY = ExternalIdentityRef(IdentityNamespace.LOCG, Kind.SERIES, "77")


def evidence(**fields):
    return PassiveReleaseOrigin(
        **{"locg_series_id": "77", "release_ids": ("1001", "1002"), "fetched_at": NOW, **fields}
    )


def snapshot(**fields):
    return MetadataSnapshot(
        **{
            "entity_kind": Kind.SERIES,
            "identities": (IDENTITY,),
            "values": MetadataValues(),
            **fields,
        }
    )


def test_enrichment_preserves_old_schema_and_serializes_exact_passive_evidence():
    old = snapshot()
    facts = SeriesReleaseFacts(MetadataValues(publisher="Calendar publisher"), evidence(), (), ())
    result = enrich_series_snapshot(old, facts, now=NOW)
    assert result.values.publisher == "Calendar publisher"
    assert old.values.publisher is None
    assert result.schema_version == 1
    assert result.identities == old.identities and result.observed_identities == ()
    assert MetadataSnapshot.model_validate_json(result.model_dump_json()) == result
    assert not hasattr(Source, "LOCG"), "cached release context is not an executable source"


@pytest.mark.parametrize(
    "attributes",
    [
        {"source": Source.METRON_API},
        {"source_updated_at": NOW},
        {"user_override": True},
        {"embedded_documents": ("ComicInfo.xml",)},
        {"derivation": "catalog"},
        {"field": "image_url", "domain": MetadataDomain.ARTWORK},
        {"field": "description"},
    ],
)
def test_passive_origin_cannot_claim_provider_override_artwork_or_embedded_provenance(attributes):
    with pytest.raises(ValidationError):
        snapshot(
            origins=(
                FieldOrigin(
                    **{
                        "field": "publisher",
                        "domain": MetadataDomain.CORE,
                        "observed_at": NOW,
                        "passive_release": evidence(),
                        **attributes,
                    }
                ),
            )
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"identities": ()},
        {"identities": (ExternalIdentityRef(IdentityNamespace.LOCG, Kind.SERIES, "78"),)},
        {
            "entity_kind": Kind.ISSUE,
            "identities": (ExternalIdentityRef(IdentityNamespace.LOCG, Kind.ISSUE, "77"),),
        },
    ],
)
def test_passive_origin_requires_the_same_confirmed_series_identity(fields):
    with pytest.raises(ValidationError):
        snapshot(
            **fields,
            origins=(
                FieldOrigin(
                    field="publisher",
                    domain=MetadataDomain.CORE,
                    observed_at=NOW,
                    passive_release=evidence(),
                ),
            ),
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"locg_series_id": "0"},
        {"locg_series_id": "077"},
        {"release_ids": ()},
        {"release_ids": ("1001", "1001")},
        {"release_ids": ("-1",)},
        {"fetched_at": datetime(2026, 10, 3)},
    ],
)
def test_invalid_release_provenance_is_rejected(fields):
    with pytest.raises((ValidationError, ValueError)):
        evidence(**fields)


@pytest.mark.parametrize("protected", ["user", "archive", "disagreement"])
def test_enrichment_never_fills_a_protected_blank(protected):
    origin = FieldOrigin(
        field="publisher",
        domain=MetadataDomain.CORE,
        observed_at=NOW,
        user_override=protected == "user",
        embedded_documents=("ComicInfo.xml",) if protected == "archive" else (),
    )
    before = snapshot(
        origins=(origin,),
        diagnostics=(
            ("archive:series:publisher:disagreement",) if protected == "disagreement" else ()
        ),
    )
    facts = SeriesReleaseFacts(MetadataValues(publisher="Calendar publisher"), evidence(), (), ())
    result = enrich_series_snapshot(before, facts, now=NOW)
    assert result == before


ISSUE_IDENTITY = ExternalIdentityRef(IdentityNamespace.METRON, Kind.ISSUE, "100")
DAY = date(2026, 9, 30)


def issue_evidence(**fields):
    return evidence(
        **{
            "issue_identity": ISSUE_IDENTITY,
            "issue_number_text": "50-X",
            "matched_store_date": DAY,
            "match_kind": "exact_issue",
            **fields,
        }
    )


def issue_snapshot(**fields):
    return snapshot(
        **{
            "entity_kind": Kind.ISSUE,
            "identities": (ISSUE_IDENTITY,),
            "values": MetadataValues(issue_number_text="50-X"),
            **fields,
        }
    )


@pytest.mark.parametrize("match_kind", ["exact_issue", "number_date"])
def test_issue_enrichment_retains_native_identity_and_round_trips_provenance(match_kind):
    before = issue_snapshot(values=MetadataValues(issue_number_text="50-X", cover_date=DAY))
    facts = IssueReleaseFacts(1, issue_evidence(match_kind=match_kind))
    result = enrich_issue_snapshot(before, facts, now=NOW)
    assert result.values.store_date == DAY
    assert result.values.cover_date == before.values.cover_date
    assert result.identities == before.identities
    assert result.observed_identities == ()
    assert MetadataSnapshot.model_validate_json(result.model_dump_json()) == result
    assert result.origins[0].passive_release == facts.origin


@pytest.mark.parametrize(
    "fields",
    [
        {"issue_identity": None},
        {"issue_number_text": None},
        {"matched_store_date": None},
        {"match_kind": None},
        {"issue_identity": IDENTITY},
        {"issue_identity": ExternalIdentityRef(IdentityNamespace.LOCG, Kind.ISSUE, "1001")},
        {"issue_number_text": "050-X"},
    ],
)
def test_passive_issue_proof_cannot_be_partial_or_use_a_variant_as_native_identity(fields):
    with pytest.raises((ValidationError, ValueError)):
        issue_evidence(**fields)


@pytest.mark.parametrize(
    "fields",
    [
        {"identities": ()},
        {"values": MetadataValues(issue_number_text="50-O", store_date=DAY)},
        {"values": MetadataValues(issue_number_text="50-X", store_date=date(2026, 10, 1))},
    ],
)
def test_issue_origin_must_match_the_exact_snapshot_identity_number_and_date(fields):
    with pytest.raises(ValidationError):
        issue_snapshot(
            **{
                "values": MetadataValues(issue_number_text="50-X", store_date=DAY),
                "origins": (
                    FieldOrigin(
                        field="store_date",
                        domain=MetadataDomain.CORE,
                        observed_at=NOW,
                        passive_release=issue_evidence(),
                    ),
                ),
                **fields,
            }
        )


@pytest.mark.parametrize("field", ["title", "cover_date", "price_amount", "issue_number_text"])
def test_issue_release_provenance_is_restricted_to_store_dates(field):
    with pytest.raises(ValidationError):
        issue_snapshot(
            values=MetadataValues(issue_number_text="50-X", store_date=DAY),
            origins=(
                FieldOrigin(
                    field=field,
                    domain=MetadataDomain.CORE,
                    observed_at=NOW,
                    passive_release=issue_evidence(),
                ),
            ),
        )
