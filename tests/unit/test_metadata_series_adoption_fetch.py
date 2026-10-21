"""Only a complete, source-bound server fetch can seed a new issue catalog."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.schemas.metadata_sources import MetadataFetch, MetadataPage, SourceStatus
from pullbox.services.metadata_series_adoption import (
    SeriesAdoptionError,
    fetch_source_series_bundle,
)
from tests.unit.test_metadata_discovery import row
from tests.unit.test_metadata_source_reads import ReadAdapter, issue_row, registry


class CatalogAdapter(ReadAdapter):
    def __init__(self, count=201):
        super().__init__()
        self.count = count
        self.pages = {}
        self.delay = 0

    async def series(self, external_id, *, validator=None):
        self.calls.append(("series", external_id))
        return MetadataFetch(
            status=SourceStatus.OK,
            data=row(self.source).model_copy(update={"issue_count": self.count}),
        )

    async def issues(self, external_id, *, page=1, validator=None):
        self.calls.append(("issues", external_id, page))
        await asyncio.sleep(self.delay)
        if page in self.pages:
            return self.pages[page]
        start = (page - 1) * 100
        return MetadataFetch(
            status=SourceStatus.OK,
            data=MetadataPage(
                results=[
                    issue_row(
                        external_id=str(1000 + i),
                        issue_number_text=str(i + 1),
                        issue_number_key=str(i + 1),
                    )
                    for i in range(start, min(start + 100, self.count))
                ],
                total=self.count,
                next_page=page + 1 if start + 100 < self.count else None,
                truncated=False,
            ),
        )


async def fetch(adapter, **kwargs):
    instance = registry(adapter)
    return await fetch_source_series_bundle(
        instance,
        Source.METRON_API,
        "42",
        source_revision=instance.runtime[Source.METRON_API].policy.revision,
        **kwargs,
    )


@pytest.mark.parametrize("count", [0, 1, 100, 101, 201])
async def test_fetch_all_pages_not_just_preview(count):
    adapter = CatalogAdapter(count)
    bundle = await fetch(adapter)
    assert len(bundle.issues) == count
    assert bundle.series.external_id == "42"
    assert adapter.calls == [("series", "42")] + [
        ("issues", "42", page) for page in range(1, max(1, (count + 99) // 100) + 1)
    ]
    assert adapter.closed == len(adapter.calls)


async def test_add_uses_first_page_representative_cover_without_extra_requests():
    adapter = CatalogAdapter(101)
    first = await adapter.issues("42", page=1)
    first.data.results[0].image_url = "https://static.metron.cloud/media/issue/first.jpg"
    adapter.pages[1] = first
    adapter.calls.clear()
    result = await fetch(adapter)
    assert result.series.image_url == first.data.results[0].image_url
    assert adapter.calls == [("series", "42"), ("issues", "42", 1), ("issues", "42", 2)]


async def test_source_revision_changed_before_fetch_makes_no_requests():
    adapter = CatalogAdapter()
    instance = registry(adapter)
    with pytest.raises(SeriesAdoptionError, match="settings changed"):
        await fetch_source_series_bundle(instance, Source.METRON_API, "42", source_revision=999)
    assert adapter.calls == []


async def test_prefetched_series_profile_is_reused_for_complete_catalog():
    adapter = CatalogAdapter(101)
    profile = row(adapter.source).model_copy(update={"issue_count": 101})
    result = await fetch(adapter, profile=profile)
    assert result.series == profile and len(result.issues) == 101
    assert adapter.calls == [("issues", "42", 1), ("issues", "42", 2)]


@pytest.mark.parametrize("change", ["source", "identity_namespace", "external_id"])
async def test_prefetched_series_profile_cannot_cross_source_identity(change):
    adapter = CatalogAdapter(1)
    profile = row(adapter.source)
    profile = profile.model_copy(
        update={
            change: {
                "source": Source.COMICVINE_API,
                "identity_namespace": Source.COMICVINE_API.identity_namespace,
                "external_id": "999",
            }[change]
        }
    )
    with pytest.raises(SeriesAdoptionError, match="different source identity"):
        await fetch(adapter, profile=profile)
    assert adapter.calls == []


@pytest.mark.parametrize(
    "status", [SourceStatus.RATE_LIMITED, SourceStatus.TIMEOUT, SourceStatus.NOT_FOUND]
)
async def test_incomplete_catalog_is_not_an_adoptable_bundle(status):
    adapter = CatalogAdapter()
    adapter.pages[2] = MetadataFetch(status=status)
    with pytest.raises(SeriesAdoptionError, match="issue catalog"):
        await fetch(adapter)
    assert adapter.calls[-1] == ("issues", "42", 2)


@pytest.mark.parametrize("mutation", ["repeat_id", "changed_total", "wrong_parent"])
async def test_cross_page_consistency_is_required(mutation):
    adapter = CatalogAdapter(101)
    item = issue_row(external_id="1100", issue_number_text="101", issue_number_key="101")
    total = 101
    if mutation == "repeat_id":
        item.external_id = "1000"
    elif mutation == "changed_total":
        total = 102
    else:
        item.series_external_id = "99"
    adapter.pages[2] = MetadataFetch(
        status=SourceStatus.OK, data=MetadataPage(results=[item], total=total)
    )
    with pytest.raises(SeriesAdoptionError):
        await fetch(adapter)


@pytest.mark.parametrize("summary_count", [0, 1, 3, 10001, None])
@pytest.mark.parametrize("catalog_count", [2, 101])
async def test_stale_summary_does_not_reject_a_complete_catalog(summary_count, catalog_count):
    adapter = CatalogAdapter(catalog_count)
    profile = row(adapter.source).model_copy(update={"issue_count": summary_count})
    try:
        result = await fetch(adapter, profile=profile)
    except SeriesAdoptionError as exc:
        pytest.fail(f"A complete catalog must not be rejected for its stale summary: {exc}")
    assert result.catalog_total == len(result.issues) == catalog_count
    assert result.series.issue_count == summary_count, "Keep the original provider evidence"
    assert adapter.calls == [
        ("issues", "42", page) for page in range(1, (catalog_count + 99) // 100 + 1)
    ]


@pytest.mark.parametrize("problem", ["changed_total", "incomplete", "repeat_id", "wrong_parent"])
async def test_stale_summary_never_excuses_an_invalid_catalog(problem):
    adapter = CatalogAdapter(101)
    profile = row(adapter.source).model_copy(update={"issue_count": 1})
    item = issue_row(external_id="1100", issue_number_text="101", issue_number_key="101")
    total = 102 if problem == "changed_total" else 101
    if problem == "repeat_id":
        item.external_id = "1000"
    elif problem == "wrong_parent":
        item.series_external_id = "99"
    adapter.pages[2] = MetadataFetch(
        status=SourceStatus.OK,
        data=MetadataPage(results=[] if problem == "incomplete" else [item], total=total),
    )
    with pytest.raises(SeriesAdoptionError):
        await fetch(adapter, profile=profile)
    assert adapter.calls[-1] == ("issues", "42", 2)


async def test_resource_cap_stops_after_first_page_without_traversing_catalog():
    adapter = CatalogAdapter(201)
    with pytest.raises(SeriesAdoptionError, match="limit"):
        await fetch(adapter, max_issues=200)
    assert adapter.calls == [("series", "42"), ("issues", "42", 1)]


async def test_total_deadline_bounds_traversal_and_closes_adapter():
    adapter = CatalogAdapter()
    adapter.delay = 0.03
    with pytest.raises(SeriesAdoptionError, match="timed out"):
        await fetch(adapter, timeout=0.01)
    assert adapter.closed == 2


async def test_cancellation_is_not_a_recoverable_adoption_result():
    adapter = CatalogAdapter()
    adapter.delay = 100
    task = asyncio.create_task(fetch(adapter))
    while len(adapter.calls) < 2 and not task.done():
        await asyncio.sleep(0)
    if task.done():
        await task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.closed == 2


async def test_local_catalog_generation_cannot_change_between_profile_and_pages():
    adapter = CatalogAdapter(1)
    adapter.source = Source.COMICVINE_LOCAL
    cutoff = datetime(2026, 9, 1, tzinfo=UTC)

    async def profile(external_id, *, validator=None):
        return MetadataFetch(
            status=SourceStatus.OK,
            data=row(adapter.source).model_copy(
                update={"issue_count": 1, "source_updated_at": cutoff}
            ),
        )

    adapter.series = profile
    adapter.pages[1] = MetadataFetch(
        status=SourceStatus.OK,
        data=MetadataPage(
            total=1,
            results=[
                issue_row(
                    source=adapter.source,
                    identity_namespace=adapter.source.identity_namespace,
                    source_updated_at=cutoff + timedelta(days=1),
                )
            ],
        ),
    )
    instance = registry(adapter)
    with pytest.raises(SeriesAdoptionError, match="catalog changed"):
        await fetch_source_series_bundle(instance, adapter.source, "42", source_revision=0)
