"""Direct packs retain their existing batch writer independently of manual imports."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree
from sqlalchemy import select

from pullbox.config import get_settings
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Issue, LibraryFile
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectAcquisitionState
from pullbox.models.download import DownloadHistory
from pullbox.models.issue import IssueStatus
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.services import direct_acquisition_executor, direct_artifact_post_processing
from tests.integration.metadata_identity.test_direct_paired_metadata import direct_artifact, execute

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pullbox.services.issue_import_service import (
        ManualIssueImportResult,
        PreparedManualIssueImport,
    )
    from tests.fixtures.metadata_identity_persistence import IdentityProbeDatabase


@pytest.fixture(params=[(False, False), (False, True), (True, False), (True, True)])
def writer_flags(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import_flag, direct_flag = request.param
    for name, enabled in (
        ("IMPORT", import_flag),
        ("DIRECT", direct_flag),
        ("DOWNLOAD", False),
        ("CONVERSION", False),
    ):
        monkeypatch.setenv(f"PULLBOX_METADATA_PAIRED_{name}_WRITER_ENABLED", str(enabled).lower())
    get_settings.cache_clear()
    monkeypatch.setattr(direct_acquisition_executor, "request_story_arc_sync_now", lambda: None)
    yield
    get_settings.cache_clear()


async def seed_two_member_pack(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[int, int, tuple[int, int], Path]:
    attempt_id, artifact_id, issue_id, source = await direct_artifact(factory, tmp_path)
    async with factory.begin() as session:
        first = await session.get(Issue, issue_id)
        first.issue_number = 5
        first.issue_number_text = "5"
        first.status = IssueStatus.WANTED
        second = Issue(
            series_id=first.series_id,
            issue_number=6,
            issue_number_text="6",
            status=IssueStatus.WANTED,
        )
        session.add(second)
        await session.flush()
        second_id = second.id
        session.add(
            IssueExternalIdentity(
                issue_id=second_id,
                identity_namespace=IdentityNamespace.METRON,
                external_id="43",
                verification_state=IdentityVerificationState.VERIFIED,
                evidence_kind="provider_result",
            )
        )
        attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
        attempt.plan_snapshot = {
            **attempt.plan_snapshot,
            "coverage": {"selected_content_issue_numbers": ["5", "6"]},
        }
    with ZipFile(source, "w") as pack:
        for number in (5, 6):
            member = io.BytesIO()
            with ZipFile(member, "w") as comic:
                comic.writestr("page.jpg", f"page for issue {number}".encode())
                comic.writestr(
                    "ComicInfo.xml",
                    "<ComicInfo><Series>Canonical series</Series>"
                    f"<Number>{number}</Number></ComicInfo>",
                )
            pack.writestr(f"Canonical series #{number}.cbz", member.getvalue())
    return attempt_id, artifact_id, (issue_id, second_id), source


@pytest.mark.asyncio
@pytest.mark.usefixtures("writer_flags")
async def test_direct_pack_completes_without_inheriting_manual_paired_writer(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path
) -> None:
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_ids, source = await seed_two_member_pack(factory, tmp_path)
    result = await asyncio.wait_for(execute(factory, tmp_path, attempt_id, artifact_id), timeout=30)
    assert result.state is DirectAcquisitionState.COMPLETED
    async with factory() as session:
        files = (await session.scalars(select(LibraryFile).order_by(LibraryFile.issue_id))).all()
        history = await session.scalar(select(DownloadHistory))
        assert len(files) == 2
        assert history.imported_at is not None
        assert history.final_path == files[0].file_path
        assert (await session.get(DirectAcquisitionAttempt, attempt_id)).library_file_id == files[
            0
        ].id
        for file, issue_id, number in zip(files, issue_ids, (5, 6), strict=True):
            assert file.issue_id == issue_id
            assert (await session.get(Issue, issue_id)).status is IssueStatus.OWNED
            with ZipFile(file.file_path) as archive:
                assert "MetronInfo.xml" not in archive.namelist()
                assert ElementTree.fromstring(archive.read("ComicInfo.xml")).findtext(
                    "Number"
                ) == str(number)
                assert archive.read("page.jpg") == f"page for issue {number}".encode()
    assert not source.exists(), "Completed pack ownership permits normal quarantine cleanup"


@pytest.mark.asyncio
@pytest.mark.usefixtures("writer_flags")
async def test_direct_pack_later_member_failure_preserves_existing_atomic_cleanup(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_ids, source = await seed_two_member_pack(factory, tmp_path)
    original = source.read_bytes()
    original_execute = direct_artifact_post_processing.execute_manual_issue_import
    completed_paths: list[Path] = []

    async def fail_second_member(
        session: AsyncSession, prepared: PreparedManualIssueImport, **kwargs: Any
    ) -> ManualIssueImportResult:
        if prepared.issue_id == issue_ids[1]:
            assert len(completed_paths) == 1 and completed_paths[0].is_file()
            raise RuntimeError("Injected later-member registration failure")
        imported = await original_execute(session, prepared, **kwargs)
        completed_paths.append(Path(imported.library_file.file_path))
        return imported

    monkeypatch.setattr(
        direct_artifact_post_processing, "execute_manual_issue_import", fail_second_member
    )
    result = await asyncio.wait_for(execute(factory, tmp_path, attempt_id, artifact_id), timeout=30)
    assert result.state is DirectAcquisitionState.INTERVENTION
    assert len(completed_paths) == 1
    assert not completed_paths[0].exists()
    assert source.read_bytes() == original
    assert not list((tmp_path / "comics").rglob("*.cbz"))
    async with factory() as session:
        assert await session.scalar(select(LibraryFile)) is None
        assert (await session.scalar(select(DownloadHistory))).imported_at is None
        for issue_id in issue_ids:
            assert (await session.get(Issue, issue_id)).status is not IssueStatus.OWNED
