"""Read-only approval and journaled writing of existing managed CBZ metadata."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
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
from pullbox.core.comicinfo_text import SINGLE_DOCUMENT
from pullbox.models import LibraryFile
from pullbox.models.archive_metadata_publication import PublicationState
from pullbox.models.library_conversion import LibraryConversion
from pullbox.schemas.issue_file_metadata import (
    FileMetadataChange,
    FileMetadataChoices,
    FileMetadataConflict,
    FileMetadataPreview,
    SharedXmlChoices,
)
from pullbox.schemas.issue_metadata_editor import EDITOR_XML_FIELDS
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
from pullbox.services.archive_metadata_preservation import (
    SHARED_FIELD_LABELS,
    review_preserved_fields,
    shared_xml_choices,
)
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
    from pullbox.schemas.issue_metadata_editor import (
        IssueMetadataEditorField,
        IssueMetadataEditorReview,
    )
    from pullbox.schemas.metadata_snapshot import MetadataSnapshot
    from pullbox.services.archive_metadata_reconciliation import ArchiveMetadataReconciliation
    from pullbox.services.archive_metadata_rendering import RenderedArchiveMetadata
    from pullbox.services.metadata_series_refresh_state import SeriesRefreshState
    from pullbox.utilities.executors.archive_subprocess import ControlCheck, ProgressCallback


def repairable_metadata_field_error(exc: ArchiveMetadataRenderError) -> bool:
    return exc.code in {
        "invalid_price",
        "rating_override_changed",
        "label_override_changed",
        "publisher_required",
    } or (exc.code == "unsupported_content" and exc.field in {"StoryArc", "StoryArcNumber"})


def file_metadata_error(exc: BaseException) -> str:
    from pullbox.services.issue_metadata_editor import IssueMetadataEditError

    if isinstance(exc, IssueMetadataEditError):
        return str(exc)
    code = getattr(exc, "code", "")
    if code == "invalid_price":
        return (
            "Open Edit issue fields and select Cover prices. Enter a two-letter country code "
            "and a decimal amount for each price, or choose Clear value. No file was changed."
        )
    if code == "comicinfo_text_identity_change":
        return (
            "This edit would change a comic identifier spanning neighboring ComicInfo.xml "
            "fields. Leave the affected field unchanged or correct the source, then preview again."
        )
    if code == "unsafe_note_edit":
        return (
            "Notes cannot be safely written with this file's protected identity evidence. "
            "Shorten the note or correct the source, then preview again. No file was changed."
        )
    if code in {"rating_override_changed", "label_override_changed"}:
        field = getattr(exc, "field", "")
        label = {
            "MainCharacterOrTeam": "main character or team",
            "AlternateSeries": "alternate series",
            "AlternateNumber": "number in alternate series",
            "AlternateCount": "total in alternate series",
            "AlternativeNumber": "alternative issue number",
            "CollectionTitle": "collection title",
            "Reprints": "reprints",
            "Stories": "story titles",
            "Prices": "cover prices",
            "Universes": "universes",
            "MangaVolume": "manga volume",
            "SeriesGroup": "series groups",
            "BlackAndWhite": "black and white",
            "Manga": "manga",
            "Review": "review",
            "ScanInformation": "scan information",
            "StoryArc": "embedded story arcs",
        }.get(field, SHARED_FIELD_LABELS.get(field, "metadata"))
        return (
            f"The file no longer matches your saved {label}. Open Edit metadata on "
            f"the issue, select {label.capitalize()}, and preview the value to keep. "
            "No file was changed."
        )
    if code == "ambiguous_list_id":
        if getattr(exc, "field", "") == "Universes":
            return (
                "Multiple Metron universes have the same name and designation. Pullbox cannot "
                "tell which ID to keep. Omit that pair from your correction or correct its IDs "
                "in the source, then preview again. No file was changed."
            )
        label = getattr(exc, "field", "entries").lower()
        return (
            f"Multiple Metron {label} share a name. Pullbox cannot tell which identifier to keep. "
            "Remove that name from your proposed list, or correct its IDs in the source, "
            "then preview again. No file was changed."
        )
    if code == "propagation_pair_required":
        return (
            "This file needs a reconciled XML pair first. Open its issue page, "
            "use Write file metadata to review both documents, then preview this series again."
        )
    if code == "propagation_issue_override":
        return (
            "An issue-level manual correction protects a selected field. Review it in "
            "Edit metadata on the issue page; this file was left unchanged."
        )
    if code == "propagation_volume_count_unset":
        return (
            "The comic has a total volume count but the library does not. Open Edit metadata "
            "on the series and set Total volumes or choose Clear value, then preview again. "
            "To keep the comic's count, leave Total volumes unchecked."
        )
    if code == "propagation_titles_unset":
        return (
            "The comic has alternate publication titles but the library does not. In Edit metadata "
            "on the series, enter the titles or choose Clear value, then preview again. "
            "Leave Alternate publication titles unchecked to keep the comic's values."
        )
    if code == "propagation_titles_identity":
        return (
            "This change would remove an embedded title identifier. Keep its exact title and "
            "language in the series editor, or leave this comic unselected. No file was changed."
        )
    if code == "propagation_titles_legacy_xml":
        return (
            "Writing these titles would also require updating unrelated MetronInfo.xml fields. "
            "Review the XML pair with Write file metadata on the issue page, or leave this comic "
            "unselected. No file was changed."
        )
    if code in {"propagation_titles_ambiguous", "propagation_titles_annotated"}:
        return (
            "The comic contains repeated titles in the same language or annotated title entries. "
            "Pullbox cannot replace them safely. Leave this comic unselected or correct its "
            "embedded list before previewing again."
        )
    if code == "editor_conversion_required":
        return (
            "This editor updates managed CBZ files without conversion. Convert this file to CBZ "
            "using the existing conversion workflow, then open Edit metadata again."
        )
    if code == "partial_issue":
        return (
            "The embedded issue number identifies a story-only extract, not a complete issue. "
            "The file was left unchanged. Open the issue page and search for a complete copy "
            "or use Import File with a complete copy and review the Replace File confirmation. "
            "Changing the metadata cannot restore missing stories."
        )
    if code == "output_disagreement":
        label = {
            "Summary": "summary",
            "description": "summary",
            "Number": "issue number",
            "issue_number_text": "issue number",
            "title": "title",
            "credits": "credits",
        }.get(getattr(exc, "field", ""), "metadata")
        return (
            f"Pullbox could not validate the {label} in its generated XML pair. "
            "The comic was left unchanged; no new download is needed. "
            "Please report this validation error before retrying."
        )
    if code == "snapshot_disagrees":
        return (
            "Pullbox's final verification did not match the approved metadata preview. "
            "The comic was left unchanged; there is no need to download it again. "
            "Please report this error before retrying."
        )
    if code == "unresolved_conflicts":
        return "Choose a value for each conflicting field and update the preview before writing."
    if code in {"invalid_choice", "choices_changed"}:
        return "The available metadata choices changed. Preview again and review each field."
    if code == "archive_unreadable":
        return "This comic archive cannot be read. Replace or repair it, then preview again."
    if code == "publisher_required":
        return (
            "An imprint needs a series publisher in MetronInfo.xml. Set or verify the publisher "
            "in the series editor, or choose Clear value for Imprint in the issue editor. "
            "Then update the preview. The file was left unchanged."
        )
    if getattr(exc, "field", "") == "credits":
        if code == "resource_metadata_change":
            return (
                "Updating creator credits would discard existing creator or role identifiers "
                "or unsupported XML content. The file was left unchanged. Keep the existing "
                "MetronInfo.xml credits or correct their resource metadata, then preview again."
            )
        if code == "unsupported_content":
            return (
                "The selected creator credits cannot be written safely to both ComicInfo.xml "
                "and MetronInfo.xml. Review the creator names and roles, then preview again. "
                "The file was left unchanged."
            )
    if code == "unreconciled_field":
        field = getattr(exc, "field", "metadata")
        if field in SHARED_FIELD_LABELS:
            return (
                f"ComicInfo.xml and MetronInfo.xml disagree about {SHARED_FIELD_LABELS[field]}. "
                "The file was left unchanged. Choose which embedded values to keep "
                "and update the preview."
            )
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
    if code == "import_operation_active":
        return (
            "An unfinished import or rollback still controls this file. "
            "Finish or cancel that operation, then retry. "
            "Completed import history does not block edits."
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
    if code == "invalid_database_metadata":
        return (
            "The selected metadata cannot be stored in the library. "
            "Choose a supported series classification or status, then preview again."
        )
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
    approved_resource_limit: int | None = None
    shared_choices: SharedXmlChoices | None = None
    effective_editor: IssueMetadataEditorReview | None = None


async def prepare_file_metadata(
    session: AsyncSession,
    issue_id: int,
    *,
    choices: FileMetadataChoices | None = None,
    approved_resource_limit: int | None = None,
    editor: IssueMetadataEditorReview | None = None,
    propagation_fields: frozenset[str] | None = None,
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
    if (
        editor is not None or propagation_fields is not None
    ) and not binding.file_path.casefold().endswith((".cbz", ".zip")):
        raise ArchiveMetadataBindingError("editor_conversion_required")
    await _require_no_import_owner(session, ids[0])
    from pullbox.core.file_safety import get_archive_size_limit_bytes

    limit = await get_archive_size_limit_bytes(session)
    from pullbox.services.direct_metadata_approval import read_direct_metadata_approval

    direct_approval = await read_direct_metadata_approval(session, ids[0])
    if approved_resource_limit is not None:
        limit = max(limit, approved_resource_limit)
    await session.commit()
    target = await inspect_archive_metadata_target(binding, allow_conversion_source=True)
    if direct_approval is not None:
        approved_budget = await direct_approval.budget(target.path)
        if approved_budget is not None:
            approved_resource_limit = max(approved_resource_limit or 0, approved_budget)
            limit = max(limit, approved_resource_limit)
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
        if propagation_fields is not None:
            from pullbox.services.series_metadata_propagation_preview import prepare_projection

            if choices or editor is not None:
                raise ArchiveMetadataRenderError("invalid_choice")
            prepared = await asyncio.to_thread(
                prepare_projection, target, files, propagation_fields
            )
        else:
            prepared = await asyncio.to_thread(_prepare, target, files, choices or {}, editor)
        prepared = replace(prepared, approved_resource_limit=approved_resource_limit)
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


def review_metadata_pair(
    metadata: SeriesRefreshState,
    files: ArchiveMetadataFiles,
    archive: ArchiveMetadataReconciliation,
    series: MetadataSnapshot,
    issue: MetadataSnapshot,
    primary: ExternalIdentityRef | None,
    choices: FileMetadataChoices,
    editor: IssueMetadataEditorReview | None,
) -> tuple[
    MetadataSnapshot,
    MetadataSnapshot,
    list[IssueMetadataEditorField],
    list[FileMetadataConflict],
    SharedXmlChoices,
    RenderedArchiveMetadata | None,
    IssueMetadataEditorReview | None,
]:
    """Review descriptive fields without requiring ownership of the source archive."""
    editor_fields: list[IssueMetadataEditorField] = []
    if editor is not None:
        from pullbox.services.issue_metadata_editor import prepare_issue_editor

        if any(not key.startswith("series.") for key in choices):
            raise ArchiveMetadataRenderError("invalid_choice")
        series, issue, editor_fields, editor = prepare_issue_editor(
            metadata, archive, series, issue, files, editor
        )
        reviewed_series, _, conflicts = review_metadata_fields(
            metadata, archive, series, issue, choices
        )
        if choices:
            series = reviewed_series
        chosen_values = {
            f"issue.{field}" for field, edit in editor.edits.items() if edit.source != "reset"
        }
        conflicts = [conflict for conflict in conflicts if conflict.key not in chosen_values]
    else:
        series, issue, conflicts = review_metadata_fields(metadata, archive, series, issue, choices)
    shared_choices = shared_xml_choices(choices)
    reviewed_xml_fields = editor.reviewed_xml_fields if editor else frozenset()
    shared_conflicts = review_preserved_fields(files, shared_choices, editor=editor is not None)
    for editor_field, key in EDITOR_XML_FIELDS.items() if editor else ():
        if editor_field in reviewed_xml_fields:
            shared_conflicts = [c for c in shared_conflicts if c.key != key]
            continue
        row = next(f for f in editor_fields if f.key == f"issue.{editor_field}")
        if (
            row.reset_only
            and editor is not None
            and editor_field in editor.edits
            and editor.edits[editor_field].source == "reset"
        ):
            continue  # No XML selection is needed to relinquish this override.
        embedded = {o.source: o.value for o in row.options if o.source != "library"}
        missing_publisher = (
            editor_field == "imprint" and not series.values.publisher and any(embedded.values())
        )
        incompatible = (
            editor_field != "community_rating"
            and editor_field not in SINGLE_DOCUMENT
            and any(not o.selectable for o in row.options)
            and (embedded.get("ComicInfo.xml") != embedded.get("MetronInfo.xml"))
        )
        if ((row.overridden and row.differs) or incompatible or missing_publisher) and not any(
            c.key == key for c in shared_conflicts
        ):
            destination = SINGLE_DOCUMENT.get(editor_field, "both documents")
            shared_conflicts.append(
                FileMetadataConflict(
                    key=key,
                    label=row.label,
                    options=[],
                    note=row.reason
                    if missing_publisher or not row.editable
                    else f"Select {row.label} to choose the value to write to {destination}.",
                )
            )
    conflicts.extend(shared_conflicts)
    ready = all(item.selected is not None for item in conflicts)
    try:
        rendered = render_archive_metadata(
            series,
            issue,
            files,
            primary_identity=primary,
            previous_series=metadata.series.baseline,
            previous_issue=metadata.issues[0].baseline,
            shared_choices=shared_choices,
            reviewed_xml_fields=reviewed_xml_fields,
        )
    except ArchiveMetadataRenderError as exc:
        # Only descriptive disagreements enter review; identity/schema guards still fail closed.
        if (
            editor is not None
            and exc.code == "invalid_price"
            and "prices" not in reviewed_xml_fields
        ):
            if not any(c.key == "xml.Prices" for c in conflicts):
                conflicts.append(
                    FileMetadataConflict(
                        key="xml.Prices",
                        label="Cover prices",
                        options=[],
                        note=file_metadata_error(exc),
                    )
                )
            ready = False
        arc_issue = (
            editor is not None
            and "story_arcs" not in reviewed_xml_fields
            and exc.field in {"StoryArc", "StoryArcNumber"}
            and exc.code == "unsupported_content"
        )
        if (
            ready
            or (
                exc.code != "unreconciled_field"
                and not arc_issue
                and not (
                    editor
                    and (
                        exc.code
                        in {"rating_override_changed", "label_override_changed", "invalid_price"}
                        or (
                            exc.code == "publisher_required"
                            and "imprint" not in reviewed_xml_fields
                        )
                    )
                )
            )
            or not any(
                item.key.split(".")[1] == ("StoryArc" if arc_issue else exc.field)
                for item in conflicts
                if item.selected is None
            )
        ):
            raise
        rendered = None
    return (
        series,
        issue,
        editor_fields,
        conflicts,
        shared_choices,
        rendered if ready else None,
        editor,
    )


def preview_xml_changes(
    files: ArchiveMetadataFiles, rendered: RenderedArchiveMetadata
) -> list[FileMetadataChange]:
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
    return changes[:200]


def _prepare(
    target: ArchiveMetadataTarget,
    files: ArchiveMetadataFiles,
    choices: FileMetadataChoices,
    editor: IssueMetadataEditorReview | None = None,
) -> PreparedFileMetadata:
    binding = target.binding
    converts = target.path.suffix.casefold() in {".cbr", ".cb7", ".pdf"}
    archive = reconcile_archive_metadata(
        files,
        story_baseline=binding.metadata.issues[0].baseline,
        reviewing_stories=editor is not None and "story_titles" in editor.reviewed_xml_fields,
    )
    series, issue = assemble_bound_archive_metadata(binding, archive, now=datetime.now(UTC))
    primary = archive_primary_identity(binding, archive)
    series, issue, editor_fields, conflicts, shared_choices, rendered, effective = (
        review_metadata_pair(
            binding.metadata, files, archive, series, issue, primary, choices, editor
        )
    )
    evidence = TypeAdapter(ArchiveMetadataTarget).dump_json(target)
    evidence += json.dumps(choices, sort_keys=True, separators=(",", ":")).encode()
    if editor is not None:
        evidence += json.dumps(editor.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    if rendered is None:
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
                editor_fields=editor_fields,
                editor_changed=bool(effective and effective.edits),
            ),
            shared_choices=shared_choices,
            effective_editor=effective,
        )
    changes = preview_xml_changes(files, rendered)
    review_key = hashlib.sha256(evidence + rendered.comicinfo + rendered.metroninfo).hexdigest()
    canonical_names = False
    if not converts:
        with ZipFile(target.path) as archive_file:
            canonical_names = {"ComicInfo.xml", "MetronInfo.xml"} <= set(archive_file.namelist())
    unchanged = (
        canonical_names
        and files.comicinfo.payload == rendered.comicinfo
        and files.metroninfo.payload == rendered.metroninfo
        and not (effective and any(edit.source != "reset" for edit in effective.edits.values()))
    )
    if (
        not converts
        and not choices
        and effective
        and effective.edits
        and all(e.source == "reset" for e in effective.edits.values())
        and {f"issue.{key}" for key in effective.edits}
        <= {f.key for f in editor_fields if f.reset_only}
    ):
        # A release-only field cannot authorize even incidental archive normalization.
        changes, unchanged = [], True
        review_key = hashlib.sha256(
            evidence + (files.comicinfo.payload or b"") + (files.metroninfo.payload or b"")
        ).hexdigest()
    return PreparedFileMetadata(
        target,
        series,
        issue,
        primary,
        FileMetadataPreview(
            file_id=binding.library_file_id,
            file_name=target.path.name,
            changes=changes,
            review_key=review_key,
            unchanged=unchanged,
            converts_to_cbz=converts,
            conflicts=conflicts,
            editor_fields=editor_fields,
            editor_changed=bool(effective and effective.edits),
        ),
        shared_choices=shared_choices,
        effective_editor=effective,
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
    editor: IssueMetadataEditorReview | None = None,
    propagation_fields: frozenset[str] | None = None,
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
        if editor is not None or propagation_fields is not None:
            raise ArchiveMetadataBindingError("editor_conversion_required")
        return await write_native_file_metadata(
            factory,
            issue_id,
            review_key,
            operation,
            choices=choices,
            check_control=check_control,
            progress=progress,
            limit=limit,
        )
    recovered = await recover_file_metadata(factory, operation)
    if recovered is PublicationState.FINALIZED:
        return "recovered"
    if recovered is not None:
        raise ArchivePublicationError("approval_changed")
    await check_control()
    async with factory() as session:
        prepared = await prepare_file_metadata(
            session, issue_id, choices=choices, editor=editor, propagation_fields=propagation_fields
        )
    if not prepared.preview.ready:
        raise ArchivePublicationError("unresolved_conflicts")
    if prepared.preview.review_key != review_key:
        raise ArchivePublicationError("approval_changed")
    editor = prepared.effective_editor
    if prepared.preview.unchanged:
        if editor and editor.edits:
            from pullbox.services.issue_metadata_override_release import release_issue_overrides

            await check_control()
            async with factory.begin() as session:
                return await release_issue_overrides(session, prepared, editor, operation, job_id)
        return "unchanged"
    target, series, issue = prepared.target, prepared.series, prepared.issue
    limit = max(limit, prepared.approved_resource_limit or 0)
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
            shared_choices=prepared.shared_choices,
            reviewed_xml_fields=editor.reviewed_xml_fields if editor else frozenset(),
            propagation_fields=propagation_fields,
        ) as staged:
            plan = await prepare_archive_publication(
                target, staged, series, issue, propagation_fields=propagation_fields
            )
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
