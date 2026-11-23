"""Descriptive arc entries embedded in an issue, not tracked arc memberships."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, field_validator


class IssueArcEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=500)
    position: int | None = Field(default=None, strict=True, ge=1, le=9999999999)

    @field_validator("name")
    @classmethod
    def representable_name(cls, value: str) -> str:
        if not value.isprintable() or "," in value or value != value.strip():
            raise ValueError("Use a printable name without commas or surrounding whitespace")
        return value


def _distinct(entries: tuple[IssueArcEntry, ...]) -> tuple[IssueArcEntry, ...]:
    if len({e.name.casefold() for e in entries}) != len(entries):
        raise ValueError("Use each arc name only once")
    return entries


type IssueArcList = Annotated[
    tuple[IssueArcEntry, ...], Field(min_length=1, max_length=128), AfterValidator(_distinct)
]
ARC_LIST: TypeAdapter[tuple[IssueArcEntry, ...]] = TypeAdapter(IssueArcList)


def comicinfo_arcs(entries: tuple[IssueArcEntry, ...]) -> tuple[IssueArcEntry, ...]:
    if all(e.position is not None for e in entries):
        return entries
    return tuple(IssueArcEntry(name=e.name) for e in entries)
