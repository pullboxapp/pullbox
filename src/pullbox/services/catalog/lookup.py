"""Catalog discovery adapter for existing search and import matching contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.services.catalog.contract import CatalogError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pullbox.providers.base import (
        IssueMetadata,
        IssueSummary,
        SeriesMetadata,
        SeriesSearchResult,
    )
    from pullbox.services.catalog.reader import CatalogReader


class CatalogLookupService:
    """Basic catalog discovery only; full metadata refresh still owns its provider."""

    is_local_catalog = True

    def __init__(self, reader: CatalogReader) -> None:
        self.reader = reader

    async def search_series(
        self,
        query: str,
        year: int | None = None,
        *,
        limit: int = 1000,
        offset: int = 0,
        suppress_errors: bool = False,
    ) -> list[SeriesSearchResult]:
        return await self.reader.search(query, year, limit, offset)

    async def search_series_page(
        self,
        query: str,
        year: int | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        suppress_errors: bool = False,
    ) -> tuple[list[SeriesSearchResult], int]:
        rows = await self.reader.search(query, year, limit, offset)
        return rows, len(rows)

    async def search_series_globally(
        self,
        query: str,
        *,
        max_results: int = 1000,
        page_size: int = 100,
        suppress_errors: bool = False,
    ) -> tuple[list[SeriesSearchResult], int]:
        rows = await self.reader.search(query, limit=max_results)
        return rows, len(rows)

    async def get_series(self, series_provider_id: str) -> SeriesMetadata:
        result = await self.reader.series(int(series_provider_id))
        if result is None:
            raise CatalogError(
                "This series is not in the local catalog. Check for a catalog update."
            )
        return result

    async def get_series_cached(self, series_provider_id: str) -> SeriesMetadata | None:
        return await self.reader.series(int(series_provider_id))

    async def get_issues_for_series(self, series_provider_id: str) -> list[IssueSummary]:
        await self.get_series(series_provider_id)
        return await self.reader.issues(int(series_provider_id))

    async def get_issues_for_series_by_numbers(
        self, series_provider_id: str, issue_numbers: Sequence[float | str]
    ) -> list[IssueSummary]:
        numbers = {normalize_issue_number_text(number) for number in issue_numbers}
        return [
            issue
            for issue in await self.get_issues_for_series(series_provider_id)
            if normalize_issue_number_text(issue.issue_number_text or issue.issue_number) in numbers
        ]

    async def get_issue(self, issue_provider_id: str) -> IssueMetadata:
        result = await self.reader.issue(int(issue_provider_id))
        if result is None:
            raise CatalogError(
                "This issue is not in the local catalog. Check for a catalog update."
            )
        return result

    async def close(self) -> None:
        """Queries own and close their connections individually."""


def catalog_or_provider(provider: Any) -> Any:
    """Select the local discovery source without altering full refresh providers."""
    from pullbox.services.catalog.reader import get_catalog_reader

    reader = get_catalog_reader()
    return CatalogLookupService(reader) if reader.available else provider
