"""Issue label lists that round-trip through both embedded formats."""

from typing import Annotated

from pydantic import AfterValidator, Field, TypeAdapter, ValidationError


def _labels(values: tuple[str, ...]) -> tuple[str, ...]:
    if any(not v.isprintable() or "," in v or v != v.strip() for v in values) or len(
        {v.casefold() for v in values}
    ) != len(values):
        raise ValueError("Use distinct printable labels without commas")
    return values


type IssueLabels = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=255)], ...],
    Field(min_length=1, max_length=128),
    AfterValidator(_labels),
]
_LABELS: TypeAdapter[tuple[str, ...]] = TypeAdapter(IssueLabels)

# ComicInfo stores a single CSV element; MetronInfo stores individual resources.
LABEL_XML_PATHS = {
    "genres": ("Genre", "Genres/Genre"),
    "tags": ("Tags", "Tags/Tag"),
    "characters": ("Characters", "Characters/Character"),
    "teams": ("Teams", "Teams/Team"),
    "locations": ("Locations", "Locations/Location"),
}


def selectable_labels(value: object) -> bool:
    if value is None:
        return True
    try:
        _LABELS.validate_python(value)
    except ValidationError:
        return False
    return True
