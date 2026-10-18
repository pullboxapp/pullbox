"""Overlapping pack requests retire only after verified library completion."""

import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from pullbox.models import Issue, LibraryFile
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactFailureClass,
    DirectArtifactState,
)
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import IssueStatus
from pullbox.models.operation_progress import OperationProgress
from pullbox.models.pending_match import PendingMatch
from pullbox.services import direct_pack_paired_metadata
from pullbox.services.direct_artifact_quarantine import DirectArtifactValidationError
from pullbox.services.direct_download_history_adapter import sync_direct_download_history
from pullbox.services.direct_pack_completion import reconcile_fulfilled_pack
from pullbox.services.intervention_service import InterventionService
from tests.integration.metadata_identity.test_direct_pack_writer_boundary import (
    seed_two_member_pack,
)
from tests.integration.metadata_identity.test_direct_paired_metadata import (
    execute,
)
from tests.integration.metadata_identity.test_direct_paired_metadata import (
    no_global_sync as no_global_sync,
)
from tests.integration.metadata_identity.test_direct_paired_metadata import (
    paired_download_setting as paired_download_setting,
)
from tests.unit.test_direct_acquisition_executor import _attempt


async def competing_attempt(factory, winner_id, issue_id, source, tmp_path, *, legacy=True):
    async with factory.begin() as session:
        winner = await session.get(DirectAcquisitionAttempt, winner_id)
        attempt = _attempt()
        attempt.request_key = str(uuid4())
        attempt.issue_id = issue_id
        attempt.plan_snapshot = copy.deepcopy(winner.plan_snapshot)
        attempt.state = (
            DirectAcquisitionState.INTERVENTION
            if legacy
            else DirectAcquisitionState.POST_PROCESSING
        )
        artifact = attempt.artifact_attempts[0]
        artifact.state = (
            DirectArtifactState.INTERVENTION if legacy else DirectArtifactState.VALIDATING
        )
        session.add(attempt)
        await session.flush()
        retained = (
            tmp_path
            / "quarantine"
            / f"attempt-{attempt.id}"
            / f"artifact-{artifact.id}"
            / f"artifact-{artifact.id}.cbz"
        )
        retained.parent.mkdir(parents=True)
        retained.write_bytes(source.read_bytes())
        artifact.quarantine_path = str(retained)
        if legacy:
            attempt.failure_code = artifact.failure_code = "direct_metadata_review_required"
            attempt.failure_class = artifact.failure_class = DirectArtifactFailureClass.POST_PROCESS
            attempt.error_message = artifact.error_message = (
                "Pack metadata or ownership changed; review before retrying."
            )
            await sync_direct_download_history(session, attempt, artifact, at=datetime.now(UTC))
            await InterventionService().create_direct_attempt_intervention(session, attempt)
        return attempt.id, artifact.id, retained


async def owned_legacy_case(factory, tmp_path):
    winner, artifact, issues, source = await seed_two_member_pack(factory, tmp_path)
    saved_pack = tmp_path / "saved-pack.cbz"
    saved_pack.write_bytes(source.read_bytes())
    assert (
        await execute(factory, tmp_path, winner, artifact)
    ).state is DirectAcquisitionState.COMPLETED
    loser, _, retained = await competing_attempt(factory, winner, issues[1], saved_pack, tmp_path)
    return loser, issues, retained


@pytest.mark.usefixtures("paired_download_setting")
async def test_legacy_pack_recovery_is_retired_without_rewriting_or_fake_import(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    loser, issues, retained = await owned_legacy_case(factory, tmp_path)
    async with factory() as session:
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issues[1]))
        path = Path(file.file_path)
        before = (path.read_bytes(), path.stat().st_mtime_ns, retained.read_bytes())
        assert await reconcile_fulfilled_pack(session, loser)
        await session.commit()
    assert (path.read_bytes(), path.stat().st_mtime_ns, retained.read_bytes()) == before
    async with factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, loser)
        pending = await session.scalar(
            select(PendingMatch).where(PendingMatch.issue_id == issues[1])
        )
        history = await session.scalar(
            select(DownloadHistory).where(DownloadHistory.external_id == f"direct:{loser}")
        )
        assert attempt.state is DirectAcquisitionState.FAILED
        assert attempt.progress_snapshot["stage"] == "superseded"
        assert pending.status == "approved"
        assert pending.match_details["resolution_reason"] == "pack_fulfilled_by_library"
        assert pending.match_details["resolved_library_file_id"] == file.id
        assert history.state is DownloadState.FAILED
        assert history.imported_at is None, "Do not invent a successful second import"
        assert (await session.get(Issue, issues[1])).status is IssueStatus.OWNED
        rows = (
            await session.scalars(
                select(OperationProgress).where(OperationProgress.operation_key == str(history.id))
            )
        ).all()
        assert rows and all(not row.attention_required for row in rows)
        assert not await reconcile_fulfilled_pack(session, loser), (
            "Repeat reconciliation is a no-op"
        )


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize(
    "invalid",
    ["missing", "changed", "wanted", "replacement", "metadata_conflict", "active", "deselected"],
)
async def test_pack_reconciliation_preserves_unproven_or_unrelated_recovery(
    identity_probe_db, tmp_path, invalid
):
    _, factory, _ = identity_probe_db
    loser, issues, _ = await owned_legacy_case(factory, tmp_path)
    async with factory.begin() as session:
        attempt = await session.get(DirectAcquisitionAttempt, loser)
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issues[1]))
        if invalid == "missing":
            Path(file.file_path).unlink()
        elif invalid == "changed":
            Path(file.file_path).write_bytes(b"changed")
        elif invalid == "wanted":
            (await session.get(Issue, issues[1])).status = IssueStatus.WANTED
        elif invalid == "replacement":
            attempt.replace_existing_file = True
        elif invalid == "metadata_conflict":
            attempt.error_message = "Pack metadata needs review. No pack issues were imported."
        elif invalid == "active":
            attempt.state = DirectAcquisitionState.POST_PROCESSING
        else:
            from pullbox.models.direct_acquisition import DirectArtifactAttempt

            (
                await session.scalar(
                    select(DirectArtifactAttempt).where(
                        DirectArtifactAttempt.acquisition_attempt_id == loser
                    )
                )
            ).is_selected = False
    async with factory() as session:
        assert not await reconcile_fulfilled_pack(session, loser)
        await session.commit()
        pending = await session.scalar(
            select(PendingMatch).where(PendingMatch.issue_id == issues[1])
        )
        assert pending.status == "pending"


@pytest.mark.usefixtures("paired_download_setting")
async def test_pack_finishing_during_other_pack_staging_reconciles_without_metadata_review(
    identity_probe_db, tmp_path, monkeypatch
):
    _, factory, _ = identity_probe_db
    winner, artifact, issues, source = await seed_two_member_pack(factory, tmp_path)
    loser, loser_artifact, retained = await competing_attempt(
        factory, winner, issues[1], source, tmp_path, legacy=False
    )
    stage = direct_pack_paired_metadata.stage_manual_metadata
    calls = 0
    inside_winner = False

    @asynccontextmanager
    async def finish_winner(*args, **kwargs):
        nonlocal calls, inside_winner
        async with stage(*args, **kwargs) as staged:
            if not inside_winner:
                calls += 1
                if calls == 2:
                    inside_winner = True
                    assert (
                        await execute(factory, tmp_path, winner, artifact)
                    ).state is DirectAcquisitionState.COMPLETED
            yield staged

    monkeypatch.setattr(direct_pack_paired_metadata, "stage_manual_metadata", finish_winner)
    result = await execute(factory, tmp_path, loser, loser_artifact)
    assert result.state is DirectAcquisitionState.FAILED, (
        "The competing request must be retired, not sent to metadata review"
    )
    async with factory() as session:
        attempt = await session.get(DirectAcquisitionAttempt, loser)
        assert attempt.progress_snapshot["stage"] == "superseded"
        assert (
            await session.scalar(select(PendingMatch).where(PendingMatch.status == "pending"))
            is None
        )
        assert len((await session.scalars(select(LibraryFile))).all()) == 2
    assert retained.exists(), "Retain evidence rather than deleting downloads during reconciliation"


@pytest.mark.usefixtures("paired_download_setting")
async def test_successful_pack_reconciles_earlier_overlapping_failure(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    winner, artifact, issues, source = await seed_two_member_pack(factory, tmp_path)
    loser, _, retained = await competing_attempt(factory, winner, issues[1], source, tmp_path)
    original = retained.read_bytes()
    assert (
        await execute(factory, tmp_path, winner, artifact)
    ).state is DirectAcquisitionState.COMPLETED
    async with factory() as session:
        pending = await session.scalar(
            select(PendingMatch).where(PendingMatch.issue_id == issues[1])
        )
        assert pending.status == "approved", (
            "Successful pack completion must settle older overlapping ownership failures"
        )
        assert (await session.get(DirectAcquisitionAttempt, loser)).progress_snapshot[
            "stage"
        ] == "superseded"
    assert retained.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
async def test_optional_metadata_enrichment_does_not_require_rewriting_owned_comic(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    loser, issues, _ = await owned_legacy_case(factory, tmp_path)
    async with factory.begin() as session:
        issue = await session.get(Issue, issues[1])
        issue.description = "New descriptive information from a linked catalog"
    async with factory() as session:
        from pullbox.services.issue_file_metadata import prepare_file_metadata

        prepared = await prepare_file_metadata(session, issues[1])
        assert prepared.preview.ready and not prepared.preview.unchanged
        original = prepared.target.path.read_bytes()
        assert await reconcile_fulfilled_pack(session, loser)
        await session.commit()
        assert prepared.target.path.read_bytes() == original


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("missing", [False, True])
async def test_recovery_approval_reconciles_or_preserves_unverified_owned_file(
    identity_probe_db, tmp_path, missing
):
    _, factory, _ = identity_probe_db
    loser, issues, _ = await owned_legacy_case(factory, tmp_path)
    async with factory() as session:
        pending = await session.scalar(
            select(PendingMatch).where(PendingMatch.issue_id == issues[1])
        )
        pending_id = pending.id
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issues[1]))
        if missing:
            Path(file.file_path).unlink()
            with pytest.raises(DirectArtifactValidationError, match="library copy needs review"):
                await InterventionService().approve_match(session, pending_id)
            await session.rollback()
        else:
            result = await InterventionService().approve_match(session, pending_id)
            assert result.id == loser and result.state is DirectAcquisitionState.FAILED
            await session.commit()
    async with factory() as session:
        assert (await session.get(PendingMatch, pending_id)).status == (
            "pending" if missing else "approved"
        )


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("evidence", ["missing", "wrong", "different-number"])
async def test_owned_status_alone_is_not_completion_evidence(identity_probe_db, tmp_path, evidence):
    _, factory, _ = identity_probe_db
    loser, issues, _ = await owned_legacy_case(factory, tmp_path)
    async with factory() as session:
        file = await session.scalar(select(LibraryFile).where(LibraryFile.issue_id == issues[1]))
        path = Path(file.file_path)
        with ZipFile(path, "w") as comic:
            comic.writestr("page.jpg", b"page")
            comic.writestr(
                "ComicInfo.xml",
                "<ComicInfo><Series>Canonical series</Series>"
                f"<Number>{'9' if evidence == 'different-number' else '6'}</Number>"
                + (
                    "<Web>https://comicvine.gamespot.com/issue/4000-99999/</Web>"
                    if evidence == "wrong"
                    else ""
                )
                + "</ComicInfo>",
            )
        original = path.read_bytes()
        assert not await reconcile_fulfilled_pack(session, loser)
        await session.commit()
        assert path.read_bytes() == original
        assert (
            await session.scalar(select(PendingMatch).where(PendingMatch.issue_id == issues[1]))
        ).status == "pending"


@pytest.mark.usefixtures("paired_download_setting")
@pytest.mark.parametrize("change", ["file", "cancel", "selection", "metadata"])
async def test_reconciliation_rechecks_after_file_validation(
    identity_probe_db, tmp_path, monkeypatch, change
):
    from pullbox.models.direct_acquisition import DirectArtifactAttempt
    from pullbox.services import direct_pack_completion

    _, factory, _ = identity_probe_db
    loser, issues, _ = await owned_legacy_case(factory, tmp_path)
    validator = direct_pack_completion.validate_direct_artifact

    async def changed_after_validation(session, path):
        result = await validator(session, path)
        if change == "file":
            path.write_bytes(b"changed after validation")
        else:
            async with factory.begin() as other:
                if change == "cancel":
                    attempt = await other.get(DirectAcquisitionAttempt, loser)
                    attempt.state = DirectAcquisitionState.CANCELLED
                elif change == "metadata":
                    issue = await other.get(Issue, issues[1])
                    issue.description = "Metadata changed during validation"
                else:
                    item = await other.scalar(
                        select(DirectArtifactAttempt).where(
                            DirectArtifactAttempt.acquisition_attempt_id == loser
                        )
                    )
                    item.is_selected = False
        return result

    monkeypatch.setattr(
        direct_pack_completion, "validate_direct_artifact", changed_after_validation
    )
    async with factory() as session:
        assert not await reconcile_fulfilled_pack(session, loser)
        await session.commit()
        assert (
            await session.scalar(select(PendingMatch).where(PendingMatch.issue_id == issues[1]))
        ).status == "pending"
