"""Transaction-owned Add Series command for a server-fetched source catalog."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.events import EventBus, SeriesAdded
from pullbox.core.metadata_identity import ExternalIdentityRef, MetadataEntityKind
from pullbox.services.cover_cache_service import purge_series_cover_cache
from pullbox.services.metadata_series_adoption import (
    SeriesAdoptionResult,
    SourceSeriesBundle,
    adopt_source_series_bundle,
    persist_adoption_series_baseline,
)
from pullbox.services.metadata_service import (
    MetadataService,
    classify_issue_metadata,
    infer_series_type_from_issue_evidence,
)
from pullbox.services.series_service import (
    SeriesFolderCreationResult,
    SeriesService,
    rollback_series_folder_creation,
)


@asynccontextmanager
async def source_series_add_transaction(
    session: AsyncSession,
    bundle: SourceSeriesBundle,
    *,
    library_root_id: int | None,
    search_on_add: bool,
    event_bus: EventBus,
    before_adopt: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[SeriesAdoptionResult]:
    """Own this add transaction, including response construction before commit.

    The caller releases its read snapshot before fetching the bundle. This is an
    explicit lifecycle boundary, not a flush-only service or a nested transaction.
    Only newly created directories are eligible for rollback cleanup.
    """
    if session.in_transaction():
        raise ValueError("Source Add requires a session without an active transaction")
    folder: SeriesFolderCreationResult | None = None
    try:
        async with session.begin():
            if before_adopt is not None:
                await before_adopt()
            result = await adopt_source_series_bundle(session, bundle, monitored=search_on_add)
            if result.created:
                series = result.series
                infer_series_type_from_issue_evidence(
                    series,
                    [
                        classify_issue_metadata(series.series_type, issue.title)[0]
                        for issue in bundle.issues
                    ],
                )
                await MetadataService.classify_and_link_series(
                    session,
                    series,
                    preserve_provider_type=bundle.series.series_type is not None,
                )
                if result.snapshot is not None:
                    result = replace(
                        result,
                        snapshot=await persist_adoption_series_baseline(
                            session, series, result.snapshot
                        ),
                    )
                if library_root_id is not None:
                    folder = await SeriesService._create_series_folder(
                        session,
                        series,
                        library_root_id,
                        series.comicvine_id,
                        source_identity=ExternalIdentityRef(
                            bundle.series.identity_namespace,
                            MetadataEntityKind.SERIES,
                            bundle.series.external_id,
                        ),
                    )
                await purge_series_cover_cache(session, series.id)
                await session.flush()
            yield result
    except BaseException:
        if folder is not None:
            rollback_series_folder_creation(folder)
        raise
    if result.created:
        await event_bus.emit(
            SeriesAdded(series_id=result.series.id, comicvine_id=result.series.comicvine_id)
        )
