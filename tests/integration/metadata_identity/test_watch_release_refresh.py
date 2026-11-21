"""Fresh release observations request confirmation, never guess an automatic Add."""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.models import (
    Issue,
    LibraryRoot,
    Series,
    SeriesExternalIdentity,
    SeriesInterest,
    SeriesInterestState,
)
from pullbox.models.issue import IssueStatus
from pullbox.schemas.whats_new import WhatsNewWatchRequest
from pullbox.services.series_interest import cancel_watch, save_watch
from pullbox.services.whats_new_refresh_queue import run_whats_new_refresh
from tests.integration.metadata_identity.test_series_interest import prepare
from tests.ui.test_whats_new_ui_routes import _issue_summary


@pytest.fixture
def watch_refresh_enabled(monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class ReleaseClient:
    def __init__(self, releases, *, upcoming=False):
        self.releases = releases
        self.upcoming = upcoming

    async def get_current_week(self):
        return {
            "store_date": date.today().isoformat(),
            "issues": [] if self.upcoming else self.releases,
        }

    async def get_upcoming(self):
        return {
            "weeks": [{"store_date": date.today().isoformat(), "issues": self.releases}]
            if self.upcoming
            else [],
        }


async def saved_watch(factory, tmp_path):
    _, actor, contexts = await prepare(factory, tmp_path / "root")
    async with factory.begin() as session:
        interest = await save_watch(
            session, WhatsNewWatchRequest(selection=contexts[0].selection), actor
        )
        return interest.id, actor


@pytest.mark.parametrize(
    "case",
    [
        "tracked",
        "previous_confirmation",
        "corroborated",
        "published_next_issue_upcoming",
        "cross_feed_disagreement",
        "cancelled",
        "paused",
        "wrong_root",
        "disabled_root",
        "offline_root",
        "stale_link",
        "unknown_publication",
        "yesterday",
        "conflicting_variant",
        "foreign_disagreement",
        "malformed_id",
        "flag_off",
    ],
)
async def test_refresh_settles_only_verified_published_local_watch(
    identity_probe_db, tmp_path, watch_refresh_enabled, monkeypatch, case
):
    _, factory, _ = identity_probe_db
    interest_id, actor = await saved_watch(factory, tmp_path)
    async with factory.begin() as session:
        interest = await session.get(SeriesInterest, interest_id)
        if case == "previous_confirmation":
            interest.state = SeriesInterestState.NEEDS_CONFIRMATION
        if case == "cancelled":
            await cancel_watch(session, interest_id, actor)
        root = await session.get(LibraryRoot, interest.target_library_root_id)
        series = Series(
            title="Different display title",
            sort_title="different",
            monitored=case != "paused",
            preferred_library_root_id=None if case == "wrong_root" else root.id,
        )
        session.add(series)
        await session.flush()
        series_id = series.id
        session.add(Issue(series_id=series_id, issue_number=1, status=IssueStatus.SKIPPED))
        for namespace, identity in [("locg", interest.source_series_id), ("metron", "998")]:
            session.add(
                SeriesExternalIdentity(
                    series_id=series.id,
                    identity_namespace=namespace,
                    external_id=identity,
                    verification_state="stale"
                    if case == "stale_link" and namespace == "locg"
                    else "verified",
                    evidence_kind="user_selection",
                )
            )
        if case == "disabled_root":
            root.enabled = False
    if case == "offline_root":
        (tmp_path / "root").rmdir()
    if case == "flag_off":
        monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "false")
        get_settings.cache_clear()
    today = datetime.now(UTC).date()
    release = _issue_summary()
    release["store_date"] = date.today().isoformat()
    fields = dict(
        release["series"], publication_state="published", publication_as_of=today.isoformat()
    )
    if case == "yesterday":
        fields["publication_as_of"] = (today - timedelta(days=1)).isoformat()
    if case == "unknown_publication":
        fields["publication_state"] = "unknown"
    if case in {"corroborated", "foreign_disagreement", "malformed_id"}:
        fields["metron_series_id"] = {
            "corroborated": 998,
            "foreign_disagreement": 999,
            "malformed_id": True,
        }[case]
    release["series"] = fields
    if case == "published_next_issue_upcoming":
        release["store_date"] = (today + timedelta(days=30)).isoformat()
    other = {**release, "locg_series_id": 123} if case == "conflicting_variant" else release
    client = ReleaseClient([release, other], upcoming=case == "published_next_issue_upcoming")
    if case == "cross_feed_disagreement":

        class ContradictoryClient(ReleaseClient):
            async def get_upcoming(self):
                variant = {**release, "series": {**fields, "publication_state": "unknown"}}
                return {"weeks": [{"store_date": today.isoformat(), "issues": [variant]}]}

        client = ContradictoryClient([release])
    for _ in range(2):
        await run_whats_new_refresh(session_factory=factory, client=client)
    async with factory() as session:
        interest = await session.get(SeriesInterest, interest_id)
        promoted = case in {
            "tracked",
            "previous_confirmation",
            "corroborated",
            "published_next_issue_upcoming",
        }
        expected = {
            "cancelled": SeriesInterestState.CANCELLED,
            "flag_off": SeriesInterestState.WATCHING,
        }
        assert interest.state is (
            SeriesInterestState.PROMOTED
            if promoted
            else expected.get(case, SeriesInterestState.NEEDS_CONFIRMATION)
        )
        assert interest.resolved_series_id == (series_id if promoted else None)
        assert interest.last_actor_user_id == actor
        assert (await session.get(Series, series_id)).monitored == (case != "paused")
        assert await session.scalar(select(Issue.status)) == IssueStatus.SKIPPED
        assert await session.scalar(select(func.count()).select_from(Series)) == 1
        assert await session.scalar(select(func.count()).select_from(SeriesExternalIdentity)) == 2
    assert not list((tmp_path / "root").glob("*"))


@pytest.mark.parametrize("upcoming", [False, True])
async def test_available_release_requires_confirmation_without_creating_series(
    identity_probe_db, tmp_path, watch_refresh_enabled, upcoming
):
    _, factory, _ = identity_probe_db
    interest_id, _ = await saved_watch(factory, tmp_path)
    release = {**_issue_summary(), "store_date": date.today().isoformat()}
    client = ReleaseClient([release, release], upcoming=upcoming)
    await run_whats_new_refresh(session_factory=factory, client=client)
    await run_whats_new_refresh(session_factory=factory, client=client)
    async with factory() as session:
        interest = await session.get(SeriesInterest, interest_id)
        assert interest.state is SeriesInterestState.NEEDS_CONFIRMATION
        assert interest.resolved_series_id is None
        assert await session.scalar(select(func.count()).select_from(Series)) == 0
    assert not any((tmp_path / "root").iterdir())


@pytest.mark.parametrize(
    "case", ["future", "absent", "invalid_date", "conflicting_id", "no_id", "other_id", "no_issue"]
)
async def test_unavailable_or_invalid_observations_leave_watch_unchanged(
    identity_probe_db, tmp_path, watch_refresh_enabled, case
):
    _, factory, _ = identity_probe_db
    interest_id, _ = await saved_watch(factory, tmp_path)
    release = {**_issue_summary(), "store_date": date.today().isoformat()}
    if case == "future":
        release["store_date"] = (date.today() + timedelta(days=14)).isoformat()
    elif case == "invalid_date":
        release["store_date"] = "not-a-date"
    elif case == "conflicting_id":
        release["locg_series_id"] = 999999
    elif case == "no_id":
        release["locg_series_id"] = None
        release["series"] = {**release["series"], "locg_series_id": None}
    elif case == "other_id":
        release["locg_series_id"] = 999999
        release["series"] = {**release["series"], "locg_series_id": 999999}
    elif case == "no_issue":
        release["locg_issue_id"] = True
    await run_whats_new_refresh(
        session_factory=factory, client=ReleaseClient([] if case == "absent" else [release])
    )
    async with factory() as session:
        assert (
            await session.get(SeriesInterest, interest_id)
        ).state is SeriesInterestState.WATCHING


async def test_failed_refresh_preserves_saved_watch(
    identity_probe_db, tmp_path, watch_refresh_enabled
):
    _, factory, _ = identity_probe_db
    interest_id, _ = await saved_watch(factory, tmp_path)

    class FailingClient(ReleaseClient):
        async def get_upcoming(self):
            raise RuntimeError("upstream unavailable")

    with pytest.raises(RuntimeError, match="upstream unavailable"):
        await run_whats_new_refresh(
            session_factory=factory,
            client=FailingClient([{**_issue_summary(), "store_date": date.today().isoformat()}]),
        )
    async with factory() as session:
        assert (
            await session.get(SeriesInterest, interest_id)
        ).state is SeriesInterestState.WATCHING


async def test_cancel_during_fetch_cannot_be_revived_by_refresh(
    identity_probe_db, tmp_path, watch_refresh_enabled
):
    _, factory, _ = identity_probe_db
    interest_id, actor = await saved_watch(factory, tmp_path)
    entered = asyncio.Event()
    resume = asyncio.Event()

    class HeldClient(ReleaseClient):
        async def get_upcoming(self):
            entered.set()
            await resume.wait()
            return await super().get_upcoming()

    task = asyncio.create_task(
        run_whats_new_refresh(
            session_factory=factory,
            client=HeldClient([{**_issue_summary(), "store_date": date.today().isoformat()}]),
        )
    )
    try:
        await entered.wait()
        async with factory.begin() as session:
            await cancel_watch(session, interest_id, actor)
    finally:
        resume.set()
        await task
    async with factory() as session:
        assert (
            await session.get(SeriesInterest, interest_id)
        ).state is SeriesInterestState.CANCELLED


async def test_refresh_feature_flag_off_does_not_change_watch(
    identity_probe_db, tmp_path, watch_refresh_enabled, monkeypatch
):
    _, factory, _ = identity_probe_db
    interest_id, _ = await saved_watch(factory, tmp_path)
    monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "false")
    get_settings.cache_clear()
    await run_whats_new_refresh(
        session_factory=factory,
        client=ReleaseClient([{**_issue_summary(), "store_date": date.today().isoformat()}]),
    )
    async with factory() as session:
        assert (
            await session.get(SeriesInterest, interest_id)
        ).state is SeriesInterestState.WATCHING


async def test_promoted_watch_history_is_not_reopened_by_refresh(
    identity_probe_db, tmp_path, watch_refresh_enabled
):
    _, factory, _ = identity_probe_db
    interest_id, _ = await saved_watch(factory, tmp_path)
    async with factory.begin() as session:
        interest = await session.get(SeriesInterest, interest_id)
        interest.state = SeriesInterestState.PROMOTED
    await run_whats_new_refresh(
        session_factory=factory,
        client=ReleaseClient([{**_issue_summary(), "store_date": date.today().isoformat()}]),
    )
    async with factory() as session:
        assert (
            await session.get(SeriesInterest, interest_id)
        ).state is SeriesInterestState.PROMOTED
