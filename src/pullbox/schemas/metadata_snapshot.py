"""Canonical descriptive values and their provenance, independent of output format."""

from datetime import date
from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from pullbox.core.comicinfo_text import (
    ColorFlag,
    MangaFlag,
    descriptive_text,
    selectable_single_line,
)
from pullbox.core.issue_community_rating import RATING_HELP, shared_score
from pullbox.core.issue_notes import NOTES_LIMIT, safe_notes_prose
from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
    MetadataEntityKind,
    MetadataSource,
)
from pullbox.core.metroninfo import MAX_METRONINFO_FIELD_LENGTH
from pullbox.schemas.metadata_credits import MetadataCredits
from pullbox.schemas.metadata_issue_arcs import IssueArcList
from pullbox.schemas.metadata_labels import IssueLabels, ReprintLabels
from pullbox.schemas.metadata_sources import MetadataDomain

type EmbeddedDocument = Literal["ComicInfo.xml", "MetronInfo.xml"]


class SharedAgeRating(StrEnum):
    """Exact labels shared by ComicInfo 2.0 and MetronInfo 1.1."""

    UNKNOWN = "Unknown"
    EVERYONE = "Everyone"
    TEEN = "Teen"


def field_domain(kind: MetadataEntityKind, field: str) -> MetadataDomain:
    if field == "image_url":
        return MetadataDomain.ARTWORK
    if kind is MetadataEntityKind.STORY_ARC:
        return MetadataDomain.STORY_ARCS
    if field == "issue_count":
        return MetadataDomain.ISSUES
    return MetadataDomain.CORE


def selectable_imprint(value: object) -> bool:
    return value is None or (
        isinstance(value, str)
        and 0 < len(value) <= 255
        and value.isprintable()
        and value == value.strip()
    )


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
    # These embedded formats describe publication, not a library classification.
    archive_format: Literal["Limited Series", "Digital Chapter"] | None = None
    status: str | None = Field(default=None, max_length=50)
    language: str | None = Field(default=None, max_length=100)
    issue_count: int | None = Field(default=None, ge=0, le=1000000, strict=True)
    issue_number_text: str | None = Field(default=None, max_length=320)
    cover_date: date | None = None
    store_date: date | None = None
    page_count: int | None = Field(default=None, ge=0, le=1000000, strict=True)
    age_rating: SharedAgeRating | None = Field(default=None, exclude_if=lambda v: v is None)
    community_rating: str | None = Field(default=None, exclude_if=lambda v: v is None)
    tags: IssueLabels | None = Field(default=None, exclude_if=lambda v: v is None)
    story_arcs: IssueArcList | None = Field(default=None, exclude_if=lambda v: v is None)
    imprint: str | None = Field(default=None, max_length=255, exclude_if=lambda v: v is None)
    notes: str | None = Field(default=None, max_length=NOTES_LIMIT, exclude_if=lambda v: v is None)
    review: str | None = Field(default=None, exclude_if=lambda v: v is None)
    main_character_or_team: str | None = Field(default=None, exclude_if=lambda v: v is None)
    series_groups: IssueLabels | None = Field(default=None, exclude_if=lambda v: v is None)
    alternate_series: str | None = Field(default=None, exclude_if=lambda v: v is None)
    alternate_number: str | None = Field(default=None, exclude_if=lambda v: v is None)
    alternative_number: str | None = Field(default=None, exclude_if=lambda v: v is None)
    collection_title: str | None = Field(default=None, exclude_if=lambda v: v is None)
    reprints: ReprintLabels | None = Field(default=None, exclude_if=lambda v: v is None)
    manga_volume: str | None = Field(default=None, exclude_if=lambda v: v is None)
    alternate_count: int | None = Field(
        default=None, ge=-1, le=2147483647, strict=True, exclude_if=lambda v: v is None
    )
    black_and_white: ColorFlag | None = Field(default=None, exclude_if=lambda v: v is None)
    manga: MangaFlag | None = Field(default=None, exclude_if=lambda v: v is None)
    scan_information: str | None = Field(default=None, exclude_if=lambda v: v is None)
    genres: IssueLabels | None = Field(default=None, exclude_if=lambda v: v is None)
    characters: IssueLabels | None = Field(default=None, exclude_if=lambda v: v is None)
    teams: IssueLabels | None = Field(default=None, exclude_if=lambda v: v is None)
    locations: IssueLabels | None = Field(default=None, exclude_if=lambda v: v is None)
    image_url: str | None = Field(default=None, max_length=500)
    credits: MetadataCredits | None = None

    @field_validator("review", "scan_information")
    @classmethod
    def comicinfo_text(cls, value: str | None) -> str | None:
        return descriptive_text(value) if value is not None else None

    @field_validator(
        "main_character_or_team", "alternate_series", "alternate_number", "alternative_number"
    )
    @classmethod
    def single_name(cls, value: str | None) -> str | None:
        name = descriptive_text(value) if value is not None else None
        if not selectable_single_line(name):
            raise ValueError("Use one printable name of up to 255 characters.")
        return name

    @field_validator("collection_title", "manga_volume")
    @classmethod
    def metron_edition_text(cls, value: str | None) -> str | None:
        label = descriptive_text(value) if value is not None else None
        if not selectable_single_line(label, limit=MAX_METRONINFO_FIELD_LENGTH):
            raise ValueError("Use one printable line within the MetronInfo field limit.")
        return label

    @field_validator("series_groups")
    @classmethod
    def descriptive_groups(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        for name in values or ():
            descriptive_text(name)
        return values

    @field_validator("community_rating")
    @classmethod
    def canonical_score(cls, value: str | None) -> str | None:
        if value is not None and (normalized := shared_score(value)) is not None:
            return normalized
        if value is not None:
            raise ValueError(RATING_HELP)
        return None

    @field_validator("notes")
    @classmethod
    def notes_are_prose(cls, value: str | None) -> str | None:
        if value is not None and (
            not value or value.strip() != value or not safe_notes_prose(value)
        ):
            raise ValueError("Notes must contain only descriptive text, without identity markers")
        return value

    @field_validator("imprint")
    @classmethod
    def printable_imprint(cls, value: str | None) -> str | None:
        if not selectable_imprint(value):
            raise ValueError("Use a printable imprint name without surrounding whitespace")
        return value


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
    provider_managed: bool = Field(default=False, exclude_if=lambda value: not value)
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
                    origin.provider_managed
                    and (
                        origin.user_override
                        or origin.source is not None
                        or origin.embedded_documents
                        or origin.passive_release is not None
                        or origin.derivation is not None
                    )
                )
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
