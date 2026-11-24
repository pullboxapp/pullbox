"""Localized publication titles, independent of matching aliases and resource IDs."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from pullbox.core.comicinfo_text import descriptive_text


class SeriesPublicationTitle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=500)
    language: str = Field(pattern=r"^[a-z]{2}$")

    @field_validator("name")
    @classmethod
    def printable_title(cls, name: str) -> str:
        if not name.isprintable() or descriptive_text(name) != name:
            raise ValueError("Enter a printable title without surrounding whitespace.")
        return name


def _distinct(entries: tuple[SeriesPublicationTitle, ...]) -> tuple[SeriesPublicationTitle, ...]:
    if len({(entry.name.casefold(), entry.language) for entry in entries}) != len(entries):
        raise ValueError("Use each publication title only once per language.")
    return entries


type SeriesPublicationTitles = Annotated[
    tuple[SeriesPublicationTitle, ...],
    Field(min_length=1, max_length=128),
    AfterValidator(_distinct),
]
