"""Bounded descriptive edits; identifiers and series-wide values are not editable."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pullbox.schemas.metadata_issue_arcs import IssueArcEntry, IssueArcList

type EditorConflictField = Literal[
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

    source: Literal["library", "ComicInfo.xml", "MetronInfo.xml", "custom", "clear", "reset"]
    value: Annotated[str, Field(max_length=200000)] | IssueArcList | None = None

    @model_validator(mode="after")
    def explicit_custom_value(self) -> Self:
        if (self.source == "custom") != (self.value is not None):
            raise ValueError("Only a custom correction accepts a value. Use Clear to remove one.")
        return self


class IssueMetadataEditorReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edits: dict[EditableIssueField, IssueMetadataEdit] = Field(default_factory=dict, max_length=22)

    @model_validator(mode="after")
    def matching_input_types(self) -> Self:
        for field, edit in self.edits.items():
            if edit.source == "custom" and (field == "story_arcs") != isinstance(edit.value, tuple):
                raise ValueError("Enter arc names and optional positive positions as pairs")
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
    input_type: Literal["text", "textarea", "date", "number", "credits", "arcs"] = "text"
    arc_draft: tuple[IssueArcEntry, ...] = ()
    allowed_values: tuple[str, ...] = ()
    options: list[IssueMetadataEditorOption] = Field(default_factory=list)
    overridden: bool = False
    differs: bool = False
    selected: bool = False
    origin: str = ""
    xml_conflict_key: str | None = None
