"""Bounded descriptive edits; identifiers and series-wide values are not editable."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

type EditableIssueField = Literal[
    "title", "description", "cover_date", "store_date", "page_count", "credits"
]


class IssueMetadataEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["library", "ComicInfo.xml", "MetronInfo.xml", "custom", "clear", "reset"]
    value: str | None = Field(default=None, max_length=200000)

    @model_validator(mode="after")
    def explicit_custom_value(self) -> Self:
        if (self.source == "custom") != (self.value is not None):
            raise ValueError("Only a custom correction accepts a value. Use Clear to remove one.")
        return self


class IssueMetadataEditorReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edits: dict[EditableIssueField, IssueMetadataEdit] = Field(default_factory=dict, max_length=6)


class IssueMetadataEditorApproval(IssueMetadataEditorReview):
    review_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class IssueMetadataEditorOption(BaseModel):
    source: str
    value: str


class IssueMetadataEditorField(BaseModel):
    key: str
    label: str
    current: str
    proposed: str
    editable: bool = False
    reason: str = ""
    input_type: Literal["text", "textarea", "date", "number", "credits"] = "text"
    options: list[IssueMetadataEditorOption] = Field(default_factory=list)
    overridden: bool = False
    differs: bool = False
    selected: bool = False
    origin: str = ""
