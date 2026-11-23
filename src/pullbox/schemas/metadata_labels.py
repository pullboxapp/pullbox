"""Bounded issue label lists and their format-specific separators."""

from typing import Annotated

from pydantic import AfterValidator, Field, TypeAdapter, ValidationError

from pullbox.core.metroninfo import MAX_METRONINFO_FIELD_LENGTH


def _distinct_lines(values: tuple[str, ...]) -> tuple[str, ...]:
    if any(not v.isprintable() or v != v.strip() for v in values) or len(
        {v.casefold() for v in values}
    ) != len(values):
        raise ValueError("Use distinct printable labels, one per line")
    return values


def _labels(values: tuple[str, ...]) -> tuple[str, ...]:
    if any("," in v for v in values):
        raise ValueError("Use distinct printable labels without commas")
    return _distinct_lines(values)


type IssueLabels = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=255)], ...],
    Field(min_length=1, max_length=128),
    AfterValidator(_labels),
]
_LABELS: TypeAdapter[tuple[str, ...]] = TypeAdapter(IssueLabels)
type MetronTitleList = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=MAX_METRONINFO_FIELD_LENGTH)], ...],
    Field(min_length=1, max_length=128),
    AfterValidator(_distinct_lines),
]
_METRON_TITLES: TypeAdapter[tuple[str, ...]] = TypeAdapter(MetronTitleList)
METRON_TITLE_LISTS = {"reprints": "Reprints/Reprint", "story_titles": "Stories/Story"}

# ComicInfo stores a single CSV element; MetronInfo stores individual resources.
LABEL_XML_PATHS = {
    "genres": ("Genre", "Genres/Genre"),
    "tags": ("Tags", "Tags/Tag"),
    "characters": ("Characters", "Characters/Character"),
    "teams": ("Teams", "Teams/Team"),
    "locations": ("Locations", "Locations/Location"),
}


def selectable_labels(value: object, *, metron_titles: bool = False) -> bool:
    if value is None:
        return True
    try:
        (_METRON_TITLES if metron_titles else _LABELS).validate_python(value)
    except ValidationError:
        return False
    return True
