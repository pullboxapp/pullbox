"""Fresh release observations request confirmation, never guess an automatic Add."""

import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.models import Series, SeriesInterest, SeriesInterestState
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
