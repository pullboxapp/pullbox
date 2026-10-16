"""Approval for writing the current canonical pair to one managed comic."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type FileMetadataSource = Literal["library", "ComicInfo.xml", "MetronInfo.xml"]
type FileMetadataField = Literal[
    "series.title",
    "series.sort_title",
    "series.publisher",
    "series.language",
    "series.year_start",
    "series.volume",
    "series.series_type",
    "series.issue_count",
    "issue.title",
    "issue.description",
    "issue.cover_date",
    "issue.store_date",
    "issue.page_count",
    "issue.credits",
]
type FileMetadataChoices = dict[FileMetadataField, FileMetadataSource]


class FileMetadataReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choices: FileMetadataChoices = Field(default_factory=dict, max_length=14)


class FileMetadataOption(BaseModel):
    source: FileMetadataSource
    value: str
    selectable: bool = True


class FileMetadataConflict(BaseModel):
    key: FileMetadataField
    label: str
    options: list[FileMetadataOption]
    selected: FileMetadataSource | None = None
    note: str = ""


class FileMetadataChange(BaseModel):
    document: str
    field: str
    before: str | None
    after: str | None


class FileMetadataPreview(BaseModel):
    file_id: int
    file_name: str
    documents: tuple[str, str] = ("ComicInfo.xml", "MetronInfo.xml")
    changes: list[FileMetadataChange]
    review_key: str
    unchanged: bool
    ready: bool = True
    conflicts: list[FileMetadataConflict] = Field(default_factory=list)
    converts_to_cbz: bool = False


class FileMetadataApproval(FileMetadataReview):
    review_key: str = Field(pattern=r"^[a-f0-9]{64}$")
