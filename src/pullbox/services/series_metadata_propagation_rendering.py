"""Selected series-field projection into the existing reconciled XML pair."""

from xml.etree import ElementTree as ET

from pullbox.core.archive_metadata import ArchiveMetadataFiles
from pullbox.core.archive_metadata_fields import COMICINFO_ONLY_FORMATS, archive_format_label
from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.services.archive_metadata_preservation import (
    preserved_roots,
    review_preserved_fields,
    set_text,
)
from pullbox.services.archive_metadata_reconciliation import reconcile_archive_metadata
from pullbox.services.archive_metadata_rendering import (
    ArchiveMetadataRenderError,
    RenderedArchiveMetadata,
    _identities,
    _validated_pair,
)

FIELDS = {
    "title": "Series name",
    "publisher": "Publisher",
    "language": "Language",
    "format": "Publication format",
    "sort_title": "Series sort name",
    "year_start": "Series start year",
    "volume": "Series volume",
}


def render_series_propagation(
    series: MetadataSnapshot,
    issue: MetadataSnapshot,
    files: ArchiveMetadataFiles,
    fields: frozenset[str],
) -> RenderedArchiveMetadata:
    if not fields or not fields <= FIELDS.keys():
        raise ArchiveMetadataRenderError("invalid_choice")
    if not files.comicinfo.payload or not files.metroninfo.payload:
        raise ArchiveMetadataRenderError("propagation_pair_required")
    archive = reconcile_archive_metadata(files)
    _identities(series, issue, archive)
    if any(item.code != "unmapped_content" for item in archive.diagnostics):
        raise ArchiveMetadataRenderError("invalid_input")
    for document in (archive.comicinfo, archive.metroninfo):
        number = document.issue.issue_number_text
        if number is not None and normalize_issue_number_text(
            number
        ) != normalize_issue_number_text(issue.values.issue_number_text or ""):
            raise ArchiveMetadataRenderError("issue_number_conflict")
    conflicts = review_preserved_fields(files, {})
    if conflicts:
        raise ArchiveMetadataRenderError("unreconciled_field", conflicts[0].key)
    ci, mi = preserved_roots(files)
    parent = mi.find("Series")
    if parent is None:
        parent = ET.SubElement(mi, "Series")
    for field, ci_tag, mi_tag in (
        ("title", "Series", "Name"),
        ("sort_title", None, "SortName"),
        ("year_start", None, "StartYear"),
        ("volume", None, "Volume"),
    ):
        if field not in fields:
            continue
        value = getattr(series.values, field)
        if field == "title" and not value:
            raise ArchiveMetadataRenderError("missing_series_title")
        if field == "year_start" and value is not None:
            value = f"{value:04d}"
        if ci_tag:
            set_text(ci, ci_tag, value)
        set_text(parent, mi_tag, value)
    if "language" in fields:
        language = series.values.language
        set_text(ci, "LanguageISO", language)
        if language:
            parent.set("lang", language)
        else:
            parent.attrib.pop("lang", None)
    if "format" in fields:
        value = series.values.archive_format or series.values.series_type
        set_text(ci, "Format", archive_format_label(value))
        set_text(
            parent,
            "Format",
            None if value in COMICINFO_ONLY_FORMATS else archive_format_label(value),
        )
    if "publisher" in fields:
        publisher = mi.find("Publisher")
        value = series.values.publisher
        if publisher is not None and publisher.get("id") and publisher.findtext("Name") != value:
            raise ArchiveMetadataRenderError("resource_metadata_change", "publisher")
        if value:
            if publisher is None:
                publisher = ET.SubElement(mi, "Publisher")
            set_text(publisher, "Name", value)
        elif publisher is not None:
            if publisher.attrib or publisher.find("Imprint") is not None:
                raise ArchiveMetadataRenderError("unsupported_content", "Publisher")
            mi.remove(publisher)
        set_text(ci, "Publisher", value)
    return _validated_pair(ci, mi)
