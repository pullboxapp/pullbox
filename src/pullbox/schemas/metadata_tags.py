"""Issue tag labels which round-trip through both embedded formats."""

from typing import Annotated

from pydantic import AfterValidator, Field, TypeAdapter, ValidationError


def _labels(values: tuple[str, ...]) -> tuple[str, ...]:
    if any(not v.isprintable() or "," in v or v != v.strip() for v in values) or len(
        {v.casefold() for v in values}
    ) != len(values):
        raise ValueError("Use distinct printable tag labels without commas")
    return values


type IssueTags = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=255)], ...],
    Field(min_length=1, max_length=128),
    AfterValidator(_labels),
]
_TAGS: TypeAdapter[tuple[str, ...]] = TypeAdapter(IssueTags)


def selectable_tags(value: object) -> bool:
    if value is None:
        return True
    try:
        _TAGS.validate_python(value)
    except ValidationError:
        return False
    return True
