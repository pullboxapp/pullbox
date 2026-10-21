"""Queue cancellation and new intent share a short, durable ownership boundary."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pullbox.models.airdcpp import AirDcppAcquisition
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.providers.airdcpp.contracts import AirDcppQueueBundleAddInfo, AirDcppQueueFile
from pullbox.services.airdcpp_acquisition import AirDcppQueueAcquisitionService
from pullbox.services.airdcpp_recovery import act_on_queue_recovery
from tests.unit.test_airdcpp_acquisition import _candidate
from tests.unit.test_airdcpp_queue_contracts import _queue_file_payload
from tests.unit.test_airdcpp_reconciliation import _FakeApi
from tests.unit.test_airdcpp_recovery import _recovery


async def test_cancel_claim_blocks_same_file_admission_after_owner_probe(
    identity_probe_db, monkeypatch
):
    _, factory, _ = identity_probe_db
    hid, aid, pid = await _recovery(factory)
    api = _FakeApi(
        [[]],
        files=[AirDcppQueueFile.model_validate(_queue_file_payload())],
        retry_result=AirDcppQueueBundleAddInfo(id=91, merged=True),
    )
    errors = []
    async with factory() as session:
        acquisition = await session.get(AirDcppAcquisition, aid)
        history = await session.get(DownloadHistory, hid)
        client_id, issue_id = acquisition.client_config_id, history.issue_id
        client = await session.get(DownloadClientConfig, client_id)
        updated_at = client.updated_at
        await session.commit()
        commit = session.commit
        commits = 0

        async def admit_after_probe():
            nonlocal commits
            await commit()
            commits += 1
            if commits != 2:
                return
            async with factory() as competing:
                try:
                    await AirDcppQueueAcquisitionService().acquire(
                        competing,
                        candidate=_candidate(client_id),
                        issue_id=issue_id,
                        request_key="racing-new-request",
                        search_log_id=None,
                        api_client=api,
                        queue_priority=None,
                        replace_existing_file=False,
                    )
                except ValueError as exc:
                    errors.append(str(exc))

        monkeypatch.setattr(session, "commit", admit_after_probe)
        await act_on_queue_recovery(session, pid, "cancel", api)
        assert errors and "being checked" in errors[0]
        assert not api.retry_calls and not api.create_calls
        assert api.removed_bundles == [91]
        assert (
            await session.scalar(
                select(AirDcppAcquisition.id).where(
                    AirDcppAcquisition.request_key == "racing-new-request"
                )
            )
            is None
        )
        await session.refresh(client)
        assert client.updated_at == updated_at


@pytest.mark.parametrize("other_state", ["mutation_pending", "duplicate", "cancelled"])
async def test_cancel_preserves_existing_same_content_intents(identity_probe_db, other_state):
    _, factory, _ = identity_probe_db
    hid, aid, pid = await _recovery(factory)
    async with factory() as session:
        source = await session.get(AirDcppAcquisition, aid)
        source.bundle_id = 91
        original = await session.get(DownloadHistory, hid)
        other_history = DownloadHistory(
            issue_id=original.issue_id,
            title=original.title,
            download_url="airdcpp://intent/shared-request",
            download_client=DownloadClientType.AIRDCPP,
            state=DownloadState.QUEUED
            if other_state == "mutation_pending"
            else DownloadState.FAILED,
        )
        session.add(other_history)
        await session.flush()
        other = AirDcppAcquisition(
            download_history_id=other_history.id,
            request_key="shared-request",
            client_config_id=source.client_config_id,
            client_identity=source.client_identity,
            tth=source.tth,
            size_bytes=source.size_bytes,
            original_name=source.original_name,
            client_state=other_state,
            route_snapshot={"existing_bundle_owner_acquisition_id": aid},
        )
        session.add(other)
        await session.commit()
        api = _FakeApi([[]], files=[AirDcppQueueFile.model_validate(_queue_file_payload())])
        if other_state == "cancelled":
            await act_on_queue_recovery(session, pid, "cancel", api)
            assert api.removed_bundles == [91]
        else:
            with pytest.raises(ValueError, match="shared"):
                await act_on_queue_recovery(session, pid, "cancel", api)
            assert api.removed_bundles == []
            await session.refresh(source)
            assert source.bundle_id == 91
            assert source.client_state == "recovery_required"
        await session.refresh(other)
        assert other.client_state == other_state


async def test_intent_admitted_before_cancel_keeps_shared_bundle(identity_probe_db):
    _, factory, _ = identity_probe_db
    hid, aid, pid = await _recovery(factory)
    async with factory() as session:
        source = await session.get(AirDcppAcquisition, aid)
        history = await session.get(DownloadHistory, hid)
        candidate, issue_id = _candidate(source.client_config_id), history.issue_id
        await session.commit()

        class CancelDuringAdmission(_FakeApi):
            async def download_search_result(self, *args, **kwargs):
                assert not session.in_transaction()
                async with factory() as cancelling:
                    result = await act_on_queue_recovery(cancelling, pid, "cancel", self)
                    assert "shared" in result.casefold()
                return await super().download_search_result(*args, **kwargs)

        api = CancelDuringAdmission(
            [[]],
            files=[AirDcppQueueFile.model_validate(_queue_file_payload())],
            retry_result=AirDcppQueueBundleAddInfo(id=91, merged=True),
        )
        result = await AirDcppQueueAcquisitionService().acquire(
            session,
            candidate=candidate,
            issue_id=issue_id,
            request_key="admitted-before-cancel",
            search_log_id=None,
            api_client=api,
            queue_priority=None,
            replace_existing_file=False,
        )
        assert result.bundle_id == 91
        assert result.state is DownloadState.SENT
        assert not api.removed_bundles


async def test_cancel_claim_does_not_block_different_content(identity_probe_db):
    from dataclasses import replace

    _, factory, _ = identity_probe_db
    hid, aid, _ = await _recovery(factory)
    async with factory() as session:
        source = await session.get(AirDcppAcquisition, aid)
        source.client_state = "recovery_check_pending"
        history = await session.get(DownloadHistory, hid)
        candidate = _candidate(source.client_config_id)
        candidate = replace(candidate, route=replace(candidate.route, tth="A" * 39))
        await session.commit()
        api = AsyncMock()
        api.download_search_result.return_value = AirDcppQueueBundleAddInfo(id=92, merged=False)
        result = await AirDcppQueueAcquisitionService().acquire(
            session,
            candidate=candidate,
            issue_id=history.issue_id,
            request_key="other-content",
            search_log_id=None,
            api_client=api,
            queue_priority=None,
            replace_existing_file=False,
        )
        assert result.bundle_id == 92
        api.download_search_result.assert_awaited_once()
