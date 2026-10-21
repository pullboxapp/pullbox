"""An override reset is explicit; ordinary stored provenance retains its shape."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pullbox.core.metadata_identity import MetadataEntityKind, MetadataSource
from pullbox.schemas.metadata_snapshot import FieldOrigin, MetadataSnapshot
from pullbox.schemas.metadata_sources import MetadataDomain


def origin(**kwargs):
    return FieldOrigin(
        field="title", domain=MetadataDomain.CORE, observed_at=datetime.now(UTC), **kwargs
    )


def test_ordinary_origins_do_not_gain_a_new_serialized_flag():
    assert "provider_managed" not in origin(user_override=True).model_dump()
    assert origin(provider_managed=True).model_dump()["provider_managed"] is True


@pytest.mark.parametrize(
    "other",
    [
        {"user_override": True},
        {"source": MetadataSource.METRON_API},
        {"embedded_documents": ("ComicInfo.xml",)},
        {"derivation": "normalization"},
    ],
)
def test_reset_cannot_claim_competing_ownership(other):
    with pytest.raises(ValidationError):
        MetadataSnapshot(
            entity_kind=MetadataEntityKind.ISSUE,
            origins=(origin(provider_managed=True, **other),),
        )
