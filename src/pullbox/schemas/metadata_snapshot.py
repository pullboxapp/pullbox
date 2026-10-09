"""Canonical descriptive values and their provenance, independent of output format."""

from datetime import date
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
    MetadataEntityKind,
    MetadataSource,
)
from pullbox.schemas.metadata_credits import MetadataCredits
from pullbox.schemas.metadata_sources import MetadataDomain

type EmbeddedDocument = Literal["ComicInfo.xml", "MetronInfo.xml"]


def field_domain(kind: MetadataEntityKind, field: str) -> MetadataDomain:
    if field == "image_url":
        return MetadataDomain.ARTWORK
    if kind is MetadataEntityKind.STORY_ARC:
        return MetadataDomain.STORY_ARCS
    if field == "issue_count":
        return MetadataDomain.ISSUES
    return MetadataDomain.CORE


class MetadataValues(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = Field(default=None, max_length=500)
    sort_title: str | None = Field(default=None, max_length=500)
    publisher: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=200000)
    year_start: int | None = Field(default=None, ge=1, le=9999, strict=True)
    year_end: int | None = Field(default=None, ge=1, le=9999, strict=True)
    volume: str | None = Field(default=None, max_length=100)
    series_type: str | None = Field(default=None, max_length=50)
    status: str | None = Field(default=None, max_length=50)
    language: str | None = Field(default=None, max_length=100)
    issue_count: int | None = Field(default=None, ge=0, le=1000000, strict=True)
    issue_number_text: str | None = Field(default=None, max_length=320)
    cover_date: date | None = None
    store_date: date | None = None
    page_count: int | None = Field(default=None, ge=0, le=1000000, strict=True)
    image_url: str | None = Field(default=None, max_length=500)
    credits: MetadataCredits | None = None


class PassiveReleaseOrigin(BaseModel):
    """Cached calendar context, not an executable metadata source or issue identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    locg_series_id: str
    release_ids: tuple[str, ...] = Field(min_length=1, max_length=10000)
    fetched_at: AwareDatetime
    issue_identity: ExternalIdentityRef | None = None
    issue_number_text: str | None = None
    matched_store_date: date | None = None
    match_kind: Literal["exact_issue", "number_date"] | None = None

    @model_validator(mode="after")
    def exact_release_context(self) -> Self:
        if (
            ExternalIdentityRef(
                IdentityNamespace.LOCG, MetadataEntityKind.SERIES, self.locg_series_id
            ).external_id
            != self.locg_series_id
            or len(set(self.release_ids)) != len(self.release_ids)
            or any(
                ExternalIdentityRef(
                    IdentityNamespace.LOCG, MetadataEntityKind.ISSUE, identifier
                ).external_id
                != identifier
                for identifier in self.release_ids
            )
        ):
            raise ValueError("Passive release provenance requires exact, distinct IDs")
        proof = (
            self.issue_identity,
            self.issue_number_text,
            self.matched_store_date,
            self.match_kind,
        )
        if any(value is not None for value in proof) and (
            any(value is None for value in proof)
            or self.issue_identity is None
            or self.issue_identity.entity_kind is not MetadataEntityKind.ISSUE
            or self.issue_identity.namespace is IdentityNamespace.LOCG
            or normalize_issue_number_text(self.issue_number_text or "") != self.issue_number_text
        ):
            raise ValueError("Passive issue provenance requires a complete native issue proof")
        return self


class FieldOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    domain: MetadataDomain
    source: MetadataSource | None = None
    source_updated_at: AwareDatetime | None = None
    observed_at: AwareDatetime
    user_override: bool = False
    derivation: Literal["classification", "lifecycle", "catalog", "normalization"] | None = None
    embedded_documents: tuple[EmbeddedDocument, ...] = ()
    passive_release: PassiveReleaseOrigin | None = None


class MetadataSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    entity_kind: MetadataEntityKind
    identities: tuple[ExternalIdentityRef, ...]
    values: MetadataValues
    origins: tuple[FieldOrigin, ...] = ()
    observed_identities: tuple[ExternalIdentityRef, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def consistent_provenance(self) -> Self:
        identities: dict[str, ExternalIdentityRef] = {}
        for identity in (*self.identities, *self.observed_identities):
            if identity.entity_kind is not self.entity_kind or (
                identity.namespace in identities and identities[identity.namespace] != identity
            ):
                raise ValueError("Snapshot identities disagree")
            identities[identity.namespace] = identity
        fields = set()
        for origin in self.origins:
            if (
                origin.field not in MetadataValues.model_fields
                or origin.field in fields
                or origin.domain is not field_domain(self.entity_kind, origin.field)
                or (origin.user_override and origin.source is not None)
                or (
                    origin.embedded_documents
                    and (
                        len(set(origin.embedded_documents)) != len(origin.embedded_documents)
                        or origin.source is not None
                        or origin.source_updated_at is not None
                        or origin.user_override
                        or origin.derivation is not None
                    )
                )
                or (
                    origin.derivation is not None
                    and (origin.source is not None or origin.user_override)
                )
                or (
                    origin.passive_release is not None
                    and (
                        not self._passive_origin_matches(origin)
                        or origin.source is not None
                        or origin.source_updated_at is not None
                        or origin.user_override
                        or origin.derivation is not None
                        or origin.embedded_documents
                    )
                )
                or (
                    origin.source is not None
                    and not any(
                        identity.namespace is origin.source.identity_namespace
                        for identity in self.identities
                    )
                )
            ):
                raise ValueError("Snapshot field provenance is inconsistent")
            fields.add(origin.field)
        return self

    def _passive_origin_matches(self, origin: FieldOrigin) -> bool:
        proof = origin.passive_release
        assert proof is not None
        if self.entity_kind is MetadataEntityKind.SERIES:
            return (
                proof.issue_identity is None
                and origin.field in {"publisher", "year_start", "volume"}
                and any(
                    identity.namespace is IdentityNamespace.LOCG
                    and identity.external_id == proof.locg_series_id
                    for identity in self.identities
                )
            )
        return (
            self.entity_kind is MetadataEntityKind.ISSUE
            and origin.field == "store_date"
            and proof.issue_identity is not None
            and proof.issue_identity in self.identities
            and proof.issue_number_text == self.values.issue_number_text
            and proof.matched_store_date == self.values.store_date
        )
