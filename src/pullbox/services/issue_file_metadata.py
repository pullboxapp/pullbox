"""Read-only approval and journaled writing of existing managed CBZ metadata."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from pydantic import TypeAdapter
from sqlalchemy import select

from pullbox.core.archive import ArchiveError
from pullbox.core.archive_metadata import (
    MAX_METADATA_BYTES,
    ArchiveMetadataFiles,
    MetadataFile,
    read_archive_metadata,
)
from pullbox.models import LibraryFile
from pullbox.models.archive_metadata_publication import PublicationState
from pullbox.models.library_conversion import LibraryConversion
from pullbox.schemas.issue_file_metadata import (
    FileMetadataChange,
    FileMetadataChoices,
    FileMetadataPreview,
)
from pullbox.services.archive_metadata_binding import (
    ArchiveMetadataBindingError,
    ArchiveMetadataTarget,
    archive_primary_identity,
    assemble_bound_archive_metadata,
    inspect_archive_metadata_target,
    read_archive_metadata_binding,
    require_unowned_metadata_file,
)
from pullbox.services.archive_metadata_finalization import finalize_archive_publication
from pullbox.services.archive_metadata_publication import (
    ArchivePublicationError,
    inspect_archive_publication,
    load_archive_publication,
    prepare_archive_publication,
    publish_archive_publication,
    reconcile_archive_publication,
    record_archive_publication,
)
from pullbox.services.archive_metadata_reconciliation import reconcile_archive_metadata
from pullbox.services.archive_metadata_rendering import (
    ArchiveMetadataRenderError,
    render_archive_metadata,
)
from pullbox.services.issue_file_metadata_review import review_metadata_fields
from pullbox.services.library_mutation_coordination import lock_file_mutation_admission
from pullbox.utilities.executors.archive_metadata_staging import stage_cbz_metadata_interruptible
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.job_queue_cancellation import drain_task

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pullbox.core.metadata_identity import ExternalIdentityRef
    from pullbox.schemas.metadata_snapshot import MetadataSnapshot
    from pullbox.utilities.executors.archive_subprocess import ControlCheck, ProgressCallback


def file_metadata_error(exc: BaseException) -> str:
    code = getattr(exc, "code", "")
    if code == "unresolved_conflicts":
        return "Choose a value for each conflicting field and update the preview before writing."
    if code in {"invalid_choice", "choices_changed"}:
        return "The available metadata choices changed. Preview again and review each field."
    if code == "archive_unreadable":
        return "This comic archive cannot be read. Replace or repair it, then preview again."
    if code == "unreconciled_field":
        field = getattr(exc, "field", "metadata")
        label = (
            field.replace("_", " ") if field in {"title", "issue_count", "credits"} else "metadata"
        )
        return (
            f"File and library metadata disagree about {label}. "
            "The file was left unchanged; review these existing values before writing."
        )
    if code == "reference_only":
        return (
            "Files kept in place are not modified. Use a separate managed copy to write metadata."
        )
    if code in {"root_not_managed", "readonly_source"}:
        return "This library location is read-only. Choose a writable managed library copy."
    if code == "import_rollback_protected":
        return (
            "This file is protected by import rollback history. "
            "Its original bytes were left unchanged."
        )
    if code in {
        "identity_requires_review",
        "parent_identity_missing",
        "unverified_identity",
        "identity_conflict",
    }:
        return "Review and verify the issue and series provider links before writing this file."
    if code == "unsupported_format":
        return "File metadata writing supports managed CBZ, CBR, CB7, and PDF files."
    if code == "trash_not_configured":
        return (
            "Configure a library trash directory in Utilities settings "
            "before converting this comic."
        )
    if code in {"target_missing", "source_unavailable"}:
        return (
            "The registered comic file is unavailable. Restore or reconcile it, then preview again."
        )
    if code in {"publication_busy", "publication_review"}:
        return (
            "A previous file operation needs review. "
            "Check Utilities before writing this file again."
        )
    if code in {
        "approval_changed",
        "binding_changed",
        "source_changed",
        "file_changed",
        "directories_changed",
    }:
        return "The file or metadata changed after preview. Preview again before writing."
    return (
        "The embedded metadata could not be safely reconciled. "
        "Check the file and provider matches, then preview again."
    )


@dataclass(frozen=True)
class PreparedFileMetadata:
    target: ArchiveMetadataTarget
    series: MetadataSnapshot
    issue: MetadataSnapshot
    primary: ExternalIdentityRef | None
    preview: FileMetadataPreview


async def prepare_file_metadata(
    session: AsyncSession, issue_id: int, *, choices: FileMetadataChoices | None = None
) -> PreparedFileMetadata:
    """Release the clean read transaction before archive I/O; never fetch providers."""
    if session.new or session.dirty or session.deleted:
        raise ArchiveMetadataBindingError("pending_session_changes")
    ids = list(
        await session.scalars(
            select(LibraryFile.id).where(LibraryFile.issue_id == issue_id).limit(2)
        )
    )
    if len(ids) != 1:
        raise ArchiveMetadataBindingError("target_missing")
    binding = await read_archive_metadata_binding(
        session, ids[0], expected_issue_id=issue_id, allow_conversion_source=True
    )
    await _require_no_import_owner(session, ids[0])
    from pullbox.core.file_safety import get_archive_size_limit_bytes

    limit = await get_archive_size_limit_bytes(session)
    await session.commit()
    target = await inspect_archive_metadata_target(binding, allow_conversion_source=True)
    try:
        kind = target.path.suffix.lstrip(".").casefold()
        files = (
            ArchiveMetadataFiles(MetadataFile("ComicInfo.xml"), MetadataFile("MetronInfo.xml"))
            if kind == "pdf"
            else await asyncio.to_thread(
                read_archive_metadata,
                target.path,
                "cbz" if kind == "zip" else kind,
                max_solid_scan_bytes=MAX_METADATA_BYTES if kind in {"cbz", "zip"} else limit,
            )
        )
        prepared = await asyncio.to_thread(_prepare, target, files, choices or {})
    except (ArchiveError, BadZipFile) as exc:
        raise ArchiveMetadataBindingError("archive_unreadable") from exc
    await asyncio.to_thread(target.check_unchanged)
    return prepared


async def _require_no_import_owner(session: AsyncSession, file_id: int) -> None:
    await require_unowned_metadata_file(session, file_id)


def _fields(payload: bytes | None) -> dict[str, str]:
    if payload is None:
        return {}
    # Rendering validated the bounded XML before this display-only projection.
    root = ET.fromstring(payload)
    result: dict[str, str] = {}

    def visit(node: ET.Element, path: str) -> None:
        if node.text and node.text.strip():
            result[path] = node.text.strip()
        for key, value in sorted(node.attrib.items()):
            result[path + "/@" + key] = value
        counts: dict[str, int] = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            visit(child, f"{path}/{child.tag}[{counts[child.tag]}]")

    visit(root, root.tag)
    return result


def _prepare(
    target: ArchiveMetadataTarget, files: ArchiveMetadataFiles, choices: FileMetadataChoices
) -> PreparedFileMetadata:
    binding = target.binding
    converts = target.path.suffix.casefold() in {".cbr", ".cb7", ".pdf"}
    archive = reconcile_archive_metadata(files)
    series, issue = assemble_bound_archive_metadata(binding, archive, now=datetime.now(UTC))
    primary = archive_primary_identity(binding, archive)
    series, issue, conflicts = review_metadata_fields(binding, archive, series, issue, choices)
    ready = all(item.selected is not None for item in conflicts)
    evidence = TypeAdapter(ArchiveMetadataTarget).dump_json(target)
    evidence += json.dumps(choices, sort_keys=True, separators=(",", ":")).encode()
    try:
        rendered = render_archive_metadata(
            series,
            issue,
            files,
            primary_identity=primary,
            previous_series=binding.metadata.series.baseline,
            previous_issue=binding.metadata.issues[0].baseline,
        )
    except ArchiveMetadataRenderError as exc:
        # Only descriptive disagreements enter review; identity/schema guards still fail closed.
        if (
            ready
            or exc.code != "unreconciled_field"
            or not any(
                item.key.split(".")[1] == exc.field for item in conflicts if item.selected is None
            )
        ):
            raise
        rendered = None
    if not ready:
        return PreparedFileMetadata(
            target,
            series,
            issue,
            primary,
            FileMetadataPreview(
                file_id=binding.library_file_id,
                file_name=target.path.name,
                changes=[],
                review_key=hashlib.sha256(
                    evidence + (files.comicinfo.payload or b"") + (files.metroninfo.payload or b"")
                ).hexdigest(),
                unchanged=False,
                ready=False,
                conflicts=conflicts,
                converts_to_cbz=converts,
            ),
        )
    assert rendered is not None
    changes = []
    pairs = (
        ("ComicInfo.xml", files.comicinfo.payload, rendered.comicinfo),
        ("MetronInfo.xml", files.metroninfo.payload, rendered.metroninfo),
    )
    for name, previous, output in pairs:
        old, new = _fields(previous), _fields(output)
        for field in sorted(old.keys() | new.keys()):
            if old.get(field) != new.get(field):
                changes.append(
                    FileMetadataChange(
                        document=name,
                        field=field,
                        before=old[field][:500] if field in old else None,
                        after=new[field][:500] if field in new else None,
                    )
                )
        if previous is None:
            changes.insert(
                0,
                FileMetadataChange(
                    document=name, field="Document", before=None, after="Add reconciled metadata"
                ),
            )
    review_key = hashlib.sha256(evidence + rendered.comicinfo + rendered.metroninfo).hexdigest()
    canonical_names = False
    if not converts:
        with ZipFile(target.path) as archive_file:
            canonical_names = {"ComicInfo.xml", "MetronInfo.xml"} <= set(archive_file.namelist())
    unchanged = canonical_names and all(old == new for _, old, new in pairs)
    return PreparedFileMetadata(
        target,
        series,
        issue,
        primary,
        FileMetadataPreview(
            file_id=binding.library_file_id,
            file_name=target.path.name,
            changes=changes[:200],
            review_key=review_key,
            unchanged=unchanged,
            converts_to_cbz=converts,
            conflicts=conflicts,
        ),
    )


async def recover_file_metadata(
    factory: async_sessionmaker[AsyncSession], operation: UUID
) -> PublicationState | None:
    """Classify one owned receipt; never publish or remove files during recovery."""
    async with factory() as session:
        receipt = await load_archive_publication(session, operation)
    if receipt is None:
        return None
    if receipt.plan.import_owner is not None:
        raise ArchivePublicationError("publication_review")
    if receipt.state in {PublicationState.FINALIZED, PublicationState.ABANDONED}:
        return receipt.state
    inspection = await inspect_archive_publication(receipt)
    async with factory.begin() as session:
        receipt = await reconcile_archive_publication(session, receipt, inspection)
    if receipt.state is PublicationState.PUBLISHED:
        inspection = await inspect_archive_publication(receipt)
        async with factory.begin() as session:
            receipt = await finalize_archive_publication(session, receipt, inspection)
    if receipt.state is PublicationState.REVIEW:
        raise ArchivePublicationError("publication_review")
    return receipt.state


async def write_file_metadata(
    factory: async_sessionmaker[AsyncSession],
    issue_id: int,
    review_key: str,
    operation: UUID,
    *,
    limit: int,
    check_control: ControlCheck,
    progress: ProgressCallback,
    job_id: str | None = None,
    choices: FileMetadataChoices | None = None,
) -> str:
    """Approval is checked again before staging and under the publication lock."""
    from uuid import uuid5

    from pullbox.services.native_file_metadata import write_native_file_metadata

    async with factory() as reader:
        native_conversion = await reader.scalar(
            select(LibraryConversion.id).where(
                LibraryConversion.operation_id == str(uuid5(operation, "native-metadata"))
            )
        )
        file = await reader.scalar(select(LibraryFile).where(LibraryFile.issue_id == issue_id))
        native = file is not None and file.file_path.casefold().endswith((".cbr", ".cb7", ".pdf"))
    if native or native_conversion is not None:
        return await write_native_file_metadata(
            factory,
            issue_id,
            review_key,
            operation,
            choices=choices,
            check_control=check_control,
            progress=progress,
        )
    recovered = await recover_file_metadata(factory, operation)
    if recovered is PublicationState.FINALIZED:
        return "recovered"
    if recovered is not None:
        raise ArchivePublicationError("approval_changed")
    await check_control()
    async with factory() as session:
        prepared = await prepare_file_metadata(session, issue_id, choices=choices)
    if not prepared.preview.ready:
        raise ArchivePublicationError("unresolved_conflicts")
    if prepared.preview.review_key != review_key:
        raise ArchivePublicationError("approval_changed")
    if prepared.preview.unchanged:
        return "unchanged"
    target, series, issue = prepared.target, prepared.series, prepared.issue
    try:
        async with stage_cbz_metadata_interruptible(
            target.path,
            target.path.parent,
            series,
            issue,
            max_uncompressed_bytes=limit,
            primary_identity=prepared.primary,
            previous_series=target.binding.metadata.series.baseline,
            previous_issue=target.binding.metadata.issues[0].baseline,
            cancellation_check=check_control,
            progress_callback=progress,
        ) as staged:
            plan = await prepare_archive_publication(target, staged, series, issue)
            if job_id is not None:
                plan = plan.model_copy(update={"metadata_job_id": job_id})
            await check_control()
            async with factory.begin() as session:
                await lock_file_mutation_admission(session)
                await ensure_no_active_import_file_mutation(session)
                await _require_no_import_owner(session, target.binding.library_file_id)
                await record_archive_publication(session, plan, operation)
            await check_control()
            async with factory.begin() as session:
                receipt = await publish_archive_publication(session, operation)
            # Once the replacement is committed, finish accounting even if cancel arrives.
            inspection = await inspect_archive_publication(receipt)
            async with factory.begin() as session:
                await finalize_archive_publication(session, receipt, inspection)
        return "written"
    except BaseException:
        # The owned intent is settled before cancellation releases the job lane.
        task = asyncio.create_task(recover_file_metadata(factory, operation))
        if await drain_task(task) is PublicationState.FINALIZED:
            return "recovered"
        raise
