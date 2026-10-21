"""Shared download completion retires partial-copy recovery after commit."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from pullbox.models import Issue, LibraryFile
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectAcquisitionState
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import IssueStatus
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.services import direct_metadata_recovery
from tests.integration.metadata_identity.test_direct_metadata_recovery import _partial_case
from tests.integration.metadata_identity.test_direct_paired_metadata import (
    no_global_sync,  # noqa: F401
    paired_download_setting,  # noqa: F401
)
from tests.integration.metadata_identity.test_download_paired_metadata import drain

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("client", [DownloadClientType.QBITTORRENT, DownloadClientType.SABNZBD])
@pytest.mark.parametrize("followup_fails", [False, True], ids=["reconciled", "followup-failed"])
async def test_completed_replacement_reconciles_only_after_durable_import(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: DownloadClientType,
    followup_fails: bool,
) -> None:
    _, factory, _ = identity_probe_db
    pending_id, attempt_id, issue_id, retained_source = await _partial_case(factory, tmp_path)
    retained_bytes = retained_source.read_bytes()
    replacement = tmp_path / "downloads" / "complete.cbz"
    with ZipFile(replacement, "w") as archive:
        archive.writestr("page.jpg", b"complete replacement pages")
    replacement_bytes = replacement.read_bytes()

    async with factory.begin() as session:
        download = DownloadHistory(
            issue_id=issue_id,
            title="Complete replacement",
            download_url="test://complete-replacement",
            download_client=client,
            state=DownloadState.COMPLETED,
            completed_at=datetime.now(UTC),
            downloaded_path=str(replacement),
            replace_existing_file=True,
        )
        session.add(download)
        issue = await session.get(Issue, issue_id)
        assert issue is not None
        issue.status = IssueStatus.DOWNLOADING
        await session.flush()
        download_id = download.id

    real_reconcile = direct_metadata_recovery.reconcile_metadata_replacements
    published: list[tuple[Path, bytes, int]] = []

    async def reconcile_after_commit(session: AsyncSession, target_issue_id: int) -> None:
        assert target_issue_id == issue_id
        assert not session.in_transaction()
        assert not (session.new or session.dirty or session.deleted)
        async with factory() as reader:
            completed = await reader.get(DownloadHistory, download_id)
            assert completed is not None and completed.imported_at is not None
            assert completed.state is DownloadState.COMPLETED
            assert completed.post_processing_claim_token is None
            file = await reader.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
            assert file is not None and file.file_path == completed.final_path
            path = Path(file.file_path)
            published.append((path, path.read_bytes(), path.stat().st_mtime_ns))
        if followup_fails:
            raise RuntimeError("Replacement reconciliation unavailable")
        await real_reconcile(session, target_issue_id)

    monkeypatch.setattr(
        direct_metadata_recovery, "reconcile_metadata_replacements", reconcile_after_commit
    )
    await drain(factory)

    async with factory() as session:
        completed = await session.get(DownloadHistory, download_id)
        assert completed is not None
        assert completed.state is DownloadState.COMPLETED
        assert completed.imported_at is not None
        assert completed.error_message is None
        assert completed.post_processing_claim_token is None
        assert completed.post_processing_claimed_at is None
        issue = await session.get(Issue, issue_id)
        assert issue is not None and issue.status is IssueStatus.OWNED
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        assert file is not None and file.file_path == completed.final_path
        with ZipFile(file.file_path) as archive:
            assert archive.read("page.jpg") == b"complete replacement pages"
            assert {"ComicInfo.xml", "MetronInfo.xml"} <= set(archive.namelist())

        pending = await session.get(PendingMatch, pending_id)
        assert pending is not None
        assert pending.status == (
            PendingMatchStatus.PENDING if followup_fails else PendingMatchStatus.APPROVED
        )
        if not followup_fails:
            assert pending.match_details["resolution_reason"] == "metadata_superseded"
            assert pending.match_details["resolved_library_file_id"] == file.id
        attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
        assert attempt is not None
        assert attempt.state is (
            DirectAcquisitionState.INTERVENTION if followup_fails else DirectAcquisitionState.FAILED
        )
        original_history = await session.scalar(
            select(DownloadHistory).where(DownloadHistory.external_id == f"direct:{attempt_id}")
        )
        assert original_history is not None and original_history.state is DownloadState.FAILED
        assert original_history.imported_at is None

    assert len(published) == 1, "Shared queue never ran replacement reconciliation"
    path, content, modified_at = published[0]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (content, modified_at)
    assert retained_source.read_bytes() == retained_bytes
    assert replacement.read_bytes() == replacement_bytes
    await drain(factory)
    assert len(published) == 1, "An already imported download must not be processed again"
