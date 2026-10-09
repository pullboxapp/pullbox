"""Direct packs reconcile metadata without inheriting the single-import transaction."""

from __future__ import annotations

import asyncio
import io
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree
from sqlalchemy import select

from pullbox.config import get_settings
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.core.metroninfo_schema import validate_metroninfo_xml
from pullbox.models import Issue, LibraryFile, LibraryRoot
from pullbox.models.config import SystemConfig
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
)
from pullbox.models.download import DownloadHistory
from pullbox.models.issue import IssueStatus
from pullbox.models.library import FileFormat
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.services import (
    direct_acquisition_executor,
    direct_artifact_post_processing,
    direct_pack_paired_metadata,
    issue_import_service,
)
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
def writer_flags(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[bool, bool]]:
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
    yield import_flag, direct_flag
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
async def test_direct_pack_writer_obeys_direct_flag_independently_of_import_flag(
    identity_probe_db: IdentityProbeDatabase, tmp_path: Path, writer_flags: tuple[bool, bool]
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
                if writer_flags[1]:
                    payload = archive.read("MetronInfo.xml")
                    validate_metroninfo_xml(payload)
                    metron = ElementTree.fromstring(payload)
                    assert metron.findtext("Number") == str(number)
                    assert metron.findtext("IDS/ID[@source='Metron']") == str(number + 37)
                else:
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


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_flags", [(False, True), (True, True)], indirect=True)
async def test_second_member_metadata_conflict_publishes_nothing_and_preserves_pack(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    writer_flags: tuple[bool, bool],
) -> None:
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_ids, source = await seed_two_member_pack(factory, tmp_path)
    with ZipFile(source) as pack:
        first, second = (pack.read(name) for name in pack.namelist())
    replacement = io.BytesIO()
    with ZipFile(io.BytesIO(second)) as old, ZipFile(replacement, "w") as comic:
        comic.writestr("page.jpg", old.read("page.jpg"))
        comic.writestr(
            "ComicInfo.xml",
            "<ComicInfo><Series>Wrong series</Series><Number>6</Number></ComicInfo>",
        )
    with ZipFile(source, "w") as pack:
        pack.writestr("Canonical series #5.cbz", first)
        pack.writestr("Canonical series #6.cbz", replacement.getvalue())
    original = source.read_bytes()
    result = await asyncio.wait_for(execute(factory, tmp_path, attempt_id, artifact_id), timeout=30)
    assert result.state is DirectAcquisitionState.INTERVENTION
    assert source.read_bytes() == original
    assert not list((tmp_path / "comics").rglob("*.cbz"))
    async with factory() as session:
        assert await session.scalar(select(LibraryFile)) is None
        assert (await session.scalar(select(DownloadHistory))).imported_at is None
        attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
        assert attempt.failure_code == "direct_metadata_review_required"
        assert "metadata" in attempt.error_message.lower()
        for issue_id, status in zip(
            issue_ids, (IssueStatus.DOWNLOADING, IssueStatus.WANTED), strict=True
        ):
            assert (await session.get(Issue, issue_id)).status is status


@pytest.mark.asyncio
async def test_pack_cancel_after_first_registration_rolls_back_both_members(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_flags: tuple[bool, bool],
) -> None:
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_ids, _ = await seed_two_member_pack(factory, tmp_path)
    cancel = asyncio.Event()
    original_execute = direct_artifact_post_processing.execute_manual_issue_import

    async def cancel_after_first(
        session: AsyncSession, prepared: PreparedManualIssueImport, **kwargs: Any
    ) -> ManualIssueImportResult:
        imported = await original_execute(session, prepared, **kwargs)
        if prepared.issue_id == issue_ids[0]:
            cancel.set()
        return imported

    monkeypatch.setattr(
        direct_artifact_post_processing, "execute_manual_issue_import", cancel_after_first
    )
    result = await asyncio.wait_for(
        execute(factory, tmp_path, attempt_id, artifact_id, cancel_event=cancel), timeout=30
    )
    assert result.state is DirectAcquisitionState.CANCELLED
    assert not list((tmp_path / "comics").rglob("*.cbz"))
    async with factory() as session:
        assert await session.scalar(select(LibraryFile)) is None
        assert (await session.scalar(select(DownloadHistory))).imported_at is None
        for issue_id in issue_ids:
            assert (await session.get(Issue, issue_id)).status is IssueStatus.WANTED


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_flags", [(False, True)], indirect=True)
@pytest.mark.parametrize(
    "changed", ["metadata", "policy", "source", "ownership", "root", "selection", "approval"]
)
async def test_pack_revalidates_all_members_before_registration(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_flags: tuple[bool, bool],
    changed: str,
) -> None:
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_ids, source = await seed_two_member_pack(factory, tmp_path)
    original = source.read_bytes()
    stage = direct_pack_paired_metadata.stage_manual_metadata
    calls = 0

    @asynccontextmanager
    async def change_after_staging(*args: Any, **kwargs: Any):
        nonlocal calls
        async with stage(*args, **kwargs) as staged:
            calls += 1
            if calls == 2:
                if changed == "source":
                    with ZipFile(source, "a") as archive:
                        archive.writestr("changed.txt", b"pack changed during preparation")
                else:
                    async with factory.begin() as other:
                        first = await other.get(Issue, issue_ids[0])
                        if changed == "metadata":
                            first.title = "Metadata changed during preparation"
                        elif changed == "ownership":
                            first.status = IssueStatus.SKIPPED
                        elif changed == "policy":
                            policy = await other.get(
                                SystemConfig, "update_embedded_comicinfo_from_match_on_import"
                            )
                            policy.value = "false"
                        elif changed == "selection":
                            artifact = await other.get(DirectArtifactAttempt, artifact_id)
                            artifact.is_selected = False
                        elif changed == "approval":
                            attempt = await other.get(DirectAcquisitionAttempt, attempt_id)
                            attempt.plan_snapshot = {
                                **attempt.plan_snapshot,
                                "safety_review": {"changed": True},
                            }
                        else:
                            root = await other.scalar(select(LibraryRoot))
                            destination = tmp_path / "other-comics"
                            destination.mkdir()
                            root.path = str(destination)
            yield staged

    monkeypatch.setattr(direct_pack_paired_metadata, "stage_manual_metadata", change_after_staging)
    result = await asyncio.wait_for(execute(factory, tmp_path, attempt_id, artifact_id), timeout=30)
    assert result.state is DirectAcquisitionState.INTERVENTION
    assert source.exists()
    if changed != "source":
        assert source.read_bytes() == original
    assert not list((tmp_path / "comics").rglob("*.cbz"))
    assert not list((tmp_path / "other-comics").rglob("*.cbz"))
    async with factory() as session:
        assert await session.scalar(select(LibraryFile)) is None
        assert (await session.scalar(select(DownloadHistory))).imported_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_flags", [(False, True)], indirect=True)
@pytest.mark.parametrize("failure", ["registration", "process-cancel"])
async def test_pack_failure_after_publication_cleans_unregistered_member(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_flags: tuple[bool, bool],
    failure: str,
) -> None:
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_ids, source = await seed_two_member_pack(factory, tmp_path)
    original = source.read_bytes()
    register = issue_import_service.register_library_file

    async def fail_after_publishing(*args: Any, **kwargs: Any):
        result = await register(*args, **kwargs)
        if result.issue_id == issue_ids[1]:
            if failure == "process-cancel":
                raise asyncio.CancelledError
            raise OSError("Injected registration acknowledgement failure")
        return result

    monkeypatch.setattr(issue_import_service, "register_library_file", fail_after_publishing)
    if failure == "process-cancel":
        with pytest.raises(asyncio.CancelledError):
            await execute(factory, tmp_path, attempt_id, artifact_id)
    else:
        assert (
            await execute(factory, tmp_path, attempt_id, artifact_id)
        ).state is DirectAcquisitionState.INTERVENTION
    assert source.read_bytes() == original
    assert not list((tmp_path / "comics").rglob("*.cbz"))
    async with factory() as session:
        assert await session.scalar(select(LibraryFile)) is None
        assert (await session.scalar(select(DownloadHistory))).imported_at is None
        for issue_id in issue_ids:
            assert (await session.get(Issue, issue_id)).status is not IssueStatus.OWNED
    monkeypatch.setattr(issue_import_service, "register_library_file", register)
    assert (
        await execute(factory, tmp_path, attempt_id, artifact_id)
    ).state is DirectAcquisitionState.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_flags", [(False, True)], indirect=True)
async def test_pack_preserves_already_owned_other_member(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    writer_flags: tuple[bool, bool],
) -> None:
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_ids, source = await seed_two_member_pack(factory, tmp_path)
    existing = tmp_path / "comics" / "existing-six.cbz"
    existing.write_bytes(b"existing owned comic must not be rewritten")
    async with factory.begin() as session:
        issue = await session.get(Issue, issue_ids[1])
        issue.status = IssueStatus.OWNED
        file = LibraryFile(
            file_path=str(existing),
            file_name=existing.name,
            file_size=existing.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.fromtimestamp(existing.stat().st_mtime, UTC),
            library_root_id=await session.scalar(select(LibraryRoot.id)),
            issue_id=issue.id,
        )
        session.add(file)
        await session.flush()
        file_id = file.id
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is DirectAcquisitionState.COMPLETED
    assert existing.read_bytes() == b"existing owned comic must not be rewritten"
    async with factory() as session:
        assert len((await session.scalars(select(LibraryFile))).all()) == 2
        assert (await session.get(LibraryFile, file_id)).file_path == str(existing)
    assert not source.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_flags", [(False, True)], indirect=True)
@pytest.mark.parametrize("scenario", ["approved", "changed", "unsafe"])
async def test_pack_size_approval_is_bound_to_pack_and_never_waives_dangerous_content(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    writer_flags: tuple[bool, bool],
    scenario: str,
) -> None:
    from pullbox.core.library_file_ownership import build_file_identity_signature

    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await seed_two_member_pack(factory, tmp_path)
    with ZipFile(source, "w") as pack:
        for number in (5, 6):
            member = io.BytesIO()
            with ZipFile(member, "w") as comic:
                comic.writestr("page.jpg", b"real page bytes" * 100000)
                if scenario == "unsafe" and number == 6:
                    comic.writestr("../escape.jpg", b"not allowed")
            pack.writestr(f"Canonical series #{number}.cbz", member.getvalue())
    async with factory.begin() as session:
        session.add(SystemConfig(key="archive_size_limit_mb", value="1", value_type="int"))
        attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
        attempt.plan_snapshot = {
            **attempt.plan_snapshot,
            "safety_review": {
                "overrideable": True,
                "allowed_once": True,
                "source_signature": build_file_identity_signature(source),
            },
        }
    if scenario == "changed":
        with ZipFile(source, "a") as pack:
            pack.writestr("extra.txt", b"changed after approval")
    original = source.read_bytes()
    result = await execute(factory, tmp_path, attempt_id, artifact_id)
    assert result.state is (
        DirectAcquisitionState.COMPLETED
        if scenario == "approved"
        else DirectAcquisitionState.INTERVENTION
    )
    async with factory() as session:
        assert (
            (await session.scalar(select(DownloadHistory))).imported_at is not None
            if scenario == "approved"
            else (await session.scalar(select(DownloadHistory))).imported_at is None
        )
        assert (await session.get(SystemConfig, "archive_size_limit_mb")).value == "1"
        if scenario != "approved":
            assert await session.scalar(select(LibraryFile)) is None
    if scenario != "approved":
        assert source.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_flags", [(False, True)], indirect=True)
async def test_pack_process_cancellation_waits_for_short_publication_boundary(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_flags: tuple[bool, bool],
) -> None:
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await seed_two_member_pack(factory, tmp_path)
    original = source.read_bytes()
    publish = direct_pack_paired_metadata.publish_file_without_overwrite
    started, release = threading.Event(), threading.Event()

    def delayed_publish(stage: Path, target: Path) -> None:
        started.set()
        assert release.wait(timeout=10)
        publish(stage, target)

    monkeypatch.setattr(
        direct_pack_paired_metadata, "publish_file_without_overwrite", delayed_publish
    )
    task = asyncio.create_task(execute(factory, tmp_path, attempt_id, artifact_id))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0.1)
        assert not task.done(), "Cancellation must drain in-flight publication before cleanup"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert source.read_bytes() == original
    assert not list((tmp_path / "comics").rglob("*.cbz"))
    async with factory() as session:
        assert await session.scalar(select(LibraryFile)) is None
        assert (await session.scalar(select(DownloadHistory))).imported_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_flags", [(True, True)], indirect=True)
async def test_pack_embedding_policy_off_keeps_legacy_output(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    writer_flags: tuple[bool, bool],
) -> None:
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, _ = await seed_two_member_pack(factory, tmp_path)
    async with factory.begin() as session:
        policy = await session.get(SystemConfig, "update_embedded_comicinfo_from_match_on_import")
        policy.value = "false"
    assert (
        await execute(factory, tmp_path, attempt_id, artifact_id)
    ).state is DirectAcquisitionState.COMPLETED
    async with factory() as session:
        for file in await session.scalars(select(LibraryFile)):
            with ZipFile(file.file_path) as comic:
                assert "MetronInfo.xml" not in comic.namelist()


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_flags", [(False, True)], indirect=True)
async def test_pack_reuses_native_conversion_before_batch_registration(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    writer_flags: tuple[bool, bool],
) -> None:
    from tests.unit.test_nonzip_metadata_writing import nonzip_archive

    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, _, source = await seed_two_member_pack(factory, tmp_path)
    with ZipFile(source) as archive:
        first = archive.read(archive.namelist()[0])
    second = tmp_path / "second.cb7"
    nonzip_archive(second, [("page.jpg", b"actual native member")])
    with ZipFile(source, "w") as pack:
        pack.writestr("Canonical series #5.cbz", first)
        pack.writestr("Canonical series #6.cb7", second.read_bytes())
    assert (
        await execute(factory, tmp_path, attempt_id, artifact_id)
    ).state is DirectAcquisitionState.COMPLETED
    async with factory() as session:
        for file in await session.scalars(select(LibraryFile)):
            assert Path(file.file_path).suffix == ".cbz"
            with ZipFile(file.file_path) as comic:
                assert {"ComicInfo.xml", "MetronInfo.xml"} <= set(comic.namelist())


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_flags", [(False, True)], indirect=True)
async def test_pack_later_failure_restores_explicit_existing_replacement(
    identity_probe_db: IdentityProbeDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_flags: tuple[bool, bool],
) -> None:
    _, factory, _ = identity_probe_db
    attempt_id, artifact_id, issue_ids, source = await seed_two_member_pack(factory, tmp_path)
    original_pack = source.read_bytes()
    existing = tmp_path / "comics" / "existing-five.cbz"
    with ZipFile(existing, "w") as archive:
        archive.writestr("page.jpg", b"keep the previous owned comic")
    original = existing.read_bytes()
    async with factory.begin() as session:
        issue = await session.get(Issue, issue_ids[0])
        issue.status = IssueStatus.OWNED
        file = LibraryFile(
            file_path=str(existing),
            file_name=existing.name,
            file_size=existing.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.fromtimestamp(existing.stat().st_mtime, UTC),
            library_root_id=await session.scalar(select(LibraryRoot.id)),
            issue_id=issue.id,
        )
        session.add(file)
        await session.flush()
        file_id = file.id
        attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
        attempt.replace_existing_file = True
    execute_member = direct_artifact_post_processing.execute_manual_issue_import

    async def fail_second(
        session: AsyncSession, prepared: PreparedManualIssueImport, **kwargs: Any
    ):
        if prepared.issue_id == issue_ids[1]:
            raise RuntimeError("Reject the second member after replacing the first")
        return await execute_member(session, prepared, **kwargs)

    monkeypatch.setattr(direct_artifact_post_processing, "execute_manual_issue_import", fail_second)
    assert (
        await execute(factory, tmp_path, attempt_id, artifact_id)
    ).state is DirectAcquisitionState.INTERVENTION
    assert existing.read_bytes() == original
    assert source.read_bytes() == original_pack
    assert list((tmp_path / "comics").rglob("*.cbz")) == [existing]
    async with factory() as session:
        files = (await session.scalars(select(LibraryFile))).all()
        assert len(files) == 1 and files[0].id == file_id and files[0].file_path == str(existing)
        assert (await session.scalar(select(DownloadHistory))).imported_at is None
