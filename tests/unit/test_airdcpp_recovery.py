"""Recovery never blindly repeats an ambiguous queue mutation."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.models.airdcpp import AirDcppAcquisition
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.providers.airdcpp.contracts import AirDcppQueueFile
from pullbox.providers.airdcpp.errors import AirDcppResponseError
from pullbox.services.airdcpp_recovery import act_on_queue_recovery, record_queue_recovery
from tests.sqlite_schema import create_test_schema
from tests.unit.test_airdcpp_queue_contracts import _queue_file_payload
from tests.unit.test_airdcpp_reconciliation import _bundle, _FakeApi, _seed


@pytest.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await create_test_schema(connection)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _recovery(factory):
    _, hid, aid = await _seed(factory, state=DownloadState.QUEUED, bundle_id=None)
    async with factory() as session:
        a = await session.get(AirDcppAcquisition, aid)
        await session.refresh(a, ["download_history"])
        await record_queue_recovery(session, a, code="queue_lookup_failed", at=datetime.now(UTC))
        pending = await session.scalar(select(PendingMatch))
        await session.commit()
        return hid, aid, pending.id


@pytest.mark.parametrize("action", ["recheck", "retry"])
async def test_existing_exact_bundle_is_adopted_without_redownload(db_factory, action):
    hid, aid, pid = await _recovery(db_factory)
    api = _FakeApi([[]], files=[AirDcppQueueFile.model_validate(_queue_file_payload())])
    async with db_factory() as session:
        result = await act_on_queue_recovery(session, pid, action, api)
        assert result == "Existing AirDC++ download reconnected."
        assert (await session.get(AirDcppAcquisition, aid)).bundle_id == 91
        assert (await session.get(AirDcppAcquisition, aid)).next_retry_at is None
        assert (await session.get(DownloadHistory, hid)).state is DownloadState.SENT
        assert (await session.get(DownloadHistory, hid)).completed_at is None
        assert (await session.get(PendingMatch, pid)).status == PendingMatchStatus.APPROVED
    assert not api.create_calls
    assert not api.retry_calls


@pytest.mark.parametrize("action", ["recheck", "retry", "cancel"])
async def test_known_failed_bundle_has_safe_actions_without_bypassing_hash_check(
    db_factory, action
):
    hid, aid, pid = await _recovery(db_factory)
    bundle = _bundle(status_id="download_error", failed=True)
    bundle = bundle.model_copy(
        update={"status": bundle.status.model_copy(update={"str": "TTH inconsistency"})}
    )
    api = _FakeApi([[bundle]], files=[AirDcppQueueFile.model_validate(_queue_file_payload())])
    async with db_factory() as session:
        a = await session.get(AirDcppAcquisition, aid)
        a.bundle_id = 91
        await session.commit()
        if action == "cancel":
            await act_on_queue_recovery(session, pid, action, api)
            assert (await session.get(PendingMatch, pid)).status == PendingMatchStatus.REJECTED
            assert api.removed_bundles == [91]
        else:
            with pytest.raises(ValueError, match="TTH inconsistency"):
                await act_on_queue_recovery(session, pid, action, api)
            assert (await session.get(PendingMatch, pid)).match_details[
                "failure_code"
            ] == "queue_hash_mismatch"
            assert not api.removed_bundles
        assert (await session.get(DownloadHistory, hid)).state is DownloadState.FAILED
    assert not api.create_calls and not api.retry_calls


async def test_known_bundle_recheck_resumes_only_after_client_error_is_fixed(db_factory):
    hid, aid, pid = await _recovery(db_factory)
    api = _FakeApi([[_bundle()]], files=[AirDcppQueueFile.model_validate(_queue_file_payload())])
    async with db_factory() as session:
        a = await session.get(AirDcppAcquisition, aid)
        a.bundle_id = 91
        await session.commit()
        await act_on_queue_recovery(session, pid, "recheck", api)
        assert (await session.get(DownloadHistory, hid)).state in {
            DownloadState.SENT,
            DownloadState.DOWNLOADING,
        }
        assert (await session.get(PendingMatch, pid)).status == PendingMatchStatus.APPROVED
    assert not api.create_calls and not api.removed_bundles


async def test_known_bundle_cannot_be_recreated_from_a_missing_file_index_alone(db_factory):
    _, aid, pid = await _recovery(db_factory)
    api = _FakeApi([[_bundle()]])
    async with db_factory() as session:
        a = await session.get(AirDcppAcquisition, aid)
        a.bundle_id = 91
        await session.commit()
        with pytest.raises(ValueError, match="conflicting queue identities"):
            await act_on_queue_recovery(session, pid, "retry", api)
    assert not api.create_calls and not api.removed_bundles


async def test_restart_does_not_mark_a_known_hash_failure_resolved(db_factory):
    from pullbox.services.airdcpp_reconciliation import AirDcppReconciler

    hid, aid, pid = await _recovery(db_factory)
    async with db_factory() as session:
        a = await session.get(AirDcppAcquisition, aid)
        a.bundle_id = 91
        a.client_state = "recovery_check_pending"
        a.next_retry_at = datetime.now(UTC) - timedelta(minutes=1)
        (await session.get(DownloadHistory, hid)).state = DownloadState.RETRY_PENDING
        cid = a.client_config_id
        await session.commit()
    api = _FakeApi([[_bundle(status_id="download_error", failed=True)]])
    await AirDcppReconciler(db_factory).reconcile_client(cid, api)
    async with db_factory() as session:
        assert (await session.get(PendingMatch, pid)).status == PendingMatchStatus.PENDING
        assert (await session.get(DownloadHistory, hid)).state is DownloadState.FAILED


async def test_recheck_proves_absence_without_creating_download(db_factory):
    hid, _aid, pid = await _recovery(db_factory)
    api = _FakeApi([[]])
    async with db_factory() as session:
        result = await act_on_queue_recovery(session, pid, "recheck", api)
        assert (
            result
            == "No matching AirDC++ queue item was found. You can retry or cancel this request."
        )
        assert (await session.get(DownloadHistory, hid)).state is DownloadState.FAILED
        assert (await session.get(PendingMatch, pid)).status == PendingMatchStatus.PENDING
    assert not api.create_calls


async def test_retry_rechecks_absence_and_creates_only_once(db_factory):
    _hid, aid, pid = await _recovery(db_factory)
    api = _FakeApi([[]])
    async with db_factory() as session:
        result = await act_on_queue_recovery(session, pid, "retry", api)
        assert result == "AirDC++ request queued."
        with pytest.raises(ValueError):
            await act_on_queue_recovery(session, pid, "retry", api)
        assert (await session.get(AirDcppAcquisition, aid)).bundle_id == 93
    assert len(api.create_calls) == 1


async def test_failed_lookup_is_not_evidence_to_retry_or_cancel(db_factory):
    _hid, _aid, pid = await _recovery(db_factory)
    api = _FakeApi([[]], lookup_failure=AirDcppResponseError("Bad response"))
    async with db_factory() as session:
        for action in ("retry", "cancel"):
            with pytest.raises(ValueError, match="could not verify"):
                await act_on_queue_recovery(session, pid, action, api)
        assert (await session.get(PendingMatch, pid)).status == PendingMatchStatus.PENDING
    assert not api.create_calls and not api.removed_bundles


async def test_cancel_removes_only_verified_remote_bundle_and_retains_history(db_factory):
    hid, _aid, pid = await _recovery(db_factory)
    api = _FakeApi([[]], files=[AirDcppQueueFile.model_validate(_queue_file_payload())])
    async with db_factory() as session:
        result = await act_on_queue_recovery(session, pid, "cancel", api)
        assert result == "AirDC++ request cancelled. No library files were deleted."
        assert (await session.get(DownloadHistory, hid)).error_message == "Cancelled by user"
        assert (await session.get(PendingMatch, pid)).status == PendingMatchStatus.REJECTED
    assert api.removed_bundles == [91]


async def test_ambiguous_queue_identity_does_not_create_or_remove_files(db_factory):
    _, _, pid = await _recovery(db_factory)
    payload = _queue_file_payload()
    payload["target"] = "/Downloads/A different target.cbz"
    api = _FakeApi([[]], files=[AirDcppQueueFile.model_validate(payload)])
    async with db_factory() as session:
        with pytest.raises(ValueError, match="conflicting queue identities"):
            await act_on_queue_recovery(session, pid, "retry", api)
    assert not api.create_calls and not api.removed_bundles


async def test_owned_issue_is_not_downloaded_again(db_factory):
    hid, _, pid = await _recovery(db_factory)
    api = _FakeApi([[]])
    async with db_factory() as session:
        history = await session.get(DownloadHistory, hid)
        issue = await session.get(Issue, history.issue_id)
        issue.status = IssueStatus.OWNED
        await session.commit()
        with pytest.raises(ValueError, match="already owned"):
            await act_on_queue_recovery(session, pid, "retry", api)
        assert (await session.get(PendingMatch, pid)).status == PendingMatchStatus.PENDING
        assert (await session.get(Issue, issue.id)).status is IssueStatus.OWNED
    assert not api.create_calls


async def test_cancel_absent_request_preserves_history_and_issue(db_factory):
    hid, _, pid = await _recovery(db_factory)
    api = _FakeApi([[]])
    async with db_factory() as session:
        await act_on_queue_recovery(session, pid, "cancel", api)
        history = await session.get(DownloadHistory, hid)
        assert history.error_message == "Cancelled by user"
        assert (await session.get(Issue, history.issue_id)).status is IssueStatus.WANTED
    assert not api.create_calls and not api.removed_bundles


async def test_concurrent_second_action_cannot_create_another_request(db_factory):
    _, _, pid = await _recovery(db_factory)

    class RacingApi(_FakeApi):
        async def get_queue_files_by_tth(self, tth):
            async with db_factory() as other:
                with pytest.raises(ValueError, match="has changed"):
                    await act_on_queue_recovery(other, pid, "retry", self)
            return []

    api = RacingApi([[]])
    async with db_factory() as session:
        await act_on_queue_recovery(session, pid, "retry", api)
    assert len(api.create_calls) == 1


async def test_superseded_during_lookup_does_not_create_download(db_factory):
    hid, aid, pid = await _recovery(db_factory)

    class CancelDuringLookup(_FakeApi):
        async def get_queue_files_by_tth(self, tth):
            async with db_factory() as other:
                a = await other.get(AirDcppAcquisition, aid)
                h = await other.get(DownloadHistory, hid)
                a.client_state = "cancelled"
                h.state = DownloadState.FAILED
                h.error_message = "Cancelled by user"
                await other.commit()
            return []

    api = CancelDuringLookup([[]])
    async with db_factory() as session:
        with pytest.raises(ValueError, match="changed"):
            await act_on_queue_recovery(session, pid, "retry", api)
    assert not api.create_calls


async def test_dismissal_survives_same_recovery_but_not_new_failure(db_factory):
    from pullbox.services.download_operation_progress import project_download_operation_progress
    from pullbox.services.operation_activity import acknowledge_operation, list_operation_activity

    hid, _, _ = await _recovery(db_factory)
    async with db_factory() as session:
        history = await session.get(DownloadHistory, hid)
        await project_download_operation_progress(session, history)
        activity = await list_operation_activity(session)
        await acknowledge_operation(session, activity.operations[0].id)
        await project_download_operation_progress(session, history)
        assert (await list_operation_activity(session)).total_count == 0
        history.error_message = "A genuinely different failure"
        await project_download_operation_progress(session, history)
        assert (await list_operation_activity(session)).attention_count == 1


async def test_unresolved_queue_recovery_does_not_expire_with_old_search_matches(db_factory):
    from pullbox.services.intervention_service import InterventionService

    _, _, pid = await _recovery(db_factory)
    async with db_factory() as session:
        pending = await session.get(PendingMatch, pid)
        pending.created_at = datetime.now(UTC) - timedelta(days=40)
        await session.flush()
        assert await InterventionService().expire_stale(session, 30) == 0
        assert pending.status == PendingMatchStatus.PENDING


async def test_restart_recovers_expired_manual_claim_without_redownload(db_factory):
    from pullbox.services.airdcpp_reconciliation import AirDcppReconciler

    hid, aid, pid = await _recovery(db_factory)
    async with db_factory() as session:
        a = await session.get(AirDcppAcquisition, aid)
        h = await session.get(DownloadHistory, hid)
        a.client_state = "recovery_check_pending"
        a.next_retry_at = datetime.now(UTC) - timedelta(minutes=1)
        h.state = DownloadState.RETRY_PENDING
        await session.commit()
        client_id = a.client_config_id
    api = _FakeApi([[]], files=[AirDcppQueueFile.model_validate(_queue_file_payload())])
    await AirDcppReconciler(db_factory).reconcile_client(client_id, api)
    async with db_factory() as session:
        assert (await session.get(AirDcppAcquisition, aid)).bundle_id == 91
        assert (await session.get(PendingMatch, pid)).status == PendingMatchStatus.APPROVED
    assert not api.create_calls
