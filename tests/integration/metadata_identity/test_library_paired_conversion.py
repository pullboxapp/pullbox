"""Library conversion builds a reconciled metadata pair without a second repack."""

import asyncio
import io
import shutil
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from defusedxml import ElementTree
from PIL import Image
from py7zr import SevenZipFile
from sqlalchemy import select, update

from pullbox.config import get_settings
from pullbox.core.exceptions import ValidationError
from pullbox.core.file_safety import (
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
)
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.core.metroninfo import parse_metroninfo
from pullbox.core.metroninfo_schema import validate_metroninfo_xml
from pullbox.models import Issue, LibraryFile, LibraryRoot, Series
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.library import FileFormat, LibraryFileStorageMode
from pullbox.models.library_conversion import LibraryConversion
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.schemas.metadata_sources import MetadataDomain
from pullbox.services import library_conversion_files as files
from pullbox.services import library_conversion_recovery as recovery
from pullbox.services import library_convert_service
from pullbox.services.archive_metadata_binding import (
    assemble_archive_metadata_state,
    read_archive_metadata_binding,
)
from pullbox.services.archive_metadata_writing import write_cbz_metadata
from pullbox.services.library_conversion_files import conversion_metadata_digest
from tests.integration.metadata_identity.test_archive_metadata_binding import seed
from tests.unit.test_nonzip_metadata_writing import nonzip_archive
from tests.unit.test_pdf_metadata_writing import native_pdf, pdf_source


@pytest.fixture
def paired_conversion_setting(monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_CONVERSION_WRITER_ENABLED", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


async def registered_nonzip(factory, tmp_path, source_format=FileFormat.CB7):
    file_id, issue_id, series_id, root_id, zip_path = await seed(factory, tmp_path)
    source = zip_path.with_suffix(f".{source_format.value}")
    with ZipFile(zip_path) as archive:
        members = [(member.filename, archive.read(member)) for member in archive.infolist()]
    nonzip_archive(source, members)
    zip_path.unlink()
    async with factory.begin() as session:
        file = await session.get(LibraryFile, file_id)
        file.file_path = str(source)
        file.file_name = source.name
        file.file_format = source_format
        file.file_size = source.stat().st_size
        file.file_modified_at = datetime.fromtimestamp(source.stat().st_mtime, UTC)
    return source, file_id, issue_id, series_id, root_id


async def registered_cb7(factory, tmp_path):
    return await registered_nonzip(factory, tmp_path)


@pytest.mark.usefixtures("paired_conversion_setting")
@pytest.mark.parametrize("namespace", [IdentityNamespace.METRON, IdentityNamespace.GCD])
@pytest.mark.parametrize(
    "source_format",
    [
        FileFormat.CB7,
        pytest.param(
            FileFormat.CBR,
            marks=pytest.mark.skipif(
                not shutil.which("unrar"),
                reason="Native UnRAR contract; required in Docker runtime qualification",
            ),
        ),
    ],
)
async def test_real_library_conversion_writes_both_documents(
    identity_probe_db, tmp_path, namespace, source_format
):
    _, factory, _ = identity_probe_db
    source, file_id, issue_id, series_id, _ = await registered_nonzip(
        factory, tmp_path, source_format
    )
    async with factory.begin() as session:
        await session.execute(
            update(SeriesExternalIdentity)
            .where(SeriesExternalIdentity.series_id == series_id)
            .values(identity_namespace=namespace)
        )
        await session.execute(
            update(IssueExternalIdentity)
            .where(IssueExternalIdentity.issue_id == issue_id)
            .values(identity_namespace=namespace)
        )
    original = source.read_bytes()
    async with factory() as session:
        result = await library_convert_service.convert_library_file(
            session, source=source, trash_dir=tmp_path / "trash", trash_relative_path=source.name
        )
    assert not source.exists()
    assert Path(result.original_trash_path).read_bytes() == original
    with ZipFile(result.target_path) as archive:
        assert "MetronInfo.xml" in archive.namelist(), "Conversion still writes ComicInfo only"
        ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
        mi_bytes = archive.read("MetronInfo.xml")
        validate_metroninfo_xml(mi_bytes)
        mi = ElementTree.fromstring(mi_bytes)
        assert {
            item.evidence.identity.namespace for item in parse_metroninfo(mi_bytes).identities
        } == {namespace}
        assert ci.findtext("Number") == mi.findtext("Number") == "50-X"
        assert ci.findtext("Summary") == mi.findtext("Summary") == "My local summary"
        assert archive.read("page.jpg") == b"page bytes"
    async with factory() as session:
        file = await session.get(LibraryFile, file_id)
        assert file.file_path == result.target_path
        assert file.file_format is FileFormat.CBZ
        assert file.has_comicinfo
        journal = await session.scalar(select(LibraryConversion))
        assert journal.state == "complete" and not journal.active


@native_pdf
@pytest.mark.usefixtures("paired_conversion_setting")
async def test_real_pdf_library_conversion_preserves_reading_order_and_original(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    file_id, issue_id, _, _, zip_path = await seed(factory, tmp_path)
    source = pdf_source(zip_path.with_suffix(".pdf"))
    zip_path.unlink()
    original = source.read_bytes()
    async with factory.begin() as session:
        file = await session.get(LibraryFile, file_id)
        file.file_path = str(source)
        file.file_name = source.name
        file.file_format = FileFormat.PDF
        file.file_size = source.stat().st_size
        file.file_modified_at = datetime.fromtimestamp(source.stat().st_mtime, UTC)
        issue = await session.get(Issue, issue_id)
        issue.description = "A verified summary"
        issue.page_count = 3
    async with factory() as session:
        result = await library_convert_service.convert_library_file(
            session, source=source, trash_dir=tmp_path / "trash", trash_relative_path=source.name
        )

    assert not source.exists()
    assert Path(result.original_trash_path).read_bytes() == original
    with ZipFile(result.target_path) as archive:
        ci = ElementTree.fromstring(archive.read("ComicInfo.xml"))
        mi_bytes = archive.read("MetronInfo.xml")
        validate_metroninfo_xml(mi_bytes)
        mi = ElementTree.fromstring(mi_bytes)
        assert ci.findtext("Number") == mi.findtext("Number") == "50-X"
        assert ci.findtext("Summary") == mi.findtext("Summary") == "A verified summary"
        assert ci.findtext("PageCount") == mi.findtext("PageCount") == "3"
        pages = [name for name in archive.namelist() if name.endswith(".jpg")]
        assert pages == ["page_0000.jpg", "page_0001.jpg", "page_0002.jpg"]
        for name, channel in zip(pages, (0, 1, 2), strict=True):
            with Image.open(io.BytesIO(archive.read(name))) as page:
                pixel = page.convert("RGB").getpixel((100, 100))
                assert pixel[channel] > max(pixel[(channel + 1) % 3], pixel[(channel + 2) % 3])
    async with factory() as session:
        file = await session.get(LibraryFile, file_id)
        assert file.file_format is FileFormat.CBZ and file.has_comicinfo
        journal = await session.scalar(select(LibraryConversion))
        assert journal.state == "complete" and not journal.active


async def test_disabled_flag_preserves_legacy_conversion(identity_probe_db, tmp_path, monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_PAIRED_CONVERSION_WRITER_ENABLED", "false")
    get_settings.cache_clear()
    try:
        _, factory, _ = identity_probe_db
        source, *_ = await registered_cb7(factory, tmp_path)
        async with factory() as session:
            result = await library_convert_service.convert_library_file(
                session,
                source=source,
                trash_dir=tmp_path / "trash",
                trash_relative_path=source.name,
            )
        with ZipFile(result.target_path) as archive:
            assert "ComicInfo.xml" in archive.namelist()
            assert "MetronInfo.xml" not in archive.namelist()
            assert archive.read("page.jpg") == b"page bytes"
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("paired_conversion_setting")
async def test_paired_preparation_uses_one_worker_and_releases_database_writer(
    identity_probe_db, tmp_path, monkeypatch
):
    from pullbox.services.library_mutation_coordination import lock_file_mutation_admission

    _, factory, _ = identity_probe_db
    source, *_ = await registered_cb7(factory, tmp_path)
    stage = files.stage_cbz_metadata_interruptible
    calls = 0

    @asynccontextmanager
    async def counted_stage(*args, **kwargs):
        nonlocal calls
        calls += 1
        async with factory() as session:
            await asyncio.wait_for(lock_file_mutation_admission(session), 2)
            await session.rollback()
        async with stage(*args, **kwargs) as result:
            yield result

    async def legacy_repack(*args, **kwargs):
        pytest.fail("Paired conversion must not invoke a second legacy repack")

    monkeypatch.setattr(files, "stage_cbz_metadata_interruptible", counted_stage)
    monkeypatch.setattr(files, "convert_file_interruptible", legacy_repack)
    async with factory() as session:
        await library_convert_service.convert_library_file(
            session, source=source, trash_dir=tmp_path / "trash", trash_relative_path=source.name
        )
    assert calls == 1


@pytest.mark.usefixtures("paired_conversion_setting")
@pytest.mark.parametrize("point", ["intent", "backup", "output"])
async def test_paired_restart_reuses_verified_output_without_repacking(
    identity_probe_db, tmp_path, monkeypatch, point
):
    _, factory, _ = identity_probe_db
    source, file_id, *_ = await registered_cb7(factory, tmp_path)
    original = source.read_bytes()
    async with factory() as session:
        binding = await recovery.read_conversion_binding(session, source)
        metadata = await read_archive_metadata_binding(
            session, file_id, allow_conversion_source=True
        )
        limit = await get_archive_size_limit_bytes(session)
        block_dangerous = await is_dangerous_file_blocking_enabled(session)
    operation = uuid4()
    async with files.prepare_conversion(
        source,
        tmp_path / "trash" / source.name,
        binding,
        metadata_state=metadata.metadata,
        max_uncompressed_bytes=limit,
        block_dangerous=block_dangerous,
    ) as plan:
        async with factory() as session:
            await recovery.record_conversion(session, plan, operation)
            await session.commit()
        if point in {"backup", "output"}:
            files.publish_file_without_overwrite(plan.backup_stage, plan.backup.path)
        if point == "output":
            files.publish_file_without_overwrite(plan.output_stage, plan.output.path)

    async def repack(*args, **kwargs):
        pytest.fail("Recovery must use the journaled archive, not repack it")

    monkeypatch.setattr(files, "convert_file_interruptible", repack)
    monkeypatch.setattr(files, "stage_cbz_metadata_interruptible", repack)
    async with factory() as session:
        state = await recovery.recover_conversion(session, operation)
        assert state == ("complete" if point == "output" else "abandoned")
        file = await session.get(LibraryFile, file_id)
        assert file.file_path == str(plan.output.path if point == "output" else source)
    if point == "output":
        assert not source.exists()
        assert plan.backup.path.read_bytes() == original
        with ZipFile(plan.output.path) as archive:
            validate_metroninfo_xml(archive.read("MetronInfo.xml"))
            assert archive.read("page.jpg") == b"page bytes"
    else:
        assert source.read_bytes() == original
    async with factory() as session:
        assert await recovery.recover_conversion(session, operation) == state


@pytest.mark.usefixtures("paired_conversion_setting")
async def test_cancel_paired_preparation_cleans_private_stages(
    identity_probe_db, tmp_path, monkeypatch
):
    _, factory, _ = identity_probe_db
    source, *_ = await registered_cb7(factory, tmp_path)
    original = source.read_bytes()
    entered, exited = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def pending_stage(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
            yield
        finally:
            exited.set()

    monkeypatch.setattr(files, "stage_cbz_metadata_interruptible", pending_stage)
    async with factory() as session:
        task = asyncio.create_task(
            library_convert_service.convert_library_file(
                session,
                source=source,
                trash_dir=tmp_path / "trash",
                trash_relative_path=source.name,
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    assert exited.is_set()
    assert source.read_bytes() == original
    assert not source.with_suffix(".cbz").exists()
    assert not list(tmp_path.rglob(".pullbox-conversion-*"))


@pytest.mark.usefixtures("paired_conversion_setting")
@pytest.mark.parametrize("condition", ["reference", "read_only_root", "conflicted_identity"])
async def test_untrusted_conversion_is_rejected_before_archive_work(
    identity_probe_db, tmp_path, monkeypatch, condition
):
    _, factory, _ = identity_probe_db
    source, file_id, issue_id, _, root_id = await registered_cb7(factory, tmp_path)
    original = source.read_bytes()
    async with factory.begin() as session:
        if condition == "reference":
            await session.execute(
                update(LibraryFile)
                .where(LibraryFile.id == file_id)
                .values(storage_mode=LibraryFileStorageMode.REFERENCED)
            )
        elif condition == "read_only_root":
            await session.execute(
                update(LibraryRoot)
                .where(LibraryRoot.id == root_id)
                .values(allow_managed_writes=False)
            )
        else:
            await session.execute(
                update(IssueExternalIdentity)
                .where(IssueExternalIdentity.issue_id == issue_id)
                .values(verification_state=IdentityVerificationState.CONFLICTED)
            )

    @asynccontextmanager
    async def forbidden(*args, **kwargs):
        pytest.fail("Untrusted conversion must fail before archive preparation")
        yield

    monkeypatch.setattr(library_convert_service, "prepare_conversion", forbidden)
    async with factory() as session:
        with pytest.raises(ValidationError):
            await library_convert_service.convert_library_file(
                session,
                source=source,
                trash_dir=tmp_path / "trash",
                trash_relative_path=source.name,
            )
    assert source.read_bytes() == original
    assert not source.with_suffix(".cbz").exists()


@pytest.mark.usefixtures("paired_conversion_setting")
async def test_conversion_preserves_verified_embedded_primary(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    source, file_id, issue_id, series_id, _ = await registered_cb7(factory, tmp_path)
    async with factory() as session:
        metadata = await read_archive_metadata_binding(
            session, file_id, allow_conversion_source=True
        )
    series, issue = assemble_archive_metadata_state(metadata.metadata, None, now=datetime.now(UTC))
    initial = tmp_path / "initial.cbz"
    write_cbz_metadata(
        source,
        initial,
        series,
        issue,
        max_uncompressed_bytes=20_000_000,
        metadata_state=metadata.metadata,
    )
    source.unlink()
    with ZipFile(initial) as archive, SevenZipFile(source, "w") as output:
        for member in archive.infolist():
            output.writestr(archive.read(member), member.filename)
    initial.unlink()
    async with factory.begin() as session:
        session.add_all(
            [
                SeriesExternalIdentity(
                    series_id=series_id,
                    identity_namespace=IdentityNamespace.COMICVINE,
                    external_id="42",
                    verification_state=IdentityVerificationState.VERIFIED,
                    evidence_kind="provider_result",
                ),
                IssueExternalIdentity(
                    issue_id=issue_id,
                    identity_namespace=IdentityNamespace.COMICVINE,
                    external_id="7",
                    verification_state=IdentityVerificationState.VERIFIED,
                    evidence_kind="provider_result",
                ),
            ]
        )
        await session.execute(update(Series).where(Series.id == series_id).values(comicvine_id=42))
        await session.execute(update(Issue).where(Issue.id == issue_id).values(comicvine_id=7))
        file = await session.get(LibraryFile, file_id)
        file.file_size = source.stat().st_size
        file.file_modified_at = datetime.fromtimestamp(source.stat().st_mtime, UTC)
    async with factory() as session:
        result = await library_convert_service.convert_library_file(
            session, source=source, trash_dir=tmp_path / "trash", trash_relative_path=source.name
        )
    with ZipFile(result.target_path) as archive:
        metadata = parse_metroninfo(archive.read("MetronInfo.xml"))
        assert metadata.primary_source is IdentityNamespace.METRON
        assert {item.evidence.identity.namespace for item in metadata.identities} == {
            IdentityNamespace.COMICVINE,
            IdentityNamespace.METRON,
        }


@pytest.mark.usefixtures("paired_conversion_setting")
@pytest.mark.parametrize("point", ["prepared", "intended"])
async def test_metadata_edit_after_capture_keeps_original_for_review(
    identity_probe_db, tmp_path, monkeypatch, point
):
    _, factory, _ = identity_probe_db
    source, _, issue_id, *_ = await registered_cb7(factory, tmp_path)
    original = source.read_bytes()

    async def edit():
        async with factory.begin() as session:
            await session.execute(
                update(Issue).where(Issue.id == issue_id).values(title="User edit")
            )

    if point == "prepared":
        prepare = library_convert_service.prepare_conversion

        @asynccontextmanager
        async def change_before_intent(*args, **kwargs):
            async with prepare(*args, **kwargs) as plan:
                await edit()
                yield plan

        monkeypatch.setattr(library_convert_service, "prepare_conversion", change_before_intent)
    else:
        publish = library_convert_service.publish_conversion

        async def change_before_publication(*args, **kwargs):
            await edit()
            return await publish(*args, **kwargs)

        monkeypatch.setattr(
            library_convert_service, "publish_conversion", change_before_publication
        )
    async with factory() as session:
        with pytest.raises(ValidationError):
            await library_convert_service.convert_library_file(
                session,
                source=source,
                trash_dir=tmp_path / "trash",
                trash_relative_path=source.name,
            )
    assert source.read_bytes() == original
    assert not source.with_suffix(".cbz").exists()


async def test_metadata_digest_ignores_equivalent_dictionary_order(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    _, file_id, *_ = await registered_cb7(factory, tmp_path)
    async with factory() as session:
        bound = await read_archive_metadata_binding(session, file_id, allow_conversion_source=True)
    from pullbox.core.metadata_identity import IdentityNamespace, MetadataSource
    from pullbox.schemas.metadata_sources import SourcePolicyRead, SourceSettings

    policy = SourcePolicyRead(
        source=MetadataSource.METRON_API,
        identity_namespace=IdentityNamespace.METRON,
        enabled=True,
        priority=3,
        domain_priorities={MetadataDomain.CORE: 1, MetadataDomain.ARTWORK: 2},
        settings=SourceSettings(),
        credential_configured=False,
        revision=0,
    )
    left = replace(bound.metadata, policies=(policy,))
    right = replace(
        left,
        policies=(
            policy.model_copy(
                update={"domain_priorities": dict(reversed(list(policy.domain_priorities.items())))}
            ),
        ),
    )
    assert conversion_metadata_digest(left, 1000, True) == conversion_metadata_digest(
        right, 1000, True
    )


@pytest.mark.usefixtures("paired_conversion_setting")
@pytest.mark.parametrize("point", ["before", "prepared"])
async def test_paired_conversion_does_not_rebase_retained_import_ownership(
    identity_probe_db, tmp_path, monkeypatch, point
):
    _, factory, _ = identity_probe_db
    source, file_id, issue_id, series_id, _ = await registered_cb7(factory, tmp_path)
    original = source.read_bytes()

    async def own():
        async with factory.begin() as session:
            job = ImportJob(
                source_path="retained fixture",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.COMPLETED,
            )
            session.add(job)
            await session.flush()
            series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Canonical series",
                series_id=series_id,
                status=ImportSeriesStatus.IMPORTED,
            )
            session.add(series)
            await session.flush()
            session.add(
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=series.id,
                    file_path=str(source),
                    file_name=source.name,
                    file_format="cb7",
                    file_size=source.stat().st_size,
                    library_file_id=file_id,
                    matched_issue_id=issue_id,
                    status=ImportedFileStatus.IMPORTED,
                )
            )
        async with factory() as session:
            assert (
                await session.scalar(
                    select(ImportedFile.id).where(ImportedFile.library_file_id == file_id)
                )
                is not None
            )

    if point == "before":
        await own()
    else:
        prepare = library_convert_service.prepare_conversion

        @asynccontextmanager
        async def acquired_during_conversion(*args, **kwargs):
            async with prepare(*args, **kwargs) as plan:
                await own()
                yield plan

        monkeypatch.setattr(
            library_convert_service, "prepare_conversion", acquired_during_conversion
        )
    async with factory() as session:
        with pytest.raises(ValidationError, match="rollback journal"):
            await library_convert_service.convert_library_file(
                session,
                source=source,
                trash_dir=tmp_path / "trash",
                trash_relative_path=source.name,
            )
    assert source.read_bytes() == original
    assert not source.with_suffix(".cbz").exists()
