"""Unit tests for ComicVine provider search behavior."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pullbox.providers.metadata.comicvine import ComicVineError, ComicVineProvider


def _volume_item(provider_id: int, title: str, year: int) -> dict[str, object]:
    return {
        "id": provider_id,
        "name": title,
        "start_year": year,
        "publisher": {"name": "Marvel"},
        "count_of_issues": 4,
        "image": {"medium_url": f"https://example.test/{provider_id}.jpg"},
        "deck": f"{title} deck",
        "site_detail_url": f"https://comicvine.gamespot.com/example/4050-{provider_id}/",
    }


def _issue_item(provider_id: int, issue_number: str) -> dict[str, object]:
    return {
        "id": provider_id,
        "issue_number": issue_number,
        "name": f"Issue {issue_number}",
        "cover_date": "2026-06-01",
        "image": {"medium_url": f"https://example.test/issues/{provider_id}.jpg"},
    }


def _make_response(
    *,
    json_data: dict[str, Any] | None = None,
    content: bytes = b"",
    status_code: int = 200,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.json.return_value = json_data or {"status_code": 1, "results": {}}
    response.raise_for_status = MagicMock()
    return response


@pytest.mark.asyncio
async def test_search_series_globally_fetches_volume_offsets() -> None:
    provider = ComicVineProvider(api_key="offset-test-key", rate_limit=999_999)
    first_page = [
        _volume_item(1000 + index, f"Offset Test {index}", 2000 + index) for index in range(100)
    ]
    second_page = [_volume_item(2000, "Offset Test Final", 2026)]
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_request(endpoint: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((endpoint, dict(params)))
        offset = int(params["offset"])
        return {
            "number_of_total_results": 101,
            "results": first_page if offset == 0 else second_page,
        }

    provider._request = AsyncMock(side_effect=fake_request)  # type: ignore[method-assign]

    try:
        results, total = await provider.search_series_globally(
            "Offset Test",
            max_results=101,
        )
    finally:
        await provider._client.aclose()

    assert total == 101
    assert len(results) == 101
    assert [call[1]["offset"] for call in calls] == [0, 100]
    assert all(call[0] == "/volumes/" for call in calls)
    assert calls[0][1]["filter"] == "name:Offset,name:Test"
    assert calls[0][1]["limit"] == 100
    assert calls[1][1]["limit"] == 1


@pytest.mark.asyncio
async def test_search_series_globally_reuses_short_lived_cache() -> None:
    provider = ComicVineProvider(api_key="cache-test-key", rate_limit=999_999)
    request_mock = AsyncMock(
        return_value={
            "number_of_total_results": 1,
            "results": [_volume_item(3000, "Cache Test", 2026)],
        }
    )
    provider._request = request_mock  # type: ignore[method-assign]

    try:
        first, first_total = await provider.search_series_globally("Cache Test", max_results=10)
        second, second_total = await provider.search_series_globally("Cache Test", max_results=10)
    finally:
        await provider._client.aclose()

    assert first_total == 1
    assert second_total == 1
    assert [result.provider_id for result in first] == ["3000"]
    assert [result.provider_id for result in second] == ["3000"]
    request_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_series_globally_cache_is_not_namespaced_by_api_key() -> None:
    first_provider = ComicVineProvider(api_key="first-cache-key", rate_limit=999_999)
    second_provider = ComicVineProvider(api_key="second-cache-key", rate_limit=999_999)
    first_request = AsyncMock(
        return_value={
            "number_of_total_results": 1,
            "results": [_volume_item(3500, "Shared Cache Test", 2026)],
        }
    )
    second_request = AsyncMock(
        return_value={
            "number_of_total_results": 1,
            "results": [_volume_item(3501, "Shared Cache Test", 2026)],
        }
    )
    first_provider._request = first_request  # type: ignore[method-assign]
    second_provider._request = second_request  # type: ignore[method-assign]

    try:
        first, first_total = await first_provider.search_series_globally(
            "Shared Cache Test",
            max_results=10,
        )
        second, second_total = await second_provider.search_series_globally(
            "Shared Cache Test",
            max_results=10,
        )
    finally:
        await first_provider._client.aclose()
        await second_provider._client.aclose()

    assert first_total == 1
    assert second_total == 1
    assert [result.provider_id for result in first] == ["3500"]
    assert [result.provider_id for result in second] == ["3500"]
    first_request.assert_awaited_once()
    second_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_series_globally_collapses_concurrent_identical_requests() -> None:
    provider = ComicVineProvider(api_key="single-flight-test-key", rate_limit=999_999)
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    request_count = 0

    async def fake_request(_endpoint: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal request_count
        request_count += 1
        request_started.set()
        await release_request.wait()
        return {
            "number_of_total_results": 1,
            "results": [_volume_item(4000, "Single Flight Test", 2026)],
        }

    provider._request = AsyncMock(side_effect=fake_request)  # type: ignore[method-assign]

    first_task = asyncio.create_task(
        provider.search_series_globally("Single Flight Test", max_results=10)
    )
    await request_started.wait()
    second_task = asyncio.create_task(
        provider.search_series_globally("single flight test", max_results=10)
    )
    await asyncio.sleep(0)
    release_request.set()

    try:
        first, second = await asyncio.gather(first_task, second_task)
    finally:
        await provider._client.aclose()

    assert request_count == 1
    assert first == second
    assert [result.provider_id for result in first[0]] == ["4000"]


@pytest.mark.asyncio
async def test_search_series_globally_shields_shared_task_from_caller_cancellation() -> None:
    provider = ComicVineProvider(api_key="shield-test-key", rate_limit=999_999)
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    request_count = 0

    async def fake_request(_endpoint: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal request_count
        request_count += 1
        request_started.set()
        await release_request.wait()
        return {
            "number_of_total_results": 1,
            "results": [_volume_item(5000, "Shield Test", 2026)],
        }

    provider._request = AsyncMock(side_effect=fake_request)  # type: ignore[method-assign]

    first_task = asyncio.create_task(provider.search_series_globally("Shield Test", max_results=10))
    await request_started.wait()
    second_task = asyncio.create_task(
        provider.search_series_globally("shield test", max_results=10)
    )
    await asyncio.sleep(0)

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task

    release_request.set()

    try:
        second_result = await second_task
        cached_result = await provider.search_series_globally("Shield Test", max_results=10)
    finally:
        await provider._client.aclose()

    assert request_count == 1
    assert [result.provider_id for result in second_result[0]] == ["5000"]
    assert [result.provider_id for result in cached_result[0]] == ["5000"]


@pytest.mark.asyncio
async def test_get_recent_issues_for_series_fetches_single_date_descending_page() -> None:
    provider = ComicVineProvider(api_key="recent-issues-test-key", rate_limit=999_999)
    request_mock = AsyncMock(
        return_value={
            "number_of_total_results": 250,
            "results": [_issue_item(9001, "250"), _issue_item(9000, "249")],
        }
    )
    provider._request = request_mock  # type: ignore[method-assign]

    try:
        summaries = await provider.get_recent_issues_for_series("12345", limit=2)
    finally:
        await provider._client.aclose()

    assert [summary.provider_id for summary in summaries] == ["9001", "9000"]
    request_mock.assert_awaited_once()
    endpoint, params = request_mock.await_args.args
    assert endpoint == "/issues/"
    assert params["filter"] == "volume:12345"
    assert params["sort"] == "store_date:desc"
    assert "store_date" in str(params["field_list"])
    assert params["limit"] == 2
    assert params["offset"] == 0


@pytest.mark.asyncio
async def test_request_adds_auth_params_and_returns_ok_payload() -> None:
    provider = ComicVineProvider(api_key="request-test-key", rate_limit=999_999)
    response = _make_response(
        json_data={
            "status_code": 1,
            "number_of_total_results": 1,
            "results": [{"id": 123}],
        }
    )
    provider._client.get = AsyncMock(return_value=response)  # type: ignore[method-assign]

    try:
        data = await provider._request("/search/", {"query": "Batman"})
    finally:
        await provider._client.aclose()

    assert data["results"] == [{"id": 123}]
    provider._client.get.assert_awaited_once_with(  # type: ignore[attr-defined]
        "/search/",
        params={"api_key": "request-test-key", "format": "json", "query": "Batman"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_message"),
    [
        ({"status_code": 100, "error": "Invalid API Key"}, 100, "Invalid API key"),
        ({"status_code": 101, "error": "Not Found"}, 101, "Resource not found"),
        ({"status_code": 107, "error": "Rate Limit"}, 107, "Rate limit exceeded"),
        ({"status_code": 42, "error": "Mystery"}, 42, "API error 42: Mystery"),
    ],
)
async def test_request_maps_comicvine_status_errors(
    payload: dict[str, Any],
    expected_status: int,
    expected_message: str,
) -> None:
    provider = ComicVineProvider(api_key="status-test-key", rate_limit=999_999)
    provider._client.get = AsyncMock(return_value=_make_response(json_data=payload))  # type: ignore[method-assign]

    try:
        with pytest.raises(ComicVineError, match=expected_message) as exc_info:
            await provider._request("/search/")
    finally:
        await provider._client.aclose()

    assert exc_info.value.status_code == expected_status
    assert exc_info.value.retryable is (expected_status == 107)


@pytest.mark.asyncio
async def test_request_maps_http_404_to_not_found_error() -> None:
    provider = ComicVineProvider(api_key="http-test-key", rate_limit=999_999)
    response = _make_response(status_code=404)
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "not found",
        request=MagicMock(),
        response=response,
    )
    provider._client.get = AsyncMock(return_value=response)  # type: ignore[method-assign]

    try:
        with pytest.raises(ComicVineError, match="Resource not found") as exc_info:
            await provider._request("/volume/4050-404/")
    finally:
        await provider._client.aclose()

    assert exc_info.value.status_code == 101


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [420, 429, 503])
async def test_request_preserves_retryable_http_status(status_code: int) -> None:
    provider = ComicVineProvider(api_key="http-test-key", rate_limit=999_999)
    response = _make_response(status_code=status_code)
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "provider unavailable",
        request=MagicMock(),
        response=response,
    )
    provider._client.get = AsyncMock(return_value=response)  # type: ignore[method-assign]

    try:
        with pytest.raises(ComicVineError, match=f"HTTP {status_code}") as exc_info:
            await provider._request("/issue/4000-123/")
    finally:
        await provider._client.aclose()

    assert exc_info.value.status_code == status_code
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_search_series_page_maps_results_and_sorts_exact_year_first() -> None:
    provider = ComicVineProvider(api_key="series-search-test-key", rate_limit=999_999)
    provider._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "number_of_total_results": 2,
            "results": [
                _volume_item(1, "Batman", 2025),
                _volume_item(2, "Batman", 2026),
            ],
        }
    )

    try:
        results, total = await provider.search_series_page("Batman", year=2026, limit=2)
    finally:
        await provider._client.aclose()

    assert total == 2
    assert [result.provider_id for result in results] == ["2", "1"]
    assert results[0].description == "Batman deck"
    provider._request.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_series_maps_volume_metadata() -> None:
    provider = ComicVineProvider(api_key="series-test-key", rate_limit=999_999)
    provider._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "results": {
                "id": 97508,
                "name": "The Batman",
                "start_year": "2026",
                "count_of_issues": "12",
                "publisher": {"name": "DC Comics"},
                "image": {"medium_url": "https://example.test/batman.jpg"},
                "description": "<p>Dark&nbsp;Knight</p>",
                "site_detail_url": "https://comicvine.gamespot.com/batman/4050-97508/",
            }
        }
    )

    try:
        series = await provider.get_series("97508")
    finally:
        await provider._client.aclose()

    assert series.provider_id == "97508"
    assert series.title == "The Batman"
    assert series.sort_title == "Batman, The"
    assert series.year_start == 2026
    assert series.publisher == "DC Comics"
    assert series.description == "Dark Knight"
    assert series.cover_url == "https://example.test/batman.jpg"
    assert series.issue_count == 12
    assert series.comicvine_url == "https://comicvine.gamespot.com/batman/4050-97508/"
    provider._request.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_issue_maps_issue_metadata_creators_and_story_arcs() -> None:
    provider = ComicVineProvider(api_key="issue-test-key", rate_limit=999_999)
    provider._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "results": {
                "id": 1116296,
                "volume": {"id": 165083},
                "issue_number": "½",
                "name": "Doom",
                "description": "<p>Bad&nbsp;guys assemble.</p>",
                "cover_date": "2026-06-01",
                "store_date": "2026-05-28",
                "image": {"medium_url": "https://example.test/doom.jpg"},
                "site_detail_url": "https://comicvine.gamespot.com/doom/4000-1116296/",
                "person_credits": [
                    {
                        "id": 10,
                        "name": "Writer One",
                        "role": "writer",
                        "site_detail_url": "https://comicvine.gamespot.com/writer/4040-10/",
                    },
                    {"name": "", "role": "ignored"},
                ],
                "story_arc_credits": [{"id": 20, "name": "One World Under Doom"}],
            }
        }
    )

    try:
        issue = await provider.get_issue("1116296")
    finally:
        await provider._client.aclose()

    assert issue.provider_id == "1116296"
    assert issue.series_provider_id == "165083"
    assert issue.issue_number == 0.5
    assert issue.issue_number_text == "0.5"
    assert issue.title == "Doom"
    assert issue.description == "Bad guys assemble."
    assert issue.release_date == "2026-06-01"
    assert issue.store_date == "2026-05-28"
    assert issue.cover_url == "https://example.test/doom.jpg"
    assert issue.page_count is None
    assert issue.creators == [
        {
            "name": "Writer One",
            "role": "writer",
            "provider_id": "10",
            "comicvine_url": "https://comicvine.gamespot.com/writer/4040-10/",
        }
    ]
    assert issue.story_arcs == [{"name": "One World Under Doom", "provider_id": "20"}]


@pytest.mark.asyncio
async def test_comicvine_preserves_suffix_issue_number_text() -> None:
    provider = ComicVineProvider(api_key="suffix-test-key", rate_limit=999_999)
    provider._request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "number_of_total_results": 1,
            "results": [_issue_item(12001, "1au")],
        }
    )

    try:
        summaries = await provider.get_recent_issues_for_series("97508", limit=1)
    finally:
        await provider._client.aclose()

    assert summaries[0].issue_number == 1.0
    assert summaries[0].issue_number_text == "1AU"


@pytest.mark.asyncio
async def test_get_issues_for_series_paginates_until_total_reached() -> None:
    provider = ComicVineProvider(api_key="issues-test-key", rate_limit=999_999)
    request_mock = AsyncMock(
        side_effect=[
            {
                "number_of_total_results": 101,
                "results": [_issue_item(1000 + index, str(index + 1)) for index in range(100)],
            },
            {
                "number_of_total_results": 101,
                "results": [_issue_item(2000, "101")],
            },
        ]
    )
    provider._request = request_mock  # type: ignore[method-assign]

    try:
        summaries = await provider.get_issues_for_series("97508")
    finally:
        await provider._client.aclose()

    assert len(summaries) == 101
    assert [summary.provider_id for summary in summaries[-2:]] == ["1099", "2000"]
    assert [call.args[1]["offset"] for call in request_mock.await_args_list] == [0, 100]


@pytest.mark.asyncio
async def test_get_issues_for_series_by_numbers_deduplicates_and_formats_filters() -> None:
    provider = ComicVineProvider(api_key="targeted-issues-test-key", rate_limit=999_999)
    request_mock = AsyncMock(
        side_effect=[
            {"results": [_issue_item(1001, "1")]},
            {"results": [_issue_item(10015, "1.5")]},
        ]
    )
    provider._request = request_mock  # type: ignore[method-assign]

    try:
        summaries = await provider.get_issues_for_series_by_numbers("97508", [1.5, 1.0, 1.0])
    finally:
        await provider._client.aclose()

    assert [summary.issue_number for summary in summaries] == [1.0, 1.5]
    filters = [call.args[1]["filter"] for call in request_mock.await_args_list]
    assert filters == [
        "volume:97508,issue_number:1",
        "volume:97508,issue_number:1.5",
    ]


@pytest.mark.asyncio
async def test_get_cover_image_returns_cdn_bytes_without_api_params() -> None:
    provider = ComicVineProvider(api_key="cover-test-key", rate_limit=999_999)
    provider._client.get = AsyncMock(return_value=_make_response(content=b"image-bytes"))  # type: ignore[method-assign]

    try:
        image = await provider.get_cover_image("https://comicvine.test/cover.jpg")
    finally:
        await provider._client.aclose()

    assert image == b"image-bytes"
    provider._client.get.assert_awaited_once_with("https://comicvine.test/cover.jpg")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_test_connection_reports_success_and_api_errors() -> None:
    provider = ComicVineProvider(api_key="health-test-key", rate_limit=999_999)
    provider._request = AsyncMock(return_value={"results": []})  # type: ignore[method-assign]

    try:
        healthy = await provider.test_connection()
        provider._request = AsyncMock(side_effect=ComicVineError(100, "Invalid API key"))  # type: ignore[method-assign]
        unhealthy = await provider.test_connection()
    finally:
        await provider._client.aclose()

    assert healthy.healthy is True
    assert healthy.message == "ComicVine API key is valid"
    assert unhealthy.healthy is False
    assert unhealthy.message == "ComicVine API error: Invalid API key"
    assert unhealthy.details == {"status_code": "100"}
