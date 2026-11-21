"""A release refresh may add a series only with a live Watch and exact evidence."""

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.core.events import EventBus
from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
    MetadataEntityKind,
    MetadataSource,
)
from pullbox.models import Issue, LibraryRoot, Series, SeriesExternalIdentity, SeriesInterest
from pullbox.models.issue import IssueStatus
from pullbox.models.metadata_identity import SeriesIdentityEvent
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.providers.metadata import sources
from pullbox.schemas.metadata_sources import SourceCapability
from pullbox.services.catalog import reader as catalog_reader
from pullbox.services.series_interest import cancel_watch
from pullbox.services.whats_new_refresh_queue import (
    refresh_whats_new_cache_if_needed,
    run_whats_new_refresh,
)
from pullbox.services.whats_new_refresh_service import WhatsNewRefreshService
from pullbox.services.whats_new_watch_add import finish_watch_adds
from tests.integration.metadata_identity.test_watch_release_refresh import (
    ReleaseClient,
    saved_watch,
)
from tests.ui.test_whats_new_ui_routes import _issue_summary
from tests.unit.test_catalog_reader import installed_reader
from tests.unit.test_metadata_discovery import registration
from tests.unit.test_metadata_series_adoption_fetch import CatalogAdapter


@pytest.mark.parametrize(
    "case",
    [
        "exact",
        "ambiguous_owner",
        "unsupported_claim",
        "cancel_during_read",
        "unavailable",
        "root_changed",
        "superseded",
        "prior_rejection",
        "paused_owner",
        "recovers",
        "startup",
        "restart_after_cache",
        "concurrent_delivery",
        "metron_corroborates",
        "metron_disagrees",
        "stale_owner",
        "stale_locg_during_read",
    ],
)
async def test_published_watch_uses_normal_add_without_guessing(
    identity_probe_db, tmp_path, monkeypatch, case
):
    monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "true")
    get_settings.cache_clear()
    _, factory, _ = identity_probe_db
    reader = installed_reader(tmp_path)
    monkeypatch.setattr(catalog_reader, "get_catalog_reader", lambda: reader)
    monkeypatch.setattr(sources, "get_catalog_reader", lambda: reader)
    emit = AsyncMock()
    monkeypatch.setattr(EventBus, "emit", emit)
    if case.startswith("metron_"):
        cross_id = "10" if case == "metron_corroborates" else "12"

        class Metron(CatalogAdapter):
            async def series(self, external_id, *, validator=None):
                response = await super().series(external_id, validator=validator)
                response.data.cross_identities = [
                    ExternalIdentityRef(
                        IdentityNamespace.COMICVINE, MetadataEntityKind.SERIES, cross_id
                    )
                ]
                return response

        adapter = Metron(count=1)
        factories = sources.metadata_sources()
        factories[MetadataSource.METRON_API] = registration(
            adapter, capabilities=list(SourceCapability)
        )
        monkeypatch.setattr(sources, "metadata_sources", lambda: factories)
    watch_id, actor = await saved_watch(factory, tmp_path)
    async with factory.begin() as session:
        session.add(
            MetadataSourceConfig(source="comicvine_local", enabled=True, priority=1, revision=1)
        )
        if case.startswith("metron_"):
            session.add(
                MetadataSourceConfig(source="metron_api", enabled=True, priority=2, revision=1)
            )
        existing = case in {
            "prior_rejection",
            "paused_owner",
            "stale_owner",
            "stale_locg_during_read",
        }
        if existing:
            watch = await session.get(SeriesInterest, watch_id)
            local = Series(
                title="Existing",
                sort_title="existing",
                comicvine_id=10,
                monitored=case != "paused_owner",
                preferred_library_root_id=watch.target_library_root_id,
            )
            session.add(local)
            await session.flush()
            local_id = local.id
            if case == "stale_owner":
                session.add(
                    SeriesExternalIdentity(
                        series_id=local_id,
                        identity_namespace="comicvine",
                        external_id="10",
                        verification_state="stale",
                        evidence_kind="user_selection",
                    )
                )
            if case == "prior_rejection":
                session.add(
                    SeriesIdentityEvent(
                        series_id=local.id,
                        identity_namespace="locg",
                        external_id=watch.source_series_id,
                        verification_state="rejected",
                        evidence_kind="user_selection",
                        event_key="a" * 64,
                        request_fingerprint="b" * 64,
                        request_json="{}",
                    )
                )
    original = reader.series
    calls = []

    async def series_read(number):
        calls.append(number)
        if case == "stale_locg_during_read" and len(calls) == 1:
            async with factory.begin() as session:
                w = await session.get(SeriesInterest, watch_id)
                session.add(
                    SeriesExternalIdentity(
                        series_id=local_id,
                        identity_namespace="locg",
                        external_id=w.source_series_id,
                        verification_state="stale",
                        evidence_kind="user_selection",
                    )
                )
        if case == "superseded":
            await run_whats_new_refresh(session_factory=factory, client=ReleaseClient([]))
        if case == "unavailable" or (case == "recovers" and len(calls) == 1):
            raise OSError("catalog temporarily unavailable")
        if case == "cancel_during_read":
            async with factory.begin() as session:
                await cancel_watch(session, watch_id, actor)
        if case == "root_changed":
            async with factory.begin() as session:
                watch = await session.get(SeriesInterest, watch_id)
                root = await session.get(LibraryRoot, watch.target_library_root_id)
                root.enabled = False
        return await original(number)

    monkeypatch.setattr(reader, "series", series_read)
    release = _issue_summary()
    release["store_date"] = date.today().isoformat()
    release["series"].update(
        title="Batman",
        comicvine_series_id=10,
        publication_state="published",
        publication_as_of=datetime.now(UTC).date().isoformat(),
    )
    releases = [release]
    if case == "ambiguous_owner":
        releases.append(
            {
                **release,
                "locg_issue_id": 825,
                "locg_series_id": 999,
                "series": {**release["series"], "locg_series_id": 999},
            }
        )
    if case == "unsupported_claim":
        release["series"]["metron_series_id"] = 999
    if case.startswith("metron_"):
        release["series"]["metron_series_id"] = 42
    try:
        if case in {"restart_after_cache", "concurrent_delivery"}:
            async with factory.begin() as session:
                result = await WhatsNewRefreshService(client=ReleaseClient(releases)).refresh(
                    session
                )
                assert result.watch_adds
            if case == "concurrent_delivery":
                await asyncio.gather(
                    *(finish_watch_adds(factory, result.watch_adds) for _ in range(2))
                )
        for attempt in range(2):
            run = (
                refresh_whats_new_cache_if_needed
                if case in {"startup", "restart_after_cache"}
                else run_whats_new_refresh
            )
            await run(session_factory=factory, client=ReleaseClient(releases))
            if case == "recovers" and attempt == 0:
                async with factory() as session:
                    assert (await session.get(SeriesInterest, watch_id)).state.value == "watching"
        async with factory() as session:
            watch = await session.get(SeriesInterest, watch_id)
            if case in {
                "exact",
                "recovers",
                "startup",
                "restart_after_cache",
                "concurrent_delivery",
                "metron_corroborates",
            }:
                assert watch.state.value == "promoted"
                local = await session.get(Series, watch.resolved_series_id)
                assert local.monitored and local.comicvine_id == 10
                assert Path(local.path).is_dir() and Path(local.path).is_relative_to(
                    tmp_path / "root"
                )
                issue = (await session.scalars(select(Issue))).one()
                assert issue.series_id == local.id and issue.status == IssueStatus.WANTED
                links = (await session.scalars(select(SeriesExternalIdentity))).all()
                expected = {("locg", watch.source_series_id), ("comicvine", "10")}
                if case == "metron_corroborates":
                    expected.add(("metron", "42"))
                assert {
                    (link.identity_namespace.value, link.external_id) for link in links
                } == expected
                assert await session.scalar(select(func.count()).select_from(Series)) == 1
                claim = next(link for link in links if link.identity_namespace.value == "locg")
                origin = json.loads(claim.evidence_locator)
                assert origin["actor"] == "automation" and origin["actor_user_id"] is None
                assert origin["origin"]["record_id"] == watch_id
                assert origin["origin"]["source_identity"]["external_id"] == (
                    "42" if case == "metron_corroborates" else "10"
                )
                emit.assert_awaited_once()
            else:
                assert watch.state.value == {
                    "cancel_during_read": "cancelled",
                    "unavailable": "watching",
                    "superseded": "watching",
                }.get(case, "needs_confirmation")
                assert watch.resolved_series_id is None
                assert await session.scalar(select(func.count()).select_from(Series)) == int(
                    existing
                )
                if existing:
                    assert await session.scalar(select(Series.monitored)) == (
                        case != "paused_owner"
                    )
                    links = (await session.scalars(select(SeriesExternalIdentity))).all()
                    assert [link.verification_state.value for link in links] == (
                        ["stale"] if case.startswith("stale_") else []
                    )
                assert not list((tmp_path / "root").iterdir())
                emit.assert_not_awaited()
    finally:
        get_settings.cache_clear()
