"""Optional metadata capability for explicit provider story-arc membership.

Membership completeness describes the received provider list, not bibliographic
completeness or a curated reading order. Consumers must hydrate every member ID
successfully before publishing a resolved arc.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pullbox.providers.base import IssueMetadata

MAX_STORY_ARC_MEMBERS = 5_000


@dataclass(frozen=True)
class StoryArcSearchResult:
    """An arc search result; an unavailable or unreliable count remains unknown."""

    provider_id: str
    title: str
    description: str | None = None
    publisher: str | None = None
    cover_url: str | None = None
    comicvine_url: str | None = None
    declared_issue_count: int | None = None


@dataclass(frozen=True)
class StoryArcMetadata:
    """Provider membership in response order, never claimed to be curated order."""

    provider_id: str
    title: str
    issue_provider_ids: tuple[str, ...]
    description: str | None = None
    publisher: str | None = None
    cover_url: str | None = None
    comicvine_url: str | None = None
    declared_issue_count: int | None = None
    order_basis: Literal["response_order"] = "response_order"
    membership_complete: bool = False
    warnings: tuple[str, ...] = ()


@runtime_checkable
class StoryArcMetadataProvider(Protocol):
    """Optional capability; ordinary MetadataProvider implementations need not implement it."""

    async def search_story_arcs(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> list[StoryArcSearchResult]:
        """Search one bounded page of provider arc names."""
        ...

    async def search_story_arcs_page(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[StoryArcSearchResult], int]:
        """Return a page and the provider's total number of matching arcs."""
        ...

    async def get_story_arc(self, provider_id: str) -> StoryArcMetadata:
        """Read an explicit member list without inventing a reading order."""
        ...

    async def get_story_arc_issues(self, issue_provider_ids: Sequence[str]) -> list[IssueMetadata]:
        """Hydrate the exact unique member set, returning it in requested order."""
        ...
