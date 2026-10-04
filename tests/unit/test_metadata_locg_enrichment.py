"""Passive field evidence is explicit, bounded and cannot masquerade as a provider."""

from datetime import UTC, datetime

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
from pullbox.services.metadata_locg_enrichment import SeriesReleaseFacts, enrich_series_snapshot

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
