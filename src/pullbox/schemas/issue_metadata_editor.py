"""Bounded descriptive edits; identifiers and series-wide values are not editable."""

from typing import Annotated, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pullbox.schemas.metadata_issue_arcs import IssueArcEntry, IssueArcList
from pullbox.schemas.metadata_issue_links import IssueWebLink, IssueWebLinks
from pullbox.schemas.metadata_issue_prices import IssuePriceEntry, IssuePriceList
from pullbox.schemas.metadata_issue_universes import (
    UNIVERSE_LIST,
    IssueUniverseEntry,
    IssueUniverseList,
)

type EditorConflictField = Literal[
    "xml.Web",
    "xml.ISBN",
    "xml.UPC",
    "xml.GTIN",
    "xml.Prices",
    "xml.Stories",
    "xml.Universes",
    "xml.Reprints",
    "xml.CollectionTitle",
    "xml.MangaVolume",
    "xml.AlternateSeries",
    "xml.AlternateNumber",
    "xml.AlternateCount",
    "xml.AlternativeNumber",
    "xml.MainCharacterOrTeam",
    "xml.SeriesGroup",
    "xml.BlackAndWhite",
    "xml.Manga",
    "xml.Review",
    "xml.ScanInformation",
    "xml.CommunityRating",
    "xml.Notes",
    "xml.AgeRating",
    "xml.Imprint",
    "xml.Tags",
    "xml.Genre",
    "xml.Characters",
    "xml.Teams",
    "xml.Locations",
    "xml.StoryArc",
]

type EditableIssueField = Literal[
    "web_links",
    "isbn",
    "upc",
    "gtin",
    "prices",
    "story_titles",
    "universes",
    "reprints",
    "collection_title",
    "manga_volume",
    "alternate_series",
    "alternate_number",
    "alternate_count",
    "alternative_number",
    "main_character_or_team",
    "series_groups",
    "black_and_white",
    "manga",
    "review",
    "scan_information",
    "community_rating",
    "notes",
    "title",
    "description",
    "cover_date",
    "store_date",
    "page_count",
    "credits",
    "age_rating",
    "imprint",
    "tags",
    "genres",
    "characters",
    "teams",
    "locations",
    "story_arcs",
]
type EditorXmlField = Literal[
    "web_links",
    "isbn",
    "upc",
    "gtin",
    "prices",
    "story_titles",
    "universes",
    "reprints",
    "collection_title",
    "manga_volume",
    "alternate_series",
    "alternate_number",
    "alternate_count",
    "alternative_number",
    "main_character_or_team",
    "series_groups",
    "black_and_white",
    "manga",
    "review",
    "scan_information",
    "community_rating",
    "notes",
    "age_rating",
    "imprint",
    "tags",
    "genres",
    "characters",
    "teams",
    "locations",
    "story_arcs",
]
EDITOR_XML_FIELDS: dict[EditorXmlField, EditorConflictField] = {
    "web_links": "xml.Web",
    "isbn": "xml.ISBN",
    "upc": "xml.UPC",
    "gtin": "xml.GTIN",
    "prices": "xml.Prices",
    "story_titles": "xml.Stories",
    "universes": "xml.Universes",
    "reprints": "xml.Reprints",
    "collection_title": "xml.CollectionTitle",
    "manga_volume": "xml.MangaVolume",
    "alternate_series": "xml.AlternateSeries",
    "alternate_number": "xml.AlternateNumber",
    "alternate_count": "xml.AlternateCount",
    "alternative_number": "xml.AlternativeNumber",
    "main_character_or_team": "xml.MainCharacterOrTeam",
    "series_groups": "xml.SeriesGroup",
    "black_and_white": "xml.BlackAndWhite",
    "manga": "xml.Manga",
    "review": "xml.Review",
    "scan_information": "xml.ScanInformation",
    "community_rating": "xml.CommunityRating",
    "notes": "xml.Notes",
    "age_rating": "xml.AgeRating",
    "imprint": "xml.Imprint",
    "tags": "xml.Tags",
    "genres": "xml.Genre",
    "characters": "xml.Characters",
    "teams": "xml.Teams",
    "locations": "xml.Locations",
    "story_arcs": "xml.StoryArc",
}


class IssueMetadataEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal[
        "library", "ComicInfo.xml", "MetronInfo.xml", "custom", "clear", "reset", "isbn", "upc"
    ]
    value: (
        Annotated[str, Field(max_length=200000)]
        | IssueArcList
        | IssueUniverseList
        | IssuePriceList
        | IssueWebLinks
        | None
    ) = None

    @model_validator(mode="after")
    def explicit_custom_value(self) -> Self:
        if (self.source == "custom") != (self.value is not None):
            raise ValueError("Only a custom correction accepts a value. Use Clear to remove one.")
        return self


class IssueMetadataEditorReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edits: dict[EditableIssueField, IssueMetadataEdit] = Field(
        default_factory=dict, max_length=len(get_args(EditableIssueField.__value__))
    )

    @field_validator("edits", mode="before")
    @classmethod
    def bind_universe_rows(cls, edits: object) -> object:
        # A name-only row also fits an arc. Select the schema by field, not by guessing its shape.
        if (
            isinstance(edits, dict)
            and isinstance(edit := edits.get("universes"), dict)
            and edit.get("source") == "custom"
        ):
            rows = UNIVERSE_LIST.validate_python(edit.get("value"))
            return {**edits, "universes": {**edit, "value": rows}}
        return edits

    @model_validator(mode="after")
    def matching_input_types(self) -> Self:
        for field, edit in self.edits.items():
            if edit.source in ("isbn", "upc") and field != "gtin":
                raise ValueError("Only the ComicInfo barcode can use a Metron ISBN or UPC.")
            if edit.source != "custom":
                continue
            if field == "web_links":
                if not isinstance(edit.value, tuple) or not isinstance(edit.value[0], IssueWebLink):
                    raise ValueError("Enter reference links and an optional primary designation.")
            elif field == "prices":
                if not isinstance(edit.value, tuple) or not isinstance(
                    edit.value[0], IssuePriceEntry
                ):
                    raise ValueError(
                        "Enter the two-letter country code and decimal amount for each price"
                    )
            elif field == "universes":
                if not isinstance(edit.value, tuple) or not isinstance(
                    edit.value[0], IssueUniverseEntry
                ):
                    raise ValueError("Enter universe names and optional designations as pairs")
            elif field == "story_arcs":
                if not isinstance(edit.value, tuple) or not isinstance(
                    edit.value[0], IssueArcEntry
                ):
                    raise ValueError("Enter arc names and optional positive positions as pairs")
            elif isinstance(edit.value, tuple):
                raise ValueError("This field accepts a text correction, not a list of entries")
        return self

    @property
    def reviewed_xml_fields(self) -> frozenset[EditorXmlField]:
        return frozenset(
            field
            for field in EDITOR_XML_FIELDS
            if field in self.edits and self.edits[field].source != "reset"
        )


class IssueMetadataEditorApproval(IssueMetadataEditorReview):
    review_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class IssueMetadataEditorOption(BaseModel):
    source: str
    value: str
    selectable: bool = True


class IssueMetadataEditorField(BaseModel):
    key: str
    label: str
    current: str
    current_source_label: str | None = None
    proposed: str
    editable: bool = False
    reason: str = ""
    input_type: Literal[
        "text", "textarea", "date", "number", "credits", "arcs", "universes", "prices", "links"
    ] = "text"
    arc_draft: tuple[IssueArcEntry, ...] = ()
    universe_draft: tuple[IssueUniverseEntry, ...] = Field(default=(), exclude_if=lambda v: not v)
    price_draft: tuple[IssuePriceEntry, ...] = Field(default=(), exclude_if=lambda v: not v)
    link_draft: tuple[IssueWebLink, ...] = Field(default=(), exclude_if=lambda v: not v)
    primary_link_locked: bool = Field(default=False, exclude_if=lambda v: not v)
    reset_only: bool = Field(default=False, exclude_if=lambda v: not v)
    allowed_values: tuple[str, ...] = ()
    options: list[IssueMetadataEditorOption] = Field(default_factory=list)
    overridden: bool = False
    differs: bool = False
    selected: bool = False
    origin: str = ""
    xml_conflict_key: str | None = None
