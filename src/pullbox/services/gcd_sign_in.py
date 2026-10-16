"""One-time GCD credentials never become provider configuration."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.metadata_identity import MetadataSource
from pullbox.providers.metadata import gcd_api_v2
from pullbox.schemas.metadata_sources import (
    GcdSignInRequest,
    SourcePolicyRead,
    SourcePolicyWrite,
    SourceStatus,
)
from pullbox.services.metadata_discovery import MetadataSourceError, MetadataSourceRegistry
from pullbox.services.metadata_sources import (
    SourceConfigurationConflictError,
    SourceRuntime,
    read_source_policies,
    record_source_health,
    save_source_policy,
)


async def sign_in_gcd(
    session: AsyncSession,
    body: GcdSignInRequest,
    *,
    gcd_api_enabled: bool,
) -> SourcePolicyRead:
    source = MetadataSource.GCD_API_V2
    try:
        if not gcd_api_enabled:
            raise MetadataSourceError(SourceStatus.FEATURE_DISABLED)
        base = next(item for item in await read_source_policies(session) if item.source is source)
        if base.revision != body.revision:
            raise SourceConfigurationConflictError(
                "Source settings changed; reload before signing in"
            )
        await session.rollback()
        try:
            token = await gcd_api_v2.exchange_gcd_token(body.username, body.password)
        finally:
            body.clear_credentials()
        candidate = base.model_copy(update={"enabled": True, "credential_configured": True})
        checked_at = datetime.now(UTC)
        outcome = await MetadataSourceRegistry(
            [SourceRuntime(candidate, token)],
            gcd_api_enabled=True,
        ).check(source, retry_authentication=True)
        if outcome.status is not SourceStatus.OK:
            raise MetadataSourceError(outcome.status, outcome.retry_after_seconds)
        saved = await save_source_policy(
            session,
            source,
            SourcePolicyWrite(
                revision=base.revision,
                enabled=True,
                priority=base.priority,
                domain_priorities=base.domain_priorities,
                settings=base.settings,
                credential=token,
            ),
            gcd_api_enabled=True,
        )
        if not await record_source_health(session, source, saved.revision, outcome, checked_at):
            raise SourceConfigurationConflictError(
                "Source settings changed; reload before signing in"
            )
        return next(item for item in await read_source_policies(session) if item.source is source)
    finally:
        body.clear_credentials()
