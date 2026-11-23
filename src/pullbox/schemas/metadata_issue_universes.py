"""Descriptive Metron universe pairs. Resource IDs never come from editor input."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter

from pullbox.core.metroninfo import MAX_METRONINFO_FIELD_LENGTH


def _printable(value: str) -> str:
    if value != value.strip() or not value.isprintable():
        raise ValueError("Use one printable line without surrounding whitespace")
    return value


type UniverseText = Annotated[
    str, Field(min_length=1, max_length=MAX_METRONINFO_FIELD_LENGTH), AfterValidator(_printable)
]


class IssueUniverseEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: UniverseText
    designation: UniverseText | None = None

    def label(self) -> str:
        return f"{self.name} (designation: {self.designation})" if self.designation else self.name


def _distinct(entries: tuple[IssueUniverseEntry, ...]) -> tuple[IssueUniverseEntry, ...]:
    if len({(e.name.casefold(), (e.designation or "").casefold()) for e in entries}) != len(
        entries
    ):
        raise ValueError("Use each universe name and designation pair only once")
    return entries


type IssueUniverseList = Annotated[
    tuple[IssueUniverseEntry, ...], Field(min_length=1, max_length=128), AfterValidator(_distinct)
]
UNIVERSE_LIST: TypeAdapter[tuple[IssueUniverseEntry, ...]] = TypeAdapter(IssueUniverseList)
