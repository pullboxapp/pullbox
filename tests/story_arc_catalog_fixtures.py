"""Deterministic provider data shared by catalog route and browser tests."""

from dataclasses import replace

from pullbox.providers.base import IssueMetadata, SeriesMetadata
from pullbox.providers.metadata.comicvine import ComicVineError
from pullbox.providers.story_arcs import StoryArcMetadata, StoryArcSearchResult


class CatalogProvider:
    """No HTTP client or live credentials: only the explicit arc capability."""

    def __init__(self) -> None:
        self.metadata = StoryArcMetadata(
            provider_id="42",
            title="Numbering Event",
            description="Cross-series test event",
            issue_provider_ids=("101", "102"),
            declared_issue_count=2,
            membership_complete=True,
        )
        self.fail = False
        self.closed = 0
        self.searches: list[tuple[str, int, int]] = []

    async def close(self) -> None:
        self.closed += 1

    async def search_story_arcs_page(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[StoryArcSearchResult], int]:
        self.searches.append((query, limit, offset))
        if self.fail:
            raise ComicVineError(500, "secret-bearing provider failure must not render")
        return [
            StoryArcSearchResult(provider_id="42", title="Numbering Event", declared_issue_count=2),
            StoryArcSearchResult(provider_id="43", title="Already Here"),
        ], 2

    async def search_story_arcs(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> list[StoryArcSearchResult]:
        return (await self.search_story_arcs_page(query, limit=limit, offset=offset))[0]

    async def get_story_arc(self, provider_id: str) -> StoryArcMetadata:
        if self.fail:
            raise ComicVineError(500, "secret-bearing provider failure must not render")
        return replace(self.metadata, provider_id=provider_id)

    async def get_story_arc_issues(self, issue_provider_ids: list[str]) -> list[IssueMetadata]:
        return [
            IssueMetadata(
                provider_id=provider_id,
                series_provider_id="501",
                issue_number=1_000_000 if provider_id == "101" else int(provider_id) - 101,
                issue_number_text="1000000"
                if provider_id == "101"
                else "1AU"
                if provider_id == "102"
                else str(int(provider_id) - 101),
                title=f"Issue {provider_id}",
                description="Fixture issue",
                release_date="2020-01-01",
                store_date=None,
                cover_url=None,
                page_count=24,
                comicvine_url=None,
            )
            for provider_id in issue_provider_ids
        ]

    async def get_series(self, provider_id: str) -> SeriesMetadata:
        return SeriesMetadata(
            provider_id=provider_id,
            title="Exact Comics",
            sort_title="exact comics",
            year_start=2020,
            year_end=None,
            status="ongoing",
            publisher=None,
            description="Fixture series",
            cover_url=None,
            issue_count=100,
            comicvine_url=None,
        )
