"""Confirmed passive discovery links retain ownership/audit on SQLite and PostgreSQL."""

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Series, User
from pullbox.models.metadata_identity import SeriesExternalIdentity, SeriesIdentityEvent
from pullbox.models.whats_new import WhatsNewCacheKind, WhatsNewReleaseCache
from pullbox.services.whats_new_actions import (
    WhatsNewSelectionError,
    link_confirmed_release,
    load_release_selection,
)
from tests.ui.test_whats_new_ui_routes import _issue_summary


async def setup(factory):
    async with factory.begin() as session:
        series = Series(title="Chosen series", sort_title="chosen series")
        user = User(username="discovery-reviewer", password_hash="unused")
        cache = WhatsNewReleaseCache(
            cache_key="discovery:2026-03-11",
            cache_kind=WhatsNewCacheKind.CURRENT_WEEK,
            store_date=date(2026, 3, 11),
            payload={"issues": [_issue_summary()]},
            fetched_at=datetime.now(UTC),
            last_successful_refresh_at=datetime.now(UTC),
        )
        session.add_all([series, user, cache])
        await session.flush()
        selected = await load_release_selection(session, cache.id, 1514020)
        return series.id, user.id, selected.selection


async def test_confirmation_and_repeat_have_one_owner_and_user_audit(identity_probe_db):
    _, factory, _ = identity_probe_db
    series, user, selection = await setup(factory)
    for _ in range(2):
        async with factory.begin() as session:
            await link_confirmed_release(session, selection, series, user)
    async with factory() as session:
        claim = await session.scalar(select(SeriesExternalIdentity))
        assert claim.identity_namespace is IdentityNamespace.LOCG
        assert claim.verification_state is IdentityVerificationState.VERIFIED
        assert claim.series_id == series and claim.external_id == "180901"
        assert claim.evidence_kind == "user_selection"
        assert await session.scalar(select(func.count()).select_from(SeriesIdentityEvent)) == 1
        event = await session.scalar(select(SeriesIdentityEvent))
        audit = json.loads(event.request_json)
        assert audit["actor"] == "user" and audit["actor_user_id"] == user
        assert (await session.get(Series, series)).comicvine_id is None


@pytest.mark.parametrize(
    "state",
    [
        IdentityVerificationState.VERIFIED,
        IdentityVerificationState.STALE,
        IdentityVerificationState.CONFLICTED,
    ],
)
async def test_other_owner_or_disputed_claim_cannot_be_reassigned(identity_probe_db, state):
    _, factory, _ = identity_probe_db
    series, user, selection = await setup(factory)
    async with factory.begin() as session:
        other = Series(title="Other", sort_title="other")
        session.add(other)
        await session.flush()
        other_id = other.id
        session.add(
            SeriesExternalIdentity(
                series_id=other_id,
                identity_namespace=IdentityNamespace.LOCG,
                external_id="180901",
                evidence_kind="user_selection",
                verification_state=state,
            )
        )
    with pytest.raises(WhatsNewSelectionError, match="disputed"):
        async with factory.begin() as session:
            await link_confirmed_release(session, selection, series, user)
    async with factory() as session:
        assert (await session.scalar(select(SeriesExternalIdentity))).series_id == other_id
        assert await session.scalar(select(func.count()).select_from(SeriesIdentityEvent)) == 0
