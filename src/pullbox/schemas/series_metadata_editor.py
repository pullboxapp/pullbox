"""Selected series corrections and explicit library/sidecar save scope."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from pullbox.schemas.series_sidecar import SeriesSidecarTargetRead

type EditableSeriesField = Literal[
    "title",
    "sort_title",
    "publisher",
    "description",
    "year_start",
    "year_end",
    "status",
    "series_type",
    "archive_format",
    "volume",
    "language",
]
type SeriesEditorMode = Literal["library_and_sidecars", "library_only"]


class SeriesMetadataEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["set", "clear", "reset", "library", "sidecar"]
    value: StrictStr | StrictInt | None = None
    source: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def action_payload(self) -> Self:
        if self.action == "set":
            if self.value is None or self.source is not None:
                raise ValueError("Set requires a custom value, not a saved source.")
        elif self.value is not None or ((self.action == "sidecar") != (self.source is not None)):
            raise ValueError("Saved source, clear and reset actions do not accept custom values.")
        return self


class SeriesMetadataEditorReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: SeriesEditorMode = "library_and_sidecars"
    edits: dict[EditableSeriesField, SeriesMetadataEdit] = Field(
        default_factory=dict, max_length=11
    )


class SeriesMetadataEditorApproval(SeriesMetadataEditorReview):
    review_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class SeriesMetadataEditorRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class SeriesMetadataEditorSource(BaseModel):
    token: str
    label: str
    value: str


class SeriesMetadataEditorOption(BaseModel):
    value: str
    label: str


class SeriesMetadataEditorField(BaseModel):
    key: str
    label: str
    current: str
    proposed: str
    editable: bool = True
    input_type: str = "text"
    overridden: bool = False
    selected: bool = False
    changed: bool = False
    can_clear: bool = True
    reason: str = ""
    options: list[SeriesMetadataEditorOption] = Field(default_factory=list)
    sources: list[SeriesMetadataEditorSource] = Field(default_factory=list)


class SeriesMetadataEditorPreview(BaseModel):
    series_id: int
    mode: SeriesEditorMode
    editor_fields: list[SeriesMetadataEditorField]
    editor_changed: bool
    targets: list[SeriesSidecarTargetRead]
    ready: bool
    review_key: str
    warnings: list[str] = Field(default_factory=list)
