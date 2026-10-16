"""Server-fetched source metadata adoption; never accepts browser metadata."""

import asyncio
import hashlib
import json
from copy import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import ValidationError
from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.core.metadata_identity import (
    ExactIdentityEvidence,
    ExternalIdentityRef,
    IdentityEvidenceKind,
    IdentityNamespace,
    MetadataEntityKind,
    MetadataSource,
)
from pullbox.core.metadata_identity_events import IdentityEventEvidence, IdentityEventRequest
from pullbox.core.metadata_identity_state import IdentityVerificationAction
from pullbox.core.naming import classify_series_type
from pullbox.models import Issue, Series
from pullbox.models.issue import IssueStatus
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.series import IssueCatalogState, SeriesStatus, SeriesType
from pullbox.schemas.metadata_snapshot import (
    FieldOrigin,
    MetadataSnapshot,
    MetadataValues,
    field_domain,
)
from pullbox.schemas.metadata_sources import (
    CatalogExcludedIssue,
    ProviderIssueRead,
    ProviderSeriesRead,
    SourceStatus,
)
from pullbox.services.metadata_assembly import assemble_metadata
from pullbox.services.metadata_baselines import (
    MetadataBaselineWrite,
    load_metadata_baseline,
    save_metadata_baselines,
)
from pullbox.services.metadata_catalog_checkpoints import (
    CatalogCheckpointConflictError,
    save_full_catalog_checkpoint,
)
from pullbox.services.metadata_credits import write_issue_credits
from pullbox.services.metadata_discovery import MetadataSourceRegistry
from pullbox.services.metadata_identity_attachment import attach_verified_identities
from pullbox.services.metadata_identity_review import record_identity_observation
from pullbox.services.metadata_series_artwork import with_representative_cover
from pullbox.services.metadata_service import MetadataService, classify_issue_metadata
from pullbox.services.metadata_source_reads import source_id
from pullbox.services.metadata_sources import read_source_policies
from pullbox.services.metadata_writer_identity import metadata_write_scope

logger = structlog.get_logger(__name__)


def _metadata_label(source: MetadataSource) -> str:
    # Existing refresh precedence depends on these legacy ComicVine labels.
    return {
        MetadataSource.COMICVINE_API: "comicvine",
        MetadataSource.COMICVINE_LOCAL: "pullbox_catalog",
    }.get(source, source.value)


class SeriesAdoptionError(ValueError):
    """A complete, consistent source catalog could not be adopted safely."""

    def __init__(
        self,
        message: str,
        *,
        status: SourceStatus | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_seconds = retry_after_seconds


class SeriesCatalogCountError(SeriesAdoptionError):
    """Inconsistent counts make this source unusable, not an identity conflict."""


@dataclass(frozen=True)
class SourceIssueBatch:
    source: MetadataSource
    series_external_id: str
    issues: tuple[ProviderIssueRead, ...]
    source_revision: int


@dataclass(frozen=True)
class SourceSeriesBundle:
    series: ProviderSeriesRead
    issues: tuple[ProviderIssueRead, ...]
    source_revision: int
    catalog_total: int
    catalog_started_at: datetime | None = None
    excluded_issues: tuple[ProviderIssueRead, ...] = ()


@dataclass(frozen=True)
class SeriesAdoptionResult:
    series: Series
    created: bool
    snapshot: MetadataSnapshot | None = None
    issue_snapshots: tuple[MetadataSnapshot, ...] = ()


async def fetch_source_series_bundle(
    registry: MetadataSourceRegistry,
    source: MetadataSource,
    external_id: str,
    *,
    source_revision: int,
    max_issues: int = 10000,
    timeout: float = 120,
    profile: ProviderSeriesRead | None = None,
) -> SourceSeriesBundle:
    identifier = source_id(source, MetadataEntityKind.SERIES, external_id)
    runtime = registry.runtime.get(source)
    if runtime is None or runtime.policy.revision != source_revision:
        raise SeriesAdoptionError("Metadata source settings changed. Preview the series again.")
    if type(max_issues) is not int or not 1 <= max_issues <= 10000 or not 0 < timeout <= 120:
        raise ValueError("Invalid adoption resource limits")
    if registry.revalidate_reads and registry.read_cache is not None:
        # A coalesced response may have started before this fetch. Preserve the
        # caller's cache and concurrency budget but read this full catalog live.
        registry = copy(registry)
        registry.read_cache = None
    # Cached pages without revalidation cannot establish a new modification floor.
    started_at = (
        datetime.now(UTC) if registry.read_cache is None or registry.revalidate_reads else None
    )
    try:
        async with asyncio.timeout(timeout):
            if profile is None:
                result_profile = await registry.series(source, identifier)
                if result_profile.status is not SourceStatus.OK or result_profile.data is None:
                    raise SeriesAdoptionError(
                        f"Could not read the series: {result_profile.status.value}.",
                        status=result_profile.status,
                        retry_after_seconds=result_profile.retry_after_seconds,
                    )
                profile = result_profile.data
            if (
                profile.source is not source
                or profile.identity_namespace is not source.identity_namespace
                or profile.external_id != identifier
            ):
                raise SeriesAdoptionError(
                    "The series profile belongs to a different source identity."
                )
            issues: list[ProviderIssueRead] = []
            seen: set[str] = set()
            page = 1
            total: int | None = None
            while True:
                result = await registry.issues(source, identifier, page=page)
                if result.status is not SourceStatus.OK or result.data is None:
                    raise SeriesAdoptionError(
                        f"Could not finish the issue catalog: {result.status.value}. "
                        "Retry the add.",
                        status=result.status,
                        retry_after_seconds=result.retry_after_seconds,
                    )
                current = result.data
                if total is None:
                    total = current.total
                # Summary counts can lag; completeness comes from the paginated catalog.
                if current.total != total:
                    raise SeriesCatalogCountError(
                        "The provider catalog count changed. Retry the add."
                    )
                if total > max_issues:
                    raise SeriesAdoptionError(
                        "The issue catalog exceeds the interactive add limit."
                    )
                if page == 1:
                    profile = with_representative_cover(profile, current.results)
                for issue in current.results:
                    if source in {MetadataSource.COMICVINE_LOCAL, MetadataSource.GCD_LOCAL} and (
                        profile.source_updated_at is None
                        or issue.source_updated_at != profile.source_updated_at
                    ):
                        raise SeriesAdoptionError("The local catalog changed. Retry the add.")
                    if issue.external_id in seen:
                        raise SeriesAdoptionError(
                            "The issue catalog repeats an identity. Retry the add."
                        )
                    seen.add(issue.external_id)
                    issues.append(issue)
                if current.next_page is None:
                    if len(issues) != total:
                        raise SeriesAdoptionError("The issue catalog is incomplete. Retry the add.")
                    if profile.issue_count is not None and profile.issue_count != total:
                        logger.info(
                            "metadata_series_summary_count_disagreement",
                            source=source.value,
                            external_id=identifier,
                            summary_count=profile.issue_count,
                            catalog_count=total,
                        )
                    return SourceSeriesBundle(
                        profile, tuple(issues), source_revision, total, started_at
                    )
                page = current.next_page
    except TimeoutError as exc:
        raise SeriesAdoptionError(
            "The issue catalog request timed out. Retry the add.", status=SourceStatus.TIMEOUT
        ) from exc


async def adopt_source_series_bundle(
    session: AsyncSession, bundle: SourceSeriesBundle, *, monitored: bool = False
) -> SeriesAdoptionResult:
    """Persist a server-owned bundle, leaving transactions and side effects to the caller.

    Add is not refresh: an existing exact owner keeps its metadata, issue catalog,
    monitoring and files. Crosswalk issue IDs remain observations until their
    foreign parent has been verified independently.
    """
    numbers = _validate_bundle(bundle)
    try:
        async with metadata_write_scope(session):
            config = await session.scalar(
                select(MetadataSourceConfig)
                .where(MetadataSourceConfig.source == bundle.series.source.value)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            if config is None or not config.enabled or config.revision != bundle.source_revision:
                raise SeriesAdoptionError(
                    "Metadata source settings changed. Preview the series again."
                )
            return await _adopt(session, bundle, numbers, monitored=monitored)
    except CatalogCheckpointConflictError as exc:
        raise SeriesAdoptionError(
            "Catalog progress could not be saved safely. Preview the series again."
        ) from exc
    except (ValidationError, IntegrityError) as exc:
        raise SeriesAdoptionError(
            "Metadata identity conflicts with an existing match. Review the match before retrying."
        ) from exc


def _identities(
    metadata: ProviderSeriesRead | ProviderIssueRead, kind: MetadataEntityKind
) -> tuple[ExternalIdentityRef, ...]:
    native = ExternalIdentityRef(metadata.source.identity_namespace, kind, metadata.external_id)
    if (
        native.namespace != metadata.identity_namespace
        or native.external_id != metadata.external_id
    ):
        raise SeriesAdoptionError("Metadata does not match the selected source identity.")
    identities = {native.namespace: native}
    for crosswalk in metadata.cross_identities:
        if crosswalk.entity_kind is not kind or (
            crosswalk.namespace in identities and identities[crosswalk.namespace] != crosswalk
        ):
            raise SeriesAdoptionError(
                "Provider identities disagree. Review the match before retrying."
            )
        identities[crosswalk.namespace] = crosswalk
    for identity in identities.values():
        if identity.namespace is IdentityNamespace.COMICVINE and int(identity.external_id) >= 2**63:
            raise SeriesAdoptionError("ComicVine identity exceeds the supported range.")
    return tuple(identities.values())


def _validate_bundle(bundle: SourceSeriesBundle) -> list[tuple[float, str]]:
    profile = bundle.series
    _identities(profile, MetadataEntityKind.SERIES)
    catalog = (*bundle.issues, *bundle.excluded_issues)
    if (
        not profile.title.strip()
        or len(profile.title) > 500
        or len(catalog) > 10000
        or type(bundle.catalog_total) is not int
        or bundle.catalog_total != len(catalog)
    ):
        raise SeriesAdoptionError("The series profile or issue catalog is incomplete.")
    if bundle.excluded_issues:
        if profile.source is not MetadataSource.GCD_LOCAL or not bundle.issues:
            raise SeriesAdoptionError("This catalog cannot be added with exclusions.")
        for issue in bundle.excluded_issues:
            try:
                parse_issue_number_text(issue.issue_number_text)
            except ValueError:
                continue
            raise SeriesAdoptionError("Supported issues cannot be omitted by catalog review.")
        _validate_issue_batch(
            SourceIssueBatch(profile.source, profile.external_id, catalog, bundle.source_revision),
            allow_unsupported=True,
        )
    return _validate_issue_batch(
        SourceIssueBatch(profile.source, profile.external_id, bundle.issues, bundle.source_revision)
    )


def _validate_issue_batch(
    batch: SourceIssueBatch, *, allow_unsupported: bool = False
) -> list[tuple[float, str]]:
    if (
        type(batch.source_revision) is not int
        or batch.source_revision <= 0
        or len(batch.issues) > 10000
    ):
        raise SeriesAdoptionError("Invalid catalog revision or issue batch size.")
    numbers = []
    seen_numbers: set[str] = set()
    seen_ids: set[ExternalIdentityRef] = set()
    for issue in batch.issues:
        if issue.source is not batch.source or issue.series_external_id != batch.series_external_id:
            raise SeriesAdoptionError("An issue belongs to a different source or series.")
        for identity in _identities(issue, MetadataEntityKind.ISSUE):
            if identity in seen_ids:
                raise SeriesAdoptionError("The issue catalog repeats an identity.")
            seen_ids.add(identity)
        try:
            number = parse_issue_number_text(issue.issue_number_text)
        except ValueError as exc:
            if allow_unsupported and batch.source is MetadataSource.GCD_LOCAL:
                continue
            raise SeriesAdoptionError(
                "This catalog contains an unsupported issue designation; no issues were added."
            ) from exc
        if number[1] in seen_numbers:
            raise SeriesAdoptionError(
                "Multiple provider issues use the same designation. Review the catalog."
            )
        seen_numbers.add(number[1])
        numbers.append(number)
    return numbers


def _request(
    bundle: SourceSeriesBundle | SourceIssueBatch,
    metadata: ProviderSeriesRead | ProviderIssueRead,
    identity: ExternalIdentityRef,
    local_id: int,
    *,
    observation: bool = False,
) -> IdentityEventRequest:
    native = ExternalIdentityRef(
        metadata.identity_namespace, identity.entity_kind, metadata.external_id
    )
    # Content digest for evidence/retry identity, never an authentication token.
    revision = hashlib.sha256(
        json.dumps(
            {
                "source_revision": bundle.source_revision,
                "metadata": metadata.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    parent = (
        ExternalIdentityRef(
            metadata.identity_namespace, MetadataEntityKind.SERIES, metadata.series_external_id
        )
        if isinstance(metadata, ProviderIssueRead) and identity == native
        else None
    )
    return IdentityEventRequest(
        UUID(hex=revision[:32]),
        local_id,
        IdentityVerificationAction.OBSERVE if observation else IdentityVerificationAction.VERIFY,
        IdentityEventEvidence(
            ExactIdentityEvidence(
                identity,
                IdentityEvidenceKind.PROVIDER_RESULT
                if identity == native
                else IdentityEvidenceKind.PROVIDER_CROSSWALK,
                metadata.source,
            ),
            revision,
            source_identity=native,
            parent_identity=parent,
        ),
    )


async def _series_owner(
    session: AsyncSession, identities: tuple[ExternalIdentityRef, ...]
) -> Series | None:
    owners = set(
        await session.scalars(
            select(SeriesExternalIdentity.series_id).where(
                tuple_(
                    SeriesExternalIdentity.identity_namespace, SeriesExternalIdentity.external_id
                ).in_([(identity.namespace, identity.external_id) for identity in identities])
            )
        )
    )
    cv = next((item for item in identities if item.namespace is IdentityNamespace.COMICVINE), None)
    if cv is not None:
        owners.update(
            await session.scalars(
                select(Series.id).where(Series.comicvine_id == int(cv.external_id))
            )
        )
    if len(owners) > 1:
        raise SeriesAdoptionError(
            "Provider identities belong to different library series. Review the match."
        )
    if not owners:
        return None
    result = await session.execute(
        select(Series).where(Series.id == next(iter(owners))).with_for_update()
    )
    return result.scalar_one_or_none()


async def _require_unowned_issues(
    session: AsyncSession, bundle: SourceSeriesBundle | SourceIssueBatch
) -> None:
    identities = [
        identity
        for issue in bundle.issues
        for identity in _identities(issue, MetadataEntityKind.ISSUE)
    ]
    for offset in range(0, len(identities), 200):
        batch = identities[offset : offset + 200]
        owner = await session.scalar(
            select(IssueExternalIdentity.issue_id)
            .where(
                tuple_(
                    IssueExternalIdentity.identity_namespace, IssueExternalIdentity.external_id
                ).in_([(identity.namespace, identity.external_id) for identity in batch])
            )
            .limit(1)
        )
        cv_ids = [
            int(item.external_id) for item in batch if item.namespace is IdentityNamespace.COMICVINE
        ]
        legacy = (
            await session.scalar(select(Issue.id).where(Issue.comicvine_id.in_(cv_ids)).limit(1))
            if cv_ids
            else None
        )
        if owner is not None or legacy is not None:
            raise SeriesAdoptionError(
                "An issue already belongs to another library series. Review the match."
            )


async def _adopt(
    session: AsyncSession,
    bundle: SourceSeriesBundle,
    numbers: list[tuple[float, str]],
    *,
    monitored: bool,
) -> SeriesAdoptionResult:
    profile = bundle.series
    identities = _identities(profile, MetadataEntityKind.SERIES)
    series = await _series_owner(session, identities)
    created = series is None
    snapshot = None
    issue_snapshots: tuple[MetadataSnapshot, ...] = ()
    if series is None:
        await _require_unowned_issues(session, bundle)
        policies = await read_source_policies(session)
        now = datetime.now(UTC)
        native_parent = ExternalIdentityRef(
            profile.identity_namespace, MetadataEntityKind.SERIES, profile.external_id
        )
        try:
            snapshot = assemble_metadata(
                MetadataEntityKind.SERIES, identities, [profile], policies, now=now
            )
            issue_snapshots = tuple(
                assemble_metadata(
                    MetadataEntityKind.ISSUE,
                    [
                        ExternalIdentityRef(
                            metadata.identity_namespace,
                            MetadataEntityKind.ISSUE,
                            metadata.external_id,
                        )
                    ],
                    [metadata.model_copy(update={"issue_number_text": text})],
                    policies,
                    now=now,
                    parent_identities=[native_parent],
                )
                for metadata, (_, text) in zip(bundle.issues, numbers, strict=True)
            )
        except ValueError as exc:
            raise SeriesAdoptionError("The source metadata cannot be assembled safely.") from exc
        values = snapshot.values
        publisher_id = (
            await MetadataService._ensure_publisher(session, values.publisher)
            if values.publisher
            else None
        )
        series = Series(
            title=values.title,
            sort_title=values.sort_title or values.title,
            year_start=values.year_start,
            year_end=values.year_end,
            description=values.description,
            cover_url=values.image_url,
            status=SeriesStatus(profile.status)
            if profile.status is not None and profile.status in SeriesStatus
            else SeriesStatus.UNKNOWN,
            series_type=SeriesType(profile.series_type)
            if profile.series_type is not None and profile.series_type in SeriesType
            else SeriesType(
                classify_series_type(
                    profile.title,
                    description=profile.description,
                    issue_count=bundle.catalog_total,
                    year_start=profile.year_start,
                )
            ),
            issue_count=len(bundle.issues),
            catalog_exclusions=[
                CatalogExcludedIssue(
                    source=MetadataSource.GCD_LOCAL,
                    series_external_id=issue.series_external_id,
                    external_id=issue.external_id,
                    issue_number_text=issue.issue_number_text,
                ).model_dump(mode="json")
                for issue in bundle.excluded_issues
            ],
            monitored=monitored,
            publisher_id=publisher_id,
            metadata_source=_metadata_label(profile.source),
            metadata_last_refreshed=profile.source_updated_at or now,
            issue_catalog_state=IssueCatalogState.COMPLETE,
            issue_catalog_last_synced_at=now,
            issue_catalog_last_checked_at=now,
            comicvine_url=profile.resource_url
            if profile.identity_namespace is IdentityNamespace.COMICVINE
            else None,
        )
        session.add(series)
        await session.flush()
    await attach_verified_identities(
        session,
        [_request(bundle, profile, identity, series.id) for identity in identities],
        require_current_ownership=True,
    )
    await session.refresh(series, attribute_names=["comicvine_id"])
    if not created:
        logger.info(
            "metadata_series_add_existing", series_id=series.id, source=profile.source.value
        )
        return SeriesAdoptionResult(series, False)
    for offset in range(0, len(bundle.issues), 200):
        members = []
        for metadata, (number, text), issue_snapshot in zip(
            bundle.issues[offset : offset + 200],
            numbers[offset : offset + 200],
            issue_snapshots[offset : offset + 200],
            strict=True,
        ):
            issue_values = issue_snapshot.values
            issue = Issue(
                series_id=series.id,
                issue_number=number,
                issue_number_text=text,
                title=issue_values.title,
                description=issue_values.description,
                release_date=issue_values.cover_date,
                store_date=issue_values.store_date,
                cover_url=issue_values.image_url,
                page_count=issue_values.page_count,
                status=IssueStatus.WANTED if monitored else IssueStatus.SKIPPED,
                metadata_source=_metadata_label(metadata.source),
                issue_type=classify_issue_metadata(series.series_type, metadata.title)[1],
                comicvine_url=metadata.resource_url
                if metadata.identity_namespace is IdentityNamespace.COMICVINE
                else None,
            )
            session.add(issue)
            members.append((issue, metadata))
        await session.flush()
        await attach_verified_identities(
            session,
            [
                _request(
                    bundle, metadata, _identities(metadata, MetadataEntityKind.ISSUE)[0], issue.id
                )
                for issue, metadata in members
            ],
        )
        for issue, metadata in members:
            for crosswalk in _identities(metadata, MetadataEntityKind.ISSUE)[1:]:
                await record_identity_observation(
                    session, _request(bundle, metadata, crosswalk, issue.id, observation=True)
                )
        await write_issue_credits(
            session,
            {
                issue.id: item.values.credits
                for (issue, _), item in zip(
                    members, issue_snapshots[offset : offset + 200], strict=True
                )
            },
        )
        await save_metadata_baselines(
            session,
            [
                MetadataBaselineWrite(issue.id, issue_snapshot)
                for (issue, _), issue_snapshot in zip(
                    members, issue_snapshots[offset : offset + 200], strict=True
                )
            ],
        )
    await session.flush()
    if profile.status is None:
        await MetadataService.infer_series_status(session, series)
    if snapshot is not None:
        snapshot = await persist_adoption_series_baseline(session, series, snapshot)
    if bundle.catalog_started_at is not None:
        claim = await session.scalar(
            select(SeriesExternalIdentity).where(
                SeriesExternalIdentity.series_id == series.id,
                SeriesExternalIdentity.identity_namespace == profile.identity_namespace,
            )
        )
        assert claim is not None
        await save_full_catalog_checkpoint(
            session,
            series.id,
            source=profile.source,
            source_revision=bundle.source_revision,
            identity_revision=claim.revision,
            external_id=profile.external_id,
            started_at=bundle.catalog_started_at,
            source_updated_at=profile.source_updated_at,
        )
    logger.info(
        "metadata_series_added",
        series_id=series.id,
        source=profile.source.value,
        issue_count=len(bundle.issues),
    )
    return SeriesAdoptionResult(series, True, snapshot, issue_snapshots)


async def persist_adoption_series_baseline(
    session: AsyncSession, series: Series, snapshot: MetadataSnapshot
) -> MetadataSnapshot:
    """Record Add's inferred defaults, not invented provider data or user edits."""
    values = snapshot.values.model_dump()
    origins = {origin.field: origin for origin in snapshot.origins}
    now = datetime.now(UTC)
    derived = {
        "sort_title": FieldOrigin(
            field="sort_title",
            domain=field_domain(MetadataEntityKind.SERIES, "sort_title"),
            observed_at=now,
            derivation="normalization",
        ),
        "series_type": FieldOrigin(
            field="series_type",
            domain=field_domain(MetadataEntityKind.SERIES, "series_type"),
            observed_at=now,
            derivation="classification",
        ),
        "status": FieldOrigin(
            field="status",
            domain=field_domain(MetadataEntityKind.SERIES, "status"),
            observed_at=now,
            derivation="lifecycle",
        ),
        "year_end": FieldOrigin(
            field="year_end",
            domain=field_domain(MetadataEntityKind.SERIES, "year_end"),
            observed_at=now,
            derivation="lifecycle",
        ),
        "issue_count": FieldOrigin(
            field="issue_count",
            domain=field_domain(MetadataEntityKind.SERIES, "issue_count"),
            observed_at=now,
            derivation="catalog",
        ),
    }
    actual = {
        "sort_title": series.sort_title,
        "series_type": series.series_type.value,
        "status": series.status.value,
        "year_end": series.year_end,
        "issue_count": series.issue_count,
    }
    for field, value in actual.items():
        if values[field] != value:
            values[field] = value
            origins[field] = derived[field]
    snapshot = MetadataSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "values": MetadataValues.model_validate(values),
            "origins": tuple(origins.values()),
        }
    )
    saved = await load_metadata_baseline(session, MetadataEntityKind.SERIES, series.id)
    if saved is None or saved.snapshot != snapshot:
        await save_metadata_baselines(
            session, [MetadataBaselineWrite(series.id, snapshot, saved.revision if saved else 0)]
        )
    return snapshot
