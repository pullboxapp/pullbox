"""Metadata source configuration, discovery and explicit connection checks."""

from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError

from pullbox.api.deps import AuthenticatedUser, DbSession, InteractiveOperatorUser, Settings
from pullbox.core.exceptions import ConfigurationError
from pullbox.core.metadata_identity import MetadataSource
from pullbox.models.library import LibraryRoot
from pullbox.providers.metadata.gcd_local import GcdLocalSource
from pullbox.schemas.metadata_sources import (
    DeferredMetadataRead,
    GcdSignInRequest,
    MetadataFetch,
    MetadataPage,
    ProviderIssueRead,
    SeriesAddPreviewQuery,
    SeriesDiscoveryQuery,
    SeriesDiscoveryRead,
    SeriesIssuePageQuery,
    SeriesIssuePageRead,
    SeriesPreviewRead,
    SourceDescriptor,
    SourcePolicyRead,
    SourcePolicyWrite,
    SourcePriorityWrite,
    SourceStatus,
    SourceTestRead,
    StoryArcDiscoveryQuery,
    StoryArcDiscoveryRead,
    StoryArcIssuePageQuery,
    StoryArcPreviewQuery,
    StoryArcPreviewRead,
)
from pullbox.schemas.pagination import PaginatedResponse
from pullbox.services.gcd_local_activation import activate_snapshot, validate_for_request
from pullbox.services.gcd_sign_in import sign_in_gcd
from pullbox.services.metadata_arc_preview import preview_source_arc
from pullbox.services.metadata_discovery import MetadataSourceError, MetadataSourceRegistry
from pullbox.services.metadata_read_cache import source_read_cache
from pullbox.services.metadata_series_adoption import SeriesAdoptionError
from pullbox.services.metadata_series_artwork import representative_series_cover
from pullbox.services.metadata_series_preview import preview_series_folder, preview_source_series
from pullbox.services.metadata_source_status import deferred_work, source_status
from pullbox.services.metadata_sources import (
    SourceConfigurationConflictError,
    load_source_runtime,
    read_source_policies,
    record_source_health,
    save_source_policy,
    save_source_priorities,
)

router = APIRouter(prefix="/metadata", tags=["metadata"])
logger = structlog.get_logger(__name__)


async def _preview_root(session: DbSession, root_id: int) -> LibraryRoot:
    root = await session.get(LibraryRoot, root_id, populate_existing=True)
    if root is None or not root.enabled or not root.allow_managed_writes:
        raise HTTPException(409, "Choose an enabled managed library root, then retry the preview.")
    return root


async def _require_source_revision(
    session: DbSession,
    source: MetadataSource,
    revision: int,
    *,
    subject: Literal["series", "story arc"] = "series",
) -> None:
    policies = await read_source_policies(session)
    if next(policy.revision for policy in policies if policy.source is source) != revision:
        raise HTTPException(409, f"Metadata source settings changed. Preview the {subject} again.")


@router.post("/story-arcs/search", response_model=StoryArcDiscoveryRead)
async def search_story_arcs(
    body: StoryArcDiscoveryQuery, session: DbSession, _user: AuthenticatedUser, settings: Settings
) -> StoryArcDiscoveryRead:
    runtime = await load_source_runtime(
        session, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
    )
    await session.rollback()
    return await MetadataSourceRegistry(
        runtime, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
    ).discover_arcs(body)


@router.post("/story-arcs/preview", response_model=StoryArcPreviewRead)
async def preview_story_arc(
    body: StoryArcPreviewQuery, session: DbSession, _user: AuthenticatedUser, settings: Settings
) -> StoryArcPreviewRead:
    runtime = await load_source_runtime(
        session, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
    )
    await session.rollback()
    result = await preview_source_arc(
        MetadataSourceRegistry(
            runtime,
            gcd_api_enabled=settings.metadata_gcd_api_v2_enabled,
            read_cache=source_read_cache(session),
        ),
        body.source,
        body.external_id,
    )
    await _require_source_revision(
        session, body.source, result.source_revision, subject="story arc"
    )
    return result


@router.post("/story-arcs/issues", response_model=MetadataFetch[MetadataPage[ProviderIssueRead]])
async def story_arc_issues(
    body: StoryArcIssuePageQuery, session: DbSession, _user: AuthenticatedUser, settings: Settings
) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
    runtime = await load_source_runtime(
        session, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
    )
    if (
        next(item.policy.revision for item in runtime if item.policy.source is body.source)
        != body.source_revision
    ):
        raise HTTPException(409, "Metadata source settings changed. Preview the story arc again.")
    await session.rollback()
    result = await MetadataSourceRegistry(
        runtime,
        gcd_api_enabled=settings.metadata_gcd_api_v2_enabled,
        read_cache=source_read_cache(session),
    ).story_arc_issues(body.source, body.external_id, page=body.page)
    await _require_source_revision(session, body.source, body.source_revision, subject="story arc")
    return result


@router.post("/series/preview", response_model=SeriesPreviewRead)
async def preview_series(
    body: SeriesAddPreviewQuery, session: DbSession, _user: AuthenticatedUser, settings: Settings
) -> SeriesPreviewRead:
    if body.library_root_id is not None:
        await _preview_root(session, body.library_root_id)
    runtime = await load_source_runtime(
        session, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
    )
    await session.rollback()
    try:
        result = await preview_source_series(
            MetadataSourceRegistry(
                runtime,
                gcd_api_enabled=settings.metadata_gcd_api_v2_enabled,
                read_cache=source_read_cache(session),
            ),
            body.source,
            body.external_id,
        )
    except SeriesAdoptionError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _require_source_revision(session, body.source, result.source_revision)
    if body.library_root_id is not None:
        root = await _preview_root(session, body.library_root_id)
        if result.series.data is not None:
            try:
                result.folder_preview = await preview_series_folder(
                    session, result.series.data, root
                )
            except (ConfigurationError, ValueError) as exc:
                raise HTTPException(
                    409, "Check this library root's naming settings, then retry the preview."
                ) from exc
    return result


@router.post("/series/issues", response_model=SeriesIssuePageRead)
async def series_issues(
    body: SeriesIssuePageQuery, session: DbSession, _user: AuthenticatedUser, settings: Settings
) -> SeriesIssuePageRead:
    runtime = await load_source_runtime(
        session, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
    )
    if (
        next(item.policy.revision for item in runtime if item.policy.source is body.source)
        != body.source_revision
    ):
        raise HTTPException(409, "Metadata source settings changed. Preview the series again.")
    await session.rollback()
    result = await MetadataSourceRegistry(
        runtime,
        gcd_api_enabled=settings.metadata_gcd_api_v2_enabled,
        read_cache=source_read_cache(session),
    ).issues(body.source, body.external_id, page=body.page)
    await _require_source_revision(session, body.source, body.source_revision)
    return SeriesIssuePageRead(
        **result.model_dump(),
        series_cover_url=representative_series_cover(
            body.source, body.external_id, result.data.results
        )
        if body.page == 1 and result.status is SourceStatus.OK and result.data is not None
        else None,
    )


@router.get("/sources", response_model=list[SourceDescriptor])
async def sources(
    session: DbSession, _user: InteractiveOperatorUser, settings: Settings
) -> list[SourceDescriptor]:
    return await source_status(session, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled)


@router.get("/retries", response_model=PaginatedResponse[DeferredMetadataRead])
async def retries(
    session: DbSession,
    _user: InteractiveOperatorUser,
    settings: Settings,
    source: MetadataSource | None = None,
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> PaginatedResponse[DeferredMetadataRead]:
    return await deferred_work(
        session,
        source=source,
        limit=limit,
        offset=offset,
        gcd_api_enabled=settings.metadata_gcd_api_v2_enabled,
    )


@router.put("/priorities", response_model=list[SourceDescriptor])
async def save_priorities(
    body: SourcePriorityWrite, session: DbSession, user: InteractiveOperatorUser, settings: Settings
) -> list[SourceDescriptor]:
    try:
        await save_source_priorities(session, body)
    except SourceConfigurationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    logger.info("metadata_source_priorities_updated", user_id=user.id)
    return await source_status(session, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled)


@router.post(
    "/sources/gcd_api_v2/sign-in",
    response_model=SourcePolicyRead,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": GcdSignInRequest.model_json_schema(),
                }
            },
        }
    },
)
async def gcd_sign_in(
    request: Request,
    session: DbSession,
    user: InteractiveOperatorUser,
    settings: Settings,
) -> SourcePolicyRead:
    user_id = user.id
    if not settings.metadata_gcd_api_v2_enabled:
        raise HTTPException(400, "GCD API v2 is disabled by the release feature flag.")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Use JSON for GCD sign-in.")
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 32768:
                raise HTTPException(413, "GCD sign-in input is too large.")
            raw.extend(chunk)
        # Default request-validation errors can echo secret inputs. Keep this scoped.
        try:
            body = GcdSignInRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(
                422, "Enter a GCD username, password, and current settings revision."
            ) from None
    finally:
        raw.clear()
    try:
        result = await sign_in_gcd(session, body, gcd_api_enabled=True)
    except SourceConfigurationConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except MetadataSourceError as exc:
        message = {
            SourceStatus.AUTHENTICATION_FAILED: (
                "GCD did not accept the sign-in or returned token. Check your GCD credentials."
            ),
            SourceStatus.RATE_LIMITED: "GCD is rate-limited. Wait before signing in again.",
            SourceStatus.TIMEOUT: "GCD sign-in or its connection check timed out. Try again later.",
        }.get(
            exc.status,
            "GCD sign-in or its connection check failed. "
            "The saved source is unchanged; try again later.",
        )
        raise HTTPException(
            {
                SourceStatus.AUTHENTICATION_FAILED: 400,
                SourceStatus.RATE_LIMITED: 429,
                SourceStatus.TIMEOUT: 504,
            }.get(exc.status, 502),
            message,
            headers={"Retry-After": str(exc.retry_after_seconds)}
            if exc.retry_after_seconds
            else None,
        ) from None
    finally:
        body.clear_credentials()
    logger.info("gcd_sign_in_complete", user_id=user_id, revision=result.revision)
    return result


@router.put("/sources/{source}", response_model=SourcePolicyRead)
async def save_source(
    source: MetadataSource,
    body: SourcePolicyWrite,
    request: Request,
    session: DbSession,
    user: InteractiveOperatorUser,
    settings: Settings,
) -> SourcePolicyRead:
    user_id = user.id
    try:
        candidate = None
        if source is MetadataSource.GCD_LOCAL and body.enabled:
            await _require_source_revision(session, source, body.revision)
            await session.rollback()
            candidate = await validate_for_request(
                body.settings.database_path, request.is_disconnected
            )
            # Prove the semantic joins, not just table names, before activation.
            await GcdLocalSource(candidate).check()
        result = await save_source_policy(
            session, source, body, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
        )
        if candidate is not None:
            await activate_snapshot(session, candidate)
    except SourceConfigurationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except MetadataSourceError as exc:
        raise HTTPException(
            400,
            "The GCD database could not pass its catalog read check. "
            "Validate a current official dump.",
        ) from exc
    logger.info(
        "metadata_source_policy_updated",
        source=source.value,
        revision=result.revision,
        enabled=result.enabled,
        user_id=user_id,
    )
    return result


@router.post("/search", response_model=SeriesDiscoveryRead)
async def search(
    body: SeriesDiscoveryQuery, session: DbSession, _user: AuthenticatedUser, settings: Settings
) -> SeriesDiscoveryRead:
    runtime = await load_source_runtime(
        session, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
    )
    await session.rollback()
    registry = MetadataSourceRegistry(runtime, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled)
    return await registry.discover(body)


@router.post("/sources/{source}/test", response_model=SourceTestRead)
async def test_source(
    source: MetadataSource, session: DbSession, _user: InteractiveOperatorUser, settings: Settings
) -> SourceTestRead:
    runtime = await load_source_runtime(
        session, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
    )
    revision = next(item.policy.revision for item in runtime if item.policy.source is source)
    await session.rollback()
    checked_at = datetime.now(UTC)
    outcome = await MetadataSourceRegistry(
        runtime, gcd_api_enabled=settings.metadata_gcd_api_v2_enabled
    ).check(source, retry_authentication=True)
    recorded = await record_source_health(session, source, revision, outcome, checked_at)
    logger.info(
        "metadata_source_test_complete",
        source=source.value,
        status=outcome.status.value,
        recorded=recorded,
    )
    return SourceTestRead(outcome=outcome, recorded=recorded)
