"""Creator role mapping helpers for ComicInfo.xml payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.models.creator import Creator, IssueCreator

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession


_CREATOR_ROLE_FIELD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Writer", ("writer", "script", "story")),
    ("Penciller", ("penciller", "penciler", "pencil", "artist")),
    ("Inker", ("inker", "inks")),
    ("Colorist", ("colorist", "colourist", "color", "colour")),
    ("Letterer", ("letterer", "letters")),
    ("CoverArtist", ("cover",)),
    ("Editor", ("editor",)),
)


def creator_roles_to_comicinfo_fields(
    creator_roles: Iterable[tuple[str | None, str | None]],
) -> dict[str, str]:
    """Map Pullbox creator roles onto ComicInfo creator fields."""
    grouped: dict[str, list[str]] = {field: [] for field, _tokens in _CREATOR_ROLE_FIELD_RULES}
    seen: dict[str, set[str]] = {field: set() for field in grouped}

    for name, role in creator_roles:
        normalized_name = (name or "").strip()
        normalized_role = (role or "").strip().lower()
        if not normalized_name or not normalized_role:
            continue

        for field, tokens in _CREATOR_ROLE_FIELD_RULES:
            eligible_role = (
                ", ".join(role for role in normalized_role.split(",") if "cover" not in role)
                if field == "Penciller"
                else normalized_role
            )
            if any(token in eligible_role for token in tokens):
                dedupe_key = normalized_name.casefold()
                if dedupe_key not in seen[field]:
                    grouped[field].append(normalized_name)
                    seen[field].add(dedupe_key)

    return {field: ", ".join(names) for field, names in grouped.items() if names}


async def load_comicinfo_creator_fields(
    session: AsyncSession,
    issue_id: int | None,
) -> dict[str, str]:
    """Load creator fields for an issue without touching async-lazy relationships."""
    if issue_id is None:
        return {}

    result = await session.execute(
        select(Creator.name, IssueCreator.role)
        .join(IssueCreator, IssueCreator.creator_id == Creator.id)
        .where(IssueCreator.issue_id == issue_id)
        .order_by(Creator.name.asc(), IssueCreator.role.asc())
    )
    creator_roles = [(name, role) for name, role in result.all()]
    return creator_roles_to_comicinfo_fields(creator_roles)
