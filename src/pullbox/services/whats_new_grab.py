"""Cache-bound admission for the existing interactive search and selected Grab."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from sqlalchemy import and_, literal, or_, select

from pullbox.config import get_settings
from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.models.airdcpp import AirDcppClientSettings
from pullbox.models.client import DownloadClientConfig
from pullbox.models.direct_acquisition import DirectProviderConfig, DirectProviderState
from pullbox.models.download import DownloadClientType
from pullbox.models.indexer import IndexerConfig, IndexerType
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.services.whats_new_actions import (
    WhatsNewSelectionError,
    local_release_series,
    positive_id,
    release_series_id,
)
from pullbox.services.whats_new_issue_state import (
    ReleaseIssueState,
    ReleaseIssueStatus,
    local_release_issues,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.selectable import Exists

    from pullbox.schemas.whats_new import WhatsNewIssueSelection


def cache_fingerprint(payload: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _number(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return normalize_issue_number_text(value)
    except ValueError:
        return None


async def validate_issue_selection(
    session: AsyncSession, selection: WhatsNewIssueSelection, *, require_missing: bool = True
) -> ReleaseIssueState:
    if not get_settings().metadata_whats_new_actions_enabled:
        raise WhatsNewSelectionError("Release discovery actions are not enabled.")
    row = await session.scalar(
        select(WhatsNewReleaseCache)
        .where(WhatsNewReleaseCache.id == selection.cache_id)
        .execution_options(populate_existing=True)
    )
    if row is None or cache_fingerprint(row.payload) != selection.fingerprint:
        raise WhatsNewSelectionError("These releases changed. Reload What's New and try again.")
    groups = [(row.payload.get("issues"), row.payload.get("store_date"))]
    weeks = row.payload.get("weeks")
    if isinstance(weeks, list):
        groups.extend(
            (week.get("issues"), week.get("store_date")) for week in weeks if isinstance(week, dict)
        )
    sources = [
        {**source, "store_date": source.get("store_date") or group_date}
        for group, group_date in groups
        if isinstance(group, list)
        for source in group
        if isinstance(source, dict)
    ]
    selected = [
        s for s in sources if positive_id(s.get("locg_issue_id")) == str(selection.release_id)
    ]
    if not selected:
        raise WhatsNewSelectionError("This release was removed. Reload What's New.")
    source = selected[0]
    identity = release_series_id(source)
    if not identity:
        raise WhatsNewSelectionError("This release has no confirmed series link.")
    # Recheck all copies of this discovery number/date, not just its primary cover.
    siblings = list(selected)
    for candidate in sources:
        if candidate in siblings or (
            _number(candidate.get("issue_number")) != _number(source.get("issue_number"))
            or candidate.get("store_date") != source.get("store_date")
        ):
            continue
        nested = candidate.get("series")
        candidate_ids = {
            positive_id(candidate.get("locg_series_id")),
            positive_id(nested.get("locg_series_id")) if isinstance(nested, dict) else None,
        }
        if identity in candidate_ids:
            siblings.append(candidate)
    owners = await local_release_series(session, [identity])
    states = await local_release_issues(
        session, [{**source, "_discovery_releases": siblings}], owners
    )
    state = states[0]
    if state is None or state.issue_id != selection.issue_id:
        raise WhatsNewSelectionError("This release's issue match changed. Reload What's New.")
    if require_missing and state.state is not ReleaseIssueStatus.MISSING:
        raise WhatsNewSelectionError(
            f"This issue is now {state.label.lower()}. "
            "Reload What's New before searching or grabbing."
        )
    return state


async def has_grab_capability(session: AsyncSession) -> bool:
    """Configuration-only projection in one SELECT; never decrypt or contact a source."""

    def client(types: tuple[DownloadClientType, ...]) -> Exists:
        return (
            select(DownloadClientConfig.id)
            .where(
                DownloadClientConfig.enabled.is_(True), DownloadClientConfig.client_type.in_(types)
            )
            .exists()
        )

    def indexer(types: tuple[IndexerType, ...]) -> Exists:
        return (
            select(IndexerConfig.id)
            .where(
                IndexerConfig.enabled.is_(True),
                IndexerConfig.manager_available.is_(True),
                IndexerConfig.enable_interactive_search.is_(True),
                IndexerConfig.indexer_type.in_(types),
            )
            .exists()
        )

    direct = (
        select(DirectProviderConfig.id)
        .where(
            DirectProviderConfig.enabled.is_(True),
            DirectProviderConfig.state.in_(
                (DirectProviderState.HEALTHY, DirectProviderState.DEGRADED)
            ),
        )
        .exists()
    )
    dc = (
        select(DownloadClientConfig.id)
        .join(AirDcppClientSettings)
        .where(
            DownloadClientConfig.enabled.is_(True),
            DownloadClientConfig.client_type == DownloadClientType.AIRDCPP,
            AirDcppClientSettings.search_enabled.is_(True),
        )
        .exists()
    )
    return bool(
        await session.scalar(
            select(
                or_(
                    direct,
                    and_(dc, literal(get_settings().airdcpp_enabled)),
                    and_(
                        indexer((IndexerType.NEWZNAB, IndexerType.PROWLARR)),
                        client((DownloadClientType.SABNZBD, DownloadClientType.NZBGET)),
                    ),
                    and_(
                        indexer((IndexerType.TORZNAB, IndexerType.PROWLARR)),
                        client(
                            (
                                DownloadClientType.QBITTORRENT,
                                DownloadClientType.TRANSMISSION,
                                DownloadClientType.DELUGE,
                            )
                        ),
                    ),
                )
            )
        )
    )
