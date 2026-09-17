"""Dashed issue suffixes remain exact identities across metadata and discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pullbox.core.issue_numbers import normalize_issue_number_queries, parse_issue_number_text
from pullbox.core.release_parser import normalize_issue_number, parse_release_title
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series
from pullbox.providers.metadata.comicvine import ComicVineProvider
from pullbox.services.catalog.reader import CatalogReader
from pullbox.services.metadata_service import MetadataService

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.parametrize("number", ["50-x", "50-o"])
@pytest.mark.parametrize("source", ["comicvine", "catalog"])
async def test_metadata_keeps_hyphenated_issue_identity(number: str, source: str) -> None:
    if source == "catalog":
        summary = CatalogReader._summary(
            (501, 10, number, number, "50", "Special issue", None, None, None),
            datetime(2026, 9, 17, tzinfo=UTC),
        )
    else:
        provider = ComicVineProvider(api_key="fixture", rate_limit=999_999)
        provider._request = AsyncMock(
            return_value={
                "results": [{"id": 501, "issue_number": number}],
                "number_of_total_results": 1,
            }
        )
        try:
            summary = (await provider.get_issues_for_series("10"))[0]
        finally:
            await provider._client.aclose()
    assert summary.issue_number == 50.0
    assert summary.issue_number_text == number.upper()


@pytest.mark.parametrize("batch", [False, True])
async def test_comicvine_issue_details_keep_hyphenated_identity(batch: bool) -> None:
    item = {"id": 501, "volume": {"id": 10}, "issue_number": "50-x"}
    provider = ComicVineProvider(api_key="fixture", rate_limit=999_999)
    provider._request = AsyncMock(return_value={"results": [item] if batch else item})
    try:
        issue = (
            (await provider.get_issue_batch(["501"]))["501"]
            if batch
            else await provider.get_issue("501")
        )
    finally:
        await provider._client.aclose()
    assert issue.issue_number == 50.0
    assert issue.issue_number_text == "50-X"


async def test_comicvine_targeted_lookup_preserves_dash_and_deduplicates() -> None:
    provider = ComicVineProvider(api_key="fixture", rate_limit=999_999)
    request = AsyncMock(
        side_effect=[
            {"results": [{"id": 501, "issue_number": "50-o"}]},
            {"results": [{"id": 502, "issue_number": "50-x"}]},
        ]
    )
    provider._request = request
    try:
        summaries = await provider.get_issues_for_series_by_numbers("10", ["050-x", "50-X", "50-o"])
    finally:
        await provider._client.aclose()
    assert [item.issue_number_text for item in summaries] == ["50-O", "50-X"]
    assert [call.args[1]["filter"] for call in request.await_args_list] == [
        "volume:10,issue_number:50-O",
        "volume:10,issue_number:50-X",
    ]


@pytest.mark.parametrize("existing_zero", [False, True])
async def test_refresh_keeps_suffix_siblings_and_repairs_old_zero(
    tmp_path: Path, db_session: AsyncSession, existing_zero: bool
) -> None:
    series = Series(title="X-O Manowar", sort_title="X-O Manowar", comicvine_id=10)
    db_session.add(series)
    await db_session.flush()
    previous = None
    if existing_zero:
        # Older parsing flattened the first suffix to #0. Keep ownership and its row ID.
        previous = Issue(
            series_id=series.id,
            comicvine_id=501,
            issue_number=0,
            issue_number_text="0",
            status=IssueStatus.OWNED,
        )
        db_session.add(previous)
        await db_session.flush()
    provider = ComicVineProvider(api_key="fixture", rate_limit=999_999)
    provider._request = AsyncMock(
        return_value={
            "number_of_total_results": 3,
            "results": [
                {"id": 501, "issue_number": "50-x"},
                {"id": 502, "issue_number": "50-o"},
                {"id": 503, "issue_number": "50"},
            ],
        }
    )
    service = MetadataService(provider, tmp_path)
    try:
        await service.fetch_issues_for_series(db_session, series.id)
        await service.fetch_issues_for_series(db_session, series.id)
    finally:
        await provider._client.aclose()
    issues = list(
        (await db_session.scalars(select(Issue).where(Issue.series_id == series.id))).all()
    )
    assert {issue.comicvine_id: issue.effective_issue_number_text for issue in issues} == {
        501: "50-X",
        502: "50-O",
        503: "50",
    }
    if previous is not None:
        assert next(issue for issue in issues if issue.comicvine_id == 501).id == previous.id
        assert previous.status == IssueStatus.OWNED


@pytest.mark.parametrize(
    "raw, number, exact",
    [
        ("50-x", 50, "50-X"),
        ("050-X", 50, "50-X"),
        ("50-o", 50, "50-O"),
        ("-1-x", -1, "-1-X"),
        ("0.5-o", 0.5, "0.5-O"),
    ],
)
def test_numeric_compatibility_supports_hyphenated_suffix(
    raw: str, number: float, exact: str
) -> None:
    assert normalize_issue_number(raw) == number
    assert parse_issue_number_text(raw) == (number, exact)


def test_lookup_keys_keep_suffixes_distinct_from_ranges_and_plain_numbers() -> None:
    assert normalize_issue_number_queries([50, "50-x", "050-X", "50-o", "50X"]) == [
        50.0,
        "50-O",
        "50-X",
        "50X",
    ]
    for invalid in ["50-51", "50-", "50--x", "50-x-o"]:
        with pytest.raises(ValueError):
            parse_issue_number_text(invalid)


@pytest.mark.parametrize(
    "title",
    [
        "X-O Manowar #050-x (1996).cbz",
        "X-O Manowar 050-x (1996).cbz",
        "X-O.Manowar.050-x.1996.cbz",
        "X-O_Manowar_050-x.cbz",
        "X-O Manowar No.50-x (1996).cbz",
        "X-O Manowar 50-x of 68 (1996).cbz",
    ],
)
def test_release_parser_preserves_suffix_without_mangling_series(title: str) -> None:
    parsed = parse_release_title(title)
    assert parsed is not None
    assert parsed.issue_number == 50.0
    assert parsed.issue_number_text == "50-X"
    assert parsed.series_name == "X-O Manowar"
    assert not parsed.is_pack


def test_numeric_range_is_still_a_pack_not_a_hyphenated_issue() -> None:
    parsed = parse_release_title("X-O Manowar 050-051 (1996).cbz")
    assert parsed is not None
    assert parsed.is_pack
    assert parsed.pack_range == "50-51"
