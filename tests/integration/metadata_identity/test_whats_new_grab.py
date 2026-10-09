"""Discovery cannot search or replace a stale, ambiguous, owned or active issue."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from pullbox.config import get_settings
from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType, DownloadState
from pullbox.models.indexer import IndexerConfig, IndexerType
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.models.whats_new import WhatsNewCacheKind, WhatsNewReleaseCache
from pullbox.schemas.whats_new import WhatsNewIssueSelection
from pullbox.services.whats_new_actions import WhatsNewSelectionError
from pullbox.services.whats_new_grab import (
    cache_fingerprint,
    has_grab_capability,
    validate_issue_selection,
)
from tests.integration.metadata_identity.test_whats_new_issue_state import (
    DAY,
    add_download,
    add_file,
    prepare,
    release,
)
from tests.ui.test_whats_new_ui_routes import _issue_summary


@pytest.fixture(autouse=True)
def enable_actions(monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def prepare_selection(session, tmp_path):
    root, series, issue = await prepare(session, tmp_path)
    session.add(
        SeriesExternalIdentity(
            series_id=series.id,
            identity_namespace=IdentityNamespace.LOCG,
            external_id="210209",
            verification_state=IdentityVerificationState.VERIFIED,
            evidence_kind=IdentityEvidenceKind.USER_SELECTION,
        )
    )
    source = {**_issue_summary(), **release(store_date=DAY.isoformat())}
    source["locg_issue_id"] = 8204875
    payload = {"store_date": DAY.isoformat(), "issues": [source]}
    row = WhatsNewReleaseCache(
        cache_key="current-week:2026-09-30",
        cache_kind=WhatsNewCacheKind.CURRENT_WEEK,
        store_date=DAY,
        payload=payload,
        fetched_at=datetime.now(UTC),
        last_successful_refresh_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    selected = WhatsNewIssueSelection(
        cache_id=row.id,
        release_id=8204875,
        fingerprint=cache_fingerprint(payload),
        issue_id=issue.id,
    )
    return root, series, issue, row, selected


async def test_confirmed_missing_issue_is_read_only_admitted(identity_probe_db, tmp_path):
    engine, factory, _ = identity_probe_db
    async with factory.begin() as session:
        *_, selected = await prepare_selection(session, tmp_path)
        statements = []

        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", observe)
        try:
            await validate_issue_selection(session, selected)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
        assert statements and all(sql.lstrip().startswith("SELECT") for sql in statements)
        assert not session.new and not session.dirty and not session.deleted


async def test_unrelated_invalid_release_does_not_block_selected_issue(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        *_, row, selected = await prepare_selection(session, tmp_path)
        row.payload = {
            **row.payload,
            "issues": [
                *row.payload["issues"],
                release(
                    store_date=DAY.isoformat(),
                    locg_issue_id=999,
                    locg_series_id=88,
                    series={"locg_series_id": 77},
                ),
            ],
        }
        selected.fingerprint = cache_fingerprint(row.payload)
        await session.flush()
        await validate_issue_selection(session, selected)


async def test_normalized_variant_number_cannot_hide_conflicting_identity(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        *_, row, selected = await prepare_selection(session, tmp_path)
        variant = release(
            store_date=DAY.isoformat(),
            locg_issue_id=8204876,
            issue_number="03",
            metron_issue_id=999,
        )
        row.payload = {**row.payload, "issues": [*row.payload["issues"], variant]}
        selected.fingerprint = cache_fingerprint(row.payload)
        await session.flush()
        with pytest.raises(WhatsNewSelectionError):
            await validate_issue_selection(session, selected)


@pytest.mark.parametrize(
    "change",
    ["evicted", "replaced", "owned", "active", "skipped", "foreign", "variant", "disabled"],
)
async def test_unsafe_selection_refuses_before_search_or_acquisition(
    identity_probe_db, tmp_path, monkeypatch, change
):
    from pullbox.models.issue import IssueStatus

    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        root, _series, issue, row, selected = await prepare_selection(session, tmp_path)
        if change == "evicted":
            await session.delete(row)
        elif change == "replaced":
            row.payload = {**row.payload, "issues": []}
        elif change == "owned":
            await add_file(session, root, issue)
        elif change == "active":
            await add_download(session, issue, DownloadState.QUEUED)
        elif change == "skipped":
            issue.status = IssueStatus.SKIPPED
        elif change == "foreign":
            selected = selected.model_copy(update={"issue_id": issue.id + 1})
        elif change == "variant":
            second = release(store_date=DAY.isoformat(), locg_issue_id=8204876, metron_issue_id=999)
            row.payload = {**row.payload, "issues": [row.payload["issues"][0], second]}
            selected = selected.model_copy(update={"fingerprint": cache_fingerprint(row.payload)})
        elif change == "disabled":
            monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "false")
            get_settings.cache_clear()
        await session.flush()
        with pytest.raises(WhatsNewSelectionError):
            await validate_issue_selection(session, selected)


@pytest.mark.parametrize("configured", ["none", "indexer_only", "client_only", "both"])
async def test_grab_requires_search_and_download_configuration(identity_probe_db, configured):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        if configured in {"indexer_only", "both"}:
            session.add(
                IndexerConfig(
                    name="Search",
                    indexer_type=IndexerType.NEWZNAB,
                    url="https://example.test",
                    api_key="test",
                )
            )
        if configured in {"client_only", "both"}:
            session.add(
                DownloadClientConfig(
                    name="Client",
                    client_type=DownloadClientType.SABNZBD,
                    url="https://example.test",
                )
            )
        await session.flush()
        assert await has_grab_capability(session) is (configured == "both")


@pytest.mark.parametrize(
    "state,expected", [("discovered", "missing"), ("planned", "queued"), ("paused", "paused")]
)
async def test_direct_plan_before_history_is_truthful_and_not_grabbable(
    identity_probe_db, tmp_path, state, expected
):
    from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectAcquisitionState
    from pullbox.services.whats_new_issue_state import local_release_issues

    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue, _, selected = await prepare_selection(session, tmp_path)
        session.add(
            DirectAcquisitionAttempt(
                issue_id=issue.id,
                request_key="test-plan",
                provider_identity="test",
                provider_candidate_id="test",
                state=DirectAcquisitionState(state),
            )
        )
        await session.flush()
        actual = (await local_release_issues(session, [release()], {"210209": series}))[0]
        assert actual.state.value == expected, (
            "planned direct acquisition is invisible before history"
        )
        if state == "discovered":
            await validate_issue_selection(session, selected)
        else:
            with pytest.raises(WhatsNewSelectionError):
                await validate_issue_selection(session, selected)
