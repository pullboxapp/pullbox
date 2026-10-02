"""Real adapter, registry, snapshot and saver through authenticated commands."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.core.provider_cooldown import ProviderCooldown
from pullbox.models import Base, Issue, Series, StoryArc, StoryArcExternalIdentity
from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryRoot
from pullbox.models.metadata_identity import IssueIdentityEvent
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.story_arc import IssueStoryArc
from pullbox.providers.metadata import sources
from tests.api.test_metadata_sources_api import csrf, policy
from tests.unit.test_metron_source import envelope, issue_row, series_row

pytest_plugins = ["conftest_security"]
BASE = "/api/v1/metadata/story-arcs/catalog"


@pytest.fixture
async def sec_db(tmp_path_factory):
    # Cache/account sessions must not invalidate the request's shared StaticPool connection.
    path = tmp_path_factory.mktemp("arc-catalog-db") / "database.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def arc_command_setup(authenticated_client, sec_db, monkeypatch, tmp_path):
    saved = await authenticated_client.put(
        "/api/v1/metadata/sources/metron_api",
        json=policy(credential="synthetic-arc-command-token"),
        headers=csrf(authenticated_client),
    )
    assert saved.status_code == 200
    root_path = tmp_path / "comics"
    root_path.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="Managed", path=str(root_path), allow_managed_writes=True)
        session.add_all([root, SystemConfig(key="search_on_add_default", value="true")])
        await session.flush()
        root_id = root.id
    fixture = {
        "selection": {"source": "metron_api", "external_id": "4", "source_revision": 1},
        "root_id": root_id,
        "root": root_path,
        "calls": [],
        "clients": [],
        "issues": [issue_row(100, "13a"), issue_row(101, "50-x")],
        "failure": None,
        "on_request": None,
        "searches": [],
        "arc_rows": [{"id": 4, "name": "Native arc"}],
    }

    async def handle(request):
        fixture["calls"].append(request)
        if fixture["on_request"] is not None:
            await fixture["on_request"]()
        if fixture["failure"] == request.url.path:
            return httpx.Response(429, headers={"Retry-After": "60"})
        headers = (
            {"Last-Modified": "Mon, 28 Sep 2026 10:00:00 GMT"} if fixture.get("conditional") else {}
        )
        if headers and request.headers.get("If-Modified-Since") == headers["Last-Modified"]:
            return httpx.Response(304)
        if request.url.path == "/api/arc/":
            page = int(request.url.params["page"])
            rows = fixture["arc_rows"]
            return httpx.Response(
                200,
                json=envelope(
                    rows[(page - 1) * 100 : page * 100],
                    count=len(rows),
                    next_url=str(request.url.copy_set_param("page", str(page + 1)))
                    if page * 100 < len(rows)
                    else None,
                ),
            )
        if request.url.path == "/api/arc/4/":
            return httpx.Response(200, json={"id": 4, "name": "Native arc"}, headers=headers)
        if request.url.path == "/api/arc/4/issue_list/":
            page = int(request.url.params["page"])
            rows = fixture["issues"]
            return httpx.Response(
                200,
                headers=headers,
                json=envelope(
                    rows[(page - 1) * 100 : page * 100],
                    count=len(rows),
                    next_url=f"https://metron.cloud/api/arc/4/issue_list/?page={page + 1}"
                    if page * 100 < len(rows)
                    else None,
                ),
            )
        assert request.url.path == "/api/series/8/", "Must not fetch whole parent catalogs"
        return httpx.Response(200, json={**series_row(8), "name": "Parent eight"}, headers=headers)

    adapter = sources.MetronSource

    def build(credential):
        result = adapter(
            credential,
            transport=httpx.MockTransport(handle),
            cooldown=ProviderCooldown(),
            minimum_interval=0,
        )
        fixture["clients"].append(result)
        return result

    monkeypatch.setattr(sources, "MetronSource", build)
    monkeypatch.setattr(
        "pullbox.tasks.story_arc_search_task.schedule_story_arc_search",
        fixture["searches"].append,
    )
    return fixture


async def preview(client, fixture):
    result = await client.post(BASE + "/preview", json=fixture["selection"], headers=csrf(client))
    assert result.status_code == 200, result.text
    return result.json()


async def test_arc_catalog_sessions_survive_peer_connection_invalidation(sec_db):
    async with sec_db() as reader, sec_db() as peer:
        assert await reader.scalar(select(func.count()).select_from(StoryArc)) == 0
        connection = await peer.connection()
        await connection.invalidate()
        await peer.rollback()
        assert await reader.scalar(select(func.count()).select_from(StoryArc)) == 0


async def test_arc_preview_recovers_one_local_admission_stall(
    authenticated_client, arc_command_setup, sec_db, monkeypatch
):
    from pullbox.services import metadata_account_admission

    admission = metadata_account_admission.source_account_admission
    attempts = 0

    def guarded(session, *, gcd_api_enabled):
        gate = admission(session, gcd_api_enabled=gcd_api_enabled)
        begin = gate.factory.begin

        @asynccontextmanager
        async def stalled_once():
            nonlocal attempts
            attempts += 1
            async with begin() as account_session:
                if attempts == 1:
                    await asyncio.sleep(3)
                yield account_session

        monkeypatch.setattr(gate.factory, "begin", stalled_once)
        return gate

    monkeypatch.setattr(metadata_account_admission, "source_account_admission", guarded)
    snapshot = await preview(authenticated_client, arc_command_setup)
    assert snapshot["arc"]["title"] == "Native arc"
    assert attempts == 4 and len(arc_command_setup["calls"]) == 3
    assert not arc_command_setup["searches"] and not list(arc_command_setup["root"].iterdir())
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(StoryArc)) == 0


async def test_catalog_add_revalidates_saved_response_through_real_adapter(
    authenticated_client, arc_command_setup
):
    fixture = arc_command_setup
    fixture["conditional"] = True
    snapshot = await preview(authenticated_client, fixture)
    assert len(fixture["calls"]) == 3
    result = await authenticated_client.post(
        BASE, json=decision(fixture, snapshot), headers=csrf(authenticated_client)
    )
    assert result.status_code == 201, result.text
    assert len(fixture["calls"]) == 6
    assert all(
        call.headers.get("If-Modified-Since") == "Mon, 28 Sep 2026 10:00:00 GMT"
        for call in fixture["calls"][3:]
    )


def decision(fixture, snapshot):
    return {
        **fixture["selection"],
        "fingerprint": snapshot["fingerprint"],
        "file_defaults_fingerprint": snapshot["file_defaults_fingerprint"],
        "library_root_id": fixture["root_id"],
        "ordered_issue_ids": ["101", "100"],
        "skipped_issue_ids": ["100"],
        "monitored": True,
    }


async def test_complete_native_arc_review_add_and_duplicate_keep_one_graph(
    authenticated_client, arc_command_setup, sec_db
):
    fixture = arc_command_setup
    snapshot = await preview(authenticated_client, fixture)
    assert snapshot["source"] == "metron_api" and snapshot["source_revision"] == 1
    assert snapshot["arc"]["title"] == "Native arc"
    assert [row["issue_number_text"] for row in snapshot["issues"]] == ["13a", "50-x"]
    assert snapshot["series"][0]["cross_identities"]
    assert snapshot["order_basis"] == "response_order"
    assert snapshot["file_summary"] == "No separate folder"
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(StoryArc)) == 0
    body = decision(fixture, snapshot)
    result = await authenticated_client.post(BASE, json=body, headers=csrf(authenticated_client))
    assert result.status_code == 201, result.text
    arc_id = result.json()["id"]
    assert result.json()["membership_count"] == 2 and result.json()["comicvine_id"] is None
    assert fixture["searches"] == [arc_id]
    async with sec_db() as session:
        arc = await session.get(StoryArc, arc_id)
        assert arc.diagnostics["provider_catalog"]["snapshot"]["source_evidence"]
        assert (await session.scalar(select(StoryArcExternalIdentity))).source == "metron"
        rows = list(
            await session.scalars(select(IssueStoryArc).order_by(IssueStoryArc.sequence_number))
        )
        assert [(r.source_issue_id, r.resolution_state.value) for r in rows] == [
            ("101", "resolved"),
            ("100", "skipped"),
        ]
    again = await authenticated_client.post(BASE, json=body, headers=csrf(authenticated_client))
    assert again.status_code == 409
    assert fixture["searches"] == [arc_id] and not list(fixture["root"].iterdir())
    assert {r.url.path for r in fixture["calls"]} == {
        "/api/arc/4/",
        "/api/arc/4/issue_list/",
        "/api/series/8/",
    }
    assert all(client.client.is_closed for client in fixture["clients"])
    assert "synthetic-arc-command-token" not in result.text


@pytest.mark.parametrize(
    "change", ["order", "skip", "fingerprint", "defaults", "source", "foreign"]
)
async def test_changed_or_unsafe_decision_never_partially_adds(
    authenticated_client, arc_command_setup, sec_db, change
):
    fixture = arc_command_setup
    body = decision(fixture, await preview(authenticated_client, fixture))
    if change == "order":
        body["ordered_issue_ids"] = ["100", "100"]
    elif change == "skip":
        body["skipped_issue_ids"] = ["999"]
    elif change == "fingerprint":
        fixture["issues"][0]["title"] = "Changed since preview"
    elif change == "defaults":
        async with sec_db.begin() as session:
            session.add(SystemConfig(key="story_arc_files_reading_order_width", value="4"))
    elif change == "foreign":
        async with sec_db.begin() as session:
            session.add(Series(title="Already here", sort_title="Already here", comicvine_id=9008))
    else:
        async with sec_db.begin() as session:
            await session.execute(update(MetadataSourceConfig).values(revision=2))
    fixture["calls"].clear()
    result = await authenticated_client.post(BASE, json=body, headers=csrf(authenticated_client))
    assert result.status_code == 409, result.text
    assert not fixture["searches"] and not list(fixture["root"].iterdir())
    if change == "source":
        assert not fixture["calls"]
    async with sec_db() as session:
        for model in (StoryArc, Issue, IssueIdentityEvent):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("operation", ["preview", "add"])
async def test_source_failure_is_safe_and_retryable(
    authenticated_client, arc_command_setup, operation
):
    fixture = arc_command_setup
    body = (
        fixture["selection"]
        if operation == "preview"
        else decision(fixture, await preview(authenticated_client, fixture))
    )
    fixture["failure"] = "/api/series/8/"
    result = await authenticated_client.post(
        BASE + ("/preview" if operation == "preview" else ""),
        json=body,
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409 and result.headers["Retry-After"] == "60"
    assert result.json()["detail"]["source_status"] == "rate_limited"
    assert "synthetic-arc-command-token" not in result.text
    assert not fixture["searches"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "injected"),
        ("issues", []),
        ("source_revision", True),
        ("source", "locg"),
        ("external_id", "../4"),
        ("library_root_id", True),
    ],
)
async def test_command_rejects_forged_metadata(
    authenticated_client, arc_command_setup, field, value
):
    fixture = arc_command_setup
    body = decision(fixture, {"fingerprint": "a" * 64, "file_defaults_fingerprint": "b" * 64})
    body[field] = value
    result = await authenticated_client.post(BASE, json=body, headers=csrf(authenticated_client))
    assert result.status_code == 422 and not fixture["calls"]


async def test_catalog_commands_require_auth_and_csrf(
    authenticated_client, unauthenticated_client, arc_command_setup
):
    fixture = arc_command_setup
    body = decision(fixture, {"fingerprint": "a" * 64, "file_defaults_fingerprint": "b" * 64})
    for path, payload in ((BASE, body), (BASE + "/preview", fixture["selection"])):
        assert (await unauthenticated_client.post(path, json=payload)).status_code in {401, 403}
        assert (await authenticated_client.post(path, json=payload)).status_code == 403
    assert not fixture["calls"]


async def test_native_refresh_preserves_decisions_and_reports_membership_delta(
    authenticated_client, arc_command_setup, sec_db
):
    fixture = arc_command_setup
    body = decision(fixture, await preview(authenticated_client, fixture))
    added = await authenticated_client.post(BASE, json=body, headers=csrf(authenticated_client))
    arc_id = added.json()["id"]
    fixture["issues"] = [issue_row(101, "50-x"), issue_row(102, "50-o")]
    reviewed = await authenticated_client.post(
        f"{BASE}/{arc_id}/preview", json=fixture["selection"], headers=csrf(authenticated_client)
    )
    assert reviewed.status_code == 200, reviewed.text
    snapshot = reviewed.json()
    assert snapshot["changes"] == {
        "revision": added.json()["revision"],
        "added_issue_ids": ["102"],
        "removed_issue_ids": ["100"],
    }
    assert snapshot["file_defaults_fingerprint"] is None
    refresh = {
        **fixture["selection"],
        "fingerprint": snapshot["fingerprint"],
        "expected_revision": snapshot["changes"]["revision"],
    }
    result = await authenticated_client.post(
        f"{BASE}/{arc_id}", json=refresh, headers=csrf(authenticated_client)
    )
    assert result.status_code == 200 and result.json()["membership_count"] == 3
    assert result.json()["revision"] > added.json()["revision"]
    async with sec_db() as session:
        rows = list(
            await session.scalars(select(IssueStoryArc).order_by(IssueStoryArc.sequence_number))
        )
        assert [row.source_issue_id for row in rows] == ["101", "100", "102"]
        assert rows[1].resolution_state.value == "skipped"
        assert rows[2].evidence["catalog_review_required"] and not rows[2].sync_eligible
    replay = await authenticated_client.post(
        f"{BASE}/{arc_id}", json=refresh, headers=csrf(authenticated_client)
    )
    assert replay.status_code == 409
    assert fixture["searches"] == [arc_id, arc_id]


@pytest.mark.parametrize("operation", ["preview", "add"])
async def test_policy_change_during_real_http_has_no_open_transaction_or_writes(
    authenticated_client, arc_command_setup, sec_db, monkeypatch, operation
):
    from pullbox.services import metadata_arc_commands as commands

    fixture = arc_command_setup
    body = (
        fixture["selection"]
        if operation == "preview"
        else decision(fixture, await preview(authenticated_client, fixture))
    )
    held = []
    original = commands.load_source_runtime

    async def load(session, **kwargs):
        held.append(session)
        return await original(session, **kwargs)

    async def change_policy():
        assert held and not held[-1].in_transaction()
        async with sec_db.begin() as session:
            await session.execute(update(MetadataSourceConfig).values(revision=2))

    monkeypatch.setattr(commands, "load_source_runtime", load)
    fixture["on_request"] = change_policy
    result = await authenticated_client.post(
        BASE + ("/preview" if operation == "preview" else ""),
        json=body,
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409 and result.json()["detail"]["code"] == "source_changed"
    assert not fixture["searches"]
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(StoryArc)) == 0


async def test_response_failure_rolls_back_before_scheduling(
    authenticated_client, arc_command_setup, sec_db, monkeypatch
):
    from fastapi import HTTPException

    from pullbox.api.v1 import metadata_arc_catalog as routes

    fixture = arc_command_setup
    body = decision(fixture, await preview(authenticated_client, fixture))

    async def fail_response(session, arc_id):
        assert await session.get(StoryArc, arc_id) is not None
        raise HTTPException(500, "Synthetic serialization failure")

    monkeypatch.setattr(routes, "_load_arc_response", fail_response)
    result = await authenticated_client.post(BASE, json=body, headers=csrf(authenticated_client))
    assert result.status_code == 500 and not fixture["searches"]
    async with sec_db() as session:
        for model in (StoryArc, Issue, Series, IssueIdentityEvent):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("source", ["gcd_api_v2", "comicvine_local", "gcd_local"])
async def test_nonexecutable_arc_sources_never_call_a_provider(
    authenticated_client, arc_command_setup, source
):
    result = await authenticated_client.post(
        BASE + "/preview",
        json={"source": source, "external_id": "4", "source_revision": 1},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409 and not arc_command_setup["calls"]


async def test_refresh_commands_require_auth_and_csrf(authenticated_client, unauthenticated_client):
    selection = {"source": "metron_api", "external_id": "4", "source_revision": 1}
    for path, payload in (
        (f"{BASE}/1/preview", selection),
        (f"{BASE}/1", {**selection, "expected_revision": 1, "fingerprint": "a" * 64}),
    ):
        assert (await unauthenticated_client.post(path, json=payload)).status_code in {401, 403}
        assert (await authenticated_client.post(path, json=payload)).status_code == 403


async def test_real_paginated_arc_is_complete_before_adoption(
    authenticated_client, arc_command_setup
):
    fixture = arc_command_setup
    fixture["issues"] = [issue_row(1000 + i, str(i + 1)) for i in range(103)]
    snapshot = await preview(authenticated_client, fixture)
    assert len(snapshot["issues"]) == 103
    body = decision(fixture, snapshot)
    body["ordered_issue_ids"] = [row["external_id"] for row in reversed(snapshot["issues"])]
    body["skipped_issue_ids"] = []
    result = await authenticated_client.post(BASE, json=body, headers=csrf(authenticated_client))
    assert result.status_code == 201 and result.json()["membership_count"] == 103
    assert [
        r.url.params["page"] for r in fixture["calls"] if r.url.path.endswith("issue_list/")
    ] == ["1", "2", "1", "2"]
    assert all(client.client.is_closed for client in fixture["clients"])


async def test_initial_file_work_starts_after_commit_and_excludes_skipped_members(
    authenticated_client, arc_command_setup, sec_db, monkeypatch
):
    from datetime import UTC, datetime

    from pullbox.core.issue_numbers import parse_issue_number_text
    from pullbox.models.library import FileFormat, LibraryFile
    from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity

    fixture = arc_command_setup
    destination = fixture["root"] / "arcs"
    destination.mkdir()
    async with sec_db.begin() as session:
        parent = Series(title="Owned parent", sort_title="Owned parent", comicvine_id=9008)
        session.add(parent)
        await session.flush()
        session.add(
            SeriesExternalIdentity(
                series_id=parent.id,
                identity_namespace="metron",
                external_id="8",
                verification_state="verified",
                evidence_kind="provider_result",
            )
        )
        for number, identifier in (("13a", "100"), ("50-x", "101")):
            issue = Issue(
                series_id=parent.id,
                issue_number_text=number,
                issue_number=parse_issue_number_text(number)[0],
                status="owned",
            )
            session.add(issue)
            await session.flush()
            session.add(
                IssueExternalIdentity(
                    issue_id=issue.id,
                    identity_namespace="metron",
                    external_id=identifier,
                    verification_state="verified",
                    evidence_kind="provider_result",
                )
            )
            path = fixture["root"] / f"{identifier}.cbz"
            path.write_bytes(b"original stays untouched")
            session.add(
                LibraryFile(
                    issue_id=issue.id,
                    library_root_id=fixture["root_id"],
                    file_path=str(path),
                    file_name=path.name,
                    file_size=path.stat().st_size,
                    file_modified_at=datetime.now(UTC),
                    file_format=FileFormat.CBZ,
                )
            )
        for key, value in {
            "enabled": "true",
            "library_root_id": str(fixture["root_id"]),
            "destination": str(destination),
            "synchronize": "false",
        }.items():
            session.add(SystemConfig(key="story_arc_files_" + key, value=value))
    executed = []

    async def run(arc_id, *, session_factory):
        assert session_factory is sec_db
        async with session_factory() as session:
            arc = await session.get(StoryArc, arc_id)
            assert arc is not None and arc.sync_enabled is False
            marker = arc.diagnostics["catalog_initial_placements"]
            assert marker["pending"] == marker["total"] == 1
            membership = await session.get(IssueStoryArc, marker["items"][0]["membership_id"])
            assert membership.source_issue_id == "101"
        executed.append(arc_id)

    monkeypatch.setattr(
        "pullbox.services.story_arc_catalog_placement.run_catalog_initial_placements", run
    )
    body = decision(fixture, await preview(authenticated_client, fixture))
    result = await authenticated_client.post(BASE, json=body, headers=csrf(authenticated_client))
    assert result.status_code == 201, result.text
    assert executed == [result.json()["id"]] and not list(destination.iterdir())
    assert (fixture["root"] / "100.cbz").read_bytes() == b"original stays untouched"


async def test_registry_command_keeps_comicvine_compatibility_columns(
    authenticated_client, arc_command_setup, sec_db, monkeypatch
):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from tests.unit.test_story_arc_catalog import _issue, _provider, _series

    saved = await authenticated_client.put(
        "/api/v1/metadata/sources/comicvine_api",
        json=policy(credential="synthetic-cv-arc-command-token"),
        headers=csrf(authenticated_client),
    )
    assert saved.status_code == 200, saved.text
    provider = _provider([replace(_issue(number="50"), issue_number_text="50-x")])
    provider.get_series.side_effect = lambda identifier, **kwargs: _series(identifier)
    provider.close = AsyncMock()
    monkeypatch.setattr(sources, "ComicVineProvider", lambda *args, **kwargs: provider)
    fixture = arc_command_setup
    fixture["selection"] = {
        "source": "comicvine_api",
        "external_id": "31",
        "source_revision": saved.json()["revision"],
    }
    body = decision(fixture, await preview(authenticated_client, fixture))
    body["ordered_issue_ids"], body["skipped_issue_ids"] = ["11"], []
    result = await authenticated_client.post(BASE, json=body, headers=csrf(authenticated_client))
    assert result.status_code == 201, result.text
    assert result.json()["comicvine_id"] == 31
    async with sec_db() as session:
        assert (await session.scalar(select(Series))).comicvine_id == 21
        issue = await session.scalar(select(Issue))
        assert issue.comicvine_id == 11 and issue.issue_number_text == "50-X"
    assert provider.close.await_count == 6 and not fixture["calls"]
