"""Read-only refresh cascade; callers revalidate ownership before applying a snapshot."""

import asyncio
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from pullbox.core.metadata_identity import ExternalIdentityRef, MetadataEntityKind, MetadataSource
from pullbox.schemas.metadata_snapshot import MetadataSnapshot, MetadataValues, field_domain
from pullbox.schemas.metadata_sources import (
    ProviderIssueRead,
    ProviderSeriesRead,
    ProviderStoryArcRead,
    SourceCapability,
    SourceOutcome,
    SourceStatus,
)
from pullbox.services.metadata_assembly import (
    MetadataAssemblyError,
    MetadataCandidate,
    assemble_metadata,
    missing_metadata_value,
)
from pullbox.services.metadata_discovery import MetadataSourceRegistry


class MetadataRefreshSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    snapshot: MetadataSnapshot
    outcomes: tuple[SourceOutcome, ...]
    revisions: dict[MetadataSource, int]
    candidates: tuple[MetadataCandidate, ...] = ()


async def fetch_metadata_snapshot(
    registry: MetadataSourceRegistry,
    kind: MetadataEntityKind,
    identities: Sequence[ExternalIdentityRef],
    *,
    now: datetime,
    requested_fields: frozenset[str],
    current: MetadataValues | None = None,
    previous: MetadataSnapshot | None = None,
    overrides: frozenset[str] = frozenset(),
    replace_managed: bool = False,
    parent_identities: Sequence[ExternalIdentityRef] = (),
    initial_candidates: Sequence[MetadataCandidate] = (),
) -> MetadataRefreshSnapshot:
    """Fetch only attached identities and stop each domain at its first usable value.

    No writes, searches, identity attachment, file operations or catalog membership
    changes occur here. A success is evidence for a later revision-checked write,
    not permission to apply metadata after ownership/configuration has changed.
    """
    fields = {
        MetadataEntityKind.SERIES: ProviderSeriesRead.model_fields.keys(),
        MetadataEntityKind.ISSUE: ProviderIssueRead.model_fields.keys(),
        MetadataEntityKind.STORY_ARC: ProviderStoryArcRead.model_fields.keys(),
    }[kind]
    if not requested_fields or requested_fields - (fields & MetadataValues.model_fields.keys()):
        raise ValueError("Choose supported metadata fields for this entity kind.")
    # Refresh must not mistake a fresh preview cache entry for an upstream check.
    reader = MetadataSourceRegistry(
        list(registry.runtime.values()),
        factories=registry.factories,
        gcd_api_enabled=registry.gcd_api_enabled,
        per_source_timeout=registry.per_source_timeout,
        total_timeout=registry.total_timeout,
        concurrency=registry.concurrency,
        read_cache=registry.read_cache,
        revalidate_reads=True,
    )
    policies = [runtime.policy for runtime in reader.runtime.values()]
    candidates: list[MetadataCandidate] = []

    def assemble(*, local: bool) -> MetadataSnapshot:
        return assemble_metadata(
            kind,
            identities,
            candidates,
            policies,
            now=now,
            current=current if local else None,
            previous=previous if local else None,
            overrides=overrides if local else frozenset(),
            replace_managed=replace_managed,
            parent_identities=parent_identities,
            fields=requested_fields,
        )

    snapshot = assemble(local=True)
    initial = {origin.field: origin for origin in snapshot.origins}
    needed = set()
    for field in requested_fields:
        value = getattr(snapshot.values, field)
        missing = missing_metadata_value(value)
        origin = initial.get(field)
        if origin is not None and origin.user_override:
            continue
        if missing or (
            replace_managed
            and origin is not None
            and (
                origin.source is not None
                or origin.derivation is not None
                or origin.provider_managed
            )
        ):
            needed.add(field)
    capability = {
        MetadataEntityKind.SERIES: SourceCapability.SERIES_DETAILS,
        MetadataEntityKind.ISSUE: SourceCapability.ISSUE_DETAILS,
        MetadataEntityKind.STORY_ARC: SourceCapability.STORY_ARC_DETAILS,
    }[kind]
    known = {identity.namespace: identity.external_id for identity in identities}
    outcomes = {}
    remaining = set()
    for source in reader.runtime:
        if source.identity_namespace not in known:
            continue
        status = reader._unavailable(source, capability=capability)
        outcomes[source] = SourceOutcome(source=source, status=status or SourceStatus.NOT_QUERIED)
        if status is None:
            if kind is MetadataEntityKind.ISSUE and not any(
                parent.namespace is source.identity_namespace for parent in parent_identities
            ):
                raise MetadataAssemblyError("Issue refresh requires verified parent identities.")
            remaining.add(source)

    # A freshly fetched catalog may already include this exact descriptive row.
    # Validate it through the same assembler, but still consult higher priorities.
    candidates.extend(initial_candidates)
    snapshot = assemble(local=True)
    for candidate in initial_candidates:
        remaining.discard(candidate.source)
        outcomes[candidate.source] = SourceOutcome(source=candidate.source, status=SourceStatus.OK)

    def rank(source: MetadataSource, field: str) -> tuple[int, str]:
        policy = reader.runtime[source].policy
        domain = field_domain(kind, field)
        return policy.domain_priorities.get(domain, policy.priority), source.value

    revisions = {source: runtime.policy.revision for source, runtime in reader.runtime.items()}
    deadline = asyncio.get_running_loop().time() + reader.total_timeout
    while remaining and needed:
        offered = assemble(local=False)
        winners = {origin.field: origin.source for origin in offered.origins}
        choices = []
        for field in needed:
            winner = winners.get(field)
            eligible = [
                source
                for source in remaining
                if (
                    field != "image_url"
                    or source not in {MetadataSource.GCD_LOCAL, MetadataSource.GCD_API_V2}
                )
                and (winner is None or rank(source, field) < rank(winner, field))
            ]
            if eligible:
                source = min(eligible, key=lambda source: rank(source, field))
                choices.append((rank(source, field), source))
        if not choices:
            break
        _, source = min(choices)
        remaining.remove(source)
        identifier = known[source.identity_namespace]
        try:
            async with asyncio.timeout_at(deadline):
                result = await (
                    reader.series(source, identifier)
                    if kind is MetadataEntityKind.SERIES
                    else reader.issue(source, identifier)
                    if kind is MetadataEntityKind.ISSUE
                    else reader.story_arc(source, identifier)
                )
        except TimeoutError:
            outcomes[source] = SourceOutcome(source=source, status=SourceStatus.TIMEOUT)
            break
        outcomes[source] = SourceOutcome(
            source=source, status=result.status, retry_after_seconds=result.retry_after_seconds
        )
        if result.status is SourceStatus.INCOMPATIBLE_RESPONSE:
            # The read guard also uses this status for wrong native IDs/parents.
            # Falling back would let provider priority conceal exact disagreement.
            raise MetadataAssemblyError("Source metadata requires review before refresh.")
        if result.status is SourceStatus.OK and result.data is not None:
            candidates.append(result.data)
            snapshot = assemble(local=True)
    return MetadataRefreshSnapshot(
        snapshot=snapshot,
        outcomes=tuple(outcomes.values()),
        revisions=revisions,
        candidates=tuple(candidates),
    )
