"""Strict, bounded Comic Vine story-arc requests using shared request plumbing.

The live detail endpoint returns its entire nested issues array, including more
than 100 members; offset/limit paginate neither that array nor its reading order.
The envelope total counts arcs. The misspelled issue counter is often a stale
zero, so neither value may replace the explicit membership array.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.providers.base import IssueMetadata
from pullbox.providers.metadata.comicvine import (
    ComicVineError,
    _comicvine_name_filter,
    _strip_html,
)
from pullbox.providers.story_arcs import (
    MAX_STORY_ARC_MEMBERS,
    StoryArcMetadata,
    StoryArcSearchResult,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    _Request = Callable[[str, dict[str, Any] | None], Awaitable[dict[str, Any]]]

_PAGE_SIZE = 100
_MAX_SEARCH_OFFSET = 10_000
_MAX_HYDRATION_PAGES_PER_BATCH = 10
_ARC_FIELDS = "id,name,description,deck,publisher,image,site_detail_url,count_of_isssue_appearances"
_ISSUE_FIELDS = (
    "id,volume,issue_number,name,description,deck,cover_date,store_date,image,site_detail_url"
)


def _incompatible() -> ComicVineError:
    """Never include provider response contents, credentials, or URLs in errors."""
    return ComicVineError(0, "Comic Vine returned an incompatible story-arc response")


def _canonical_id(value: object) -> str:
    if type(value) is int:
        value = str(value)
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None
        or int(value) > 2**63 - 1
    ):
        raise ValueError("A positive canonical provider ID is required")
    return value


def _provider_id(value: object) -> str:
    try:
        return _canonical_id(value)
    except ValueError:
        raise _incompatible() from None


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _incompatible()
    return value


def _text(value: object, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise _incompatible()
    return value


def _nested_text(item: dict[str, Any], key: str, child_key: str) -> str | None:
    value = item.get(key)
    return None if value is None else _text(_object(value).get(child_key))


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _incompatible()
    return value


async def _request_json(request: _Request, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await request(endpoint, params)
    except ComicVineError as exc:
        # Keep retry/status semantics without reflecting an upstream error body.
        raise ComicVineError(
            exc.status_code,
            "Comic Vine story-arc request failed",
            retryable=exc.retryable,
        ) from None
    except (ValueError, TypeError, AttributeError):
        # _request parses JSON and accesses the envelope before returning it.
        raise _incompatible() from None
    data = _object(data)
    if "status_code" in data and (type(data["status_code"]) is not int or data["status_code"] != 1):
        raise _incompatible()
    return data


def _list_page(data: dict[str, Any], *, limit: int, offset: int) -> tuple[list[Any], int]:
    items = data.get("results")
    if not isinstance(items, list) or len(items) > limit:
        raise _incompatible()
    total = _count(data.get("number_of_total_results"))
    if "number_of_page_results" in data and _count(data["number_of_page_results"]) != len(items):
        raise _incompatible()
    if items and offset + len(items) > total:
        raise _incompatible()
    if not items and offset < total:
        raise _incompatible()
    return items, total


def _arc_result(item: dict[str, Any]) -> StoryArcSearchResult:
    title = _text(item.get("name"), required=True)
    assert title is not None
    raw_count = item.get("count_of_isssue_appearances")
    count = None if raw_count is None else _count(raw_count)
    return StoryArcSearchResult(
        provider_id=_provider_id(item.get("id")),
        title=title,
        description=_strip_html(_text(item.get("description")) or _text(item.get("deck"))),
        publisher=_nested_text(item, "publisher", "name"),
        cover_url=_nested_text(item, "image", "medium_url"),
        comicvine_url=_text(item.get("site_detail_url")),
        declared_issue_count=count or None,
    )


async def search_story_arcs_page(
    request: _Request, query: str, *, limit: int = 20, offset: int = 0
) -> tuple[list[StoryArcSearchResult], int]:
    """Search one explicit page; never use the unreliable generic search resource."""
    if type(limit) is not int or not 1 <= limit <= _PAGE_SIZE:
        raise ValueError("Story-arc search limit must be from 1 to 100")
    if type(offset) is not int or not 0 <= offset <= _MAX_SEARCH_OFFSET:
        raise ValueError("Story-arc search offset must be from 0 to 10000")
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("Story-arc query must be text of at most 500 characters")
    name_filter = _comicvine_name_filter(query)
    if not name_filter:
        return [], 0
    data = await _request_json(
        request,
        "/story_arcs/",
        {
            "filter": name_filter,
            "field_list": _ARC_FIELDS,
            "limit": limit,
            "offset": offset,
        },
    )
    items, total = _list_page(data, limit=limit, offset=offset)
    results = [_arc_result(_object(item)) for item in items]
    if len({item.provider_id for item in results}) != len(results):
        raise _incompatible()
    return results, total


async def get_story_arc(request: _Request, provider_id: str) -> StoryArcMetadata:
    """Preserve the full observed member list and explicitly qualified order."""
    provider_id = _canonical_id(provider_id)
    data = await _request_json(
        request, f"/story_arc/4045-{provider_id}/", {"field_list": f"{_ARC_FIELDS},issues"}
    )
    item = _object(data.get("results"))
    result = _arc_result(item)
    members = item.get("issues")
    if (
        result.provider_id != provider_id
        or not isinstance(members, list)
        or len(members) > MAX_STORY_ARC_MEMBERS
    ):
        raise _incompatible()
    ids = tuple(_provider_id(_object(member).get("id")) for member in members)
    if len(set(ids)) != len(ids):
        raise _incompatible()
    complete = result.declared_issue_count is None or result.declared_issue_count == len(ids)
    warnings: list[str] = []
    if item.get("count_of_isssue_appearances") == 0 and ids:
        warnings.append("unreliable_zero_issue_count")
    if not complete:
        warnings.append("issue_count_mismatch")
    return StoryArcMetadata(
        provider_id=result.provider_id,
        title=result.title,
        issue_provider_ids=ids,
        description=result.description,
        publisher=result.publisher,
        cover_url=result.cover_url,
        comicvine_url=result.comicvine_url,
        declared_issue_count=result.declared_issue_count,
        membership_complete=complete,
        warnings=tuple(warnings),
    )


def _issue_metadata(item: dict[str, Any]) -> IssueMetadata:
    number = _text(item.get("issue_number"), required=True)
    assert number is not None
    try:
        numeric_number, exact_number = parse_issue_number_text(number)
    except ValueError:
        raise _incompatible() from None
    return IssueMetadata(
        provider_id=_provider_id(item.get("id")),
        series_provider_id=_provider_id(_object(item.get("volume")).get("id")),
        issue_number=numeric_number,
        issue_number_text=exact_number,
        title=_text(item.get("name")),
        description=_strip_html(_text(item.get("description")) or _text(item.get("deck"))),
        release_date=_text(item.get("cover_date")),
        store_date=_text(item.get("store_date")),
        cover_url=_nested_text(item, "image", "medium_url"),
        page_count=None,
        comicvine_url=_text(item.get("site_detail_url")),
    )


async def _hydrate_batch(request: _Request, ids: tuple[str, ...]) -> list[IssueMetadata]:
    expected_ids = set(ids)
    found: dict[str, IssueMetadata] = {}
    offset = 0
    for _ in range(_MAX_HYDRATION_PAGES_PER_BATCH):
        data = await _request_json(
            request,
            "/issues/",
            {
                "filter": f"id:{'|'.join(ids)}",
                "field_list": _ISSUE_FIELDS,
                "limit": _PAGE_SIZE,
                "offset": offset,
            },
        )
        items, total = _list_page(data, limit=_PAGE_SIZE, offset=offset)
        if total != len(ids):
            raise _incompatible()
        for raw_item in items:
            issue = _issue_metadata(_object(raw_item))
            if issue.provider_id not in expected_ids or issue.provider_id in found:
                raise _incompatible()
            found[issue.provider_id] = issue
        offset += len(items)
        if offset == total:
            if set(found) != expected_ids:
                raise _incompatible()
            return [found[provider_id] for provider_id in ids]
    raise _incompatible()


async def get_story_arc_issues(
    request: _Request, issue_provider_ids: Sequence[str]
) -> list[IssueMetadata]:
    """Hydrate bounded ID batches atomically to the caller, not partial results."""
    if (
        isinstance(issue_provider_ids, (str, bytes))
        or len(issue_provider_ids) > MAX_STORY_ARC_MEMBERS
    ):
        raise ValueError("Story-arc hydration requires at most 5000 distinct issue IDs")
    ids = tuple(_canonical_id(value) for value in issue_provider_ids)
    if len(set(ids)) != len(ids):
        raise ValueError("Story-arc hydration requires distinct issue IDs")
    results: list[IssueMetadata] = []
    for start in range(0, len(ids), _PAGE_SIZE):
        results.extend(await _hydrate_batch(request, ids[start : start + _PAGE_SIZE]))
    return results
