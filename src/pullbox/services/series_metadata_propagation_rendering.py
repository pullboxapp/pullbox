"""Selected series-field projection into the existing reconciled XML pair."""

from collections.abc import Iterable
from xml.etree import ElementTree as ET

from pydantic import ValidationError

from pullbox.core.archive_metadata import ArchiveMetadataFiles
from pullbox.core.archive_metadata_fields import COMICINFO_ONLY_FORMATS, archive_format_label
from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.metadata_xml import MetadataXmlError, parse_metadata_xml
from pullbox.core.metroninfo_schema import validate_metroninfo_xml
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.schemas.series_publication_titles import MAX_PUBLICATION_TITLES, SeriesPublicationTitle
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
    "volume_count": "Total volumes",
    "publication_titles": "Alternate publication titles",
}


class ProtectedPublicationTitlesError(ArchiveMetadataRenderError):
    """Carry editor-safe evidence for the preview, never XML or IDs in exception text."""

    def __init__(self, keys: Iterable[tuple[str, str]]) -> None:
        super().__init__("propagation_titles_identity")
        self.entries: list[SeriesPublicationTitle] = []
        self.not_shown = 0
        for name, language in keys:
            if len(self.entries) < MAX_PUBLICATION_TITLES:
                try:
                    self.entries.append(SeriesPublicationTitle(name=name, language=language))
                    continue
                except ValidationError:
                    pass
            self.not_shown += 1


def _publication_titles(parent: ET.Element, series: MetadataSnapshot) -> None:
    group = parent.find("AlternativeNames")
    titles = series.values.publication_titles
    if not titles and group is None:
        return
    if group is None:
        group = ET.SubElement(parent, "AlternativeNames")
    existing: dict[tuple[str, str], ET.Element] = {}
    for node in group:
        if node.tag != "AlternativeName" or len(node):
            raise ArchiveMetadataRenderError("propagation_titles_annotated")
        # MetronInfo defines omitted lang as English, regardless of the parent series language.
        key = (node.text or "", node.get("lang", "en"))
        if key in existing:
            raise ArchiveMetadataRenderError("propagation_titles_ambiguous")
        existing[key] = node
    ordered = []
    for title in titles or ():
        match = existing.pop((title.name, title.language), None)
        if match is None:
            match = ET.Element("AlternativeName", lang=title.language)
            match.text = title.name
        ordered.append(match)
    protected = [key for key, node in existing.items() if "id" in node.attrib]
    if protected:
        raise ProtectedPublicationTitlesError(protected)
    if (
        not titles
        and len(group)
        and not any(o.field == "publication_titles" and o.user_override for o in series.origins)
    ):
        raise ArchiveMetadataRenderError("propagation_titles_unset")
    if ordered:
        group[:] = ordered
    elif len(group):
        parent.remove(group)


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
    archive = reconcile_archive_metadata(files, story_baseline=issue)
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
    titles_only = fields == {"publication_titles"}
    if titles_only:
        # The general writer normalizes legacy IDs and schema hints. A titles-only
        # approval cannot also authorize those changes.
        original = parse_metadata_xml(
            files.metroninfo.payload, root_name="MetronInfo", preserve_misc=True
        )
        if ET.tostring(original) != ET.tostring(mi):
            try:
                validate_metroninfo_xml(files.metroninfo.payload)
            except MetadataXmlError as exc:
                raise ArchiveMetadataRenderError("propagation_titles_legacy_xml") from exc
            mi = original
    parent = mi.find("Series")
    if parent is None:
        parent = ET.SubElement(mi, "Series")
    if "publication_titles" in fields:
        _publication_titles(parent, series)
    for field, ci_tag, mi_tag in (
        ("title", "Series", "Name"),
        ("sort_title", None, "SortName"),
        ("year_start", None, "StartYear"),
        ("volume", None, "Volume"),
        ("volume_count", None, "VolumeCount"),
    ):
        if field not in fields:
            continue
        value = getattr(series.values, field)
        if field == "title" and not value:
            raise ArchiveMetadataRenderError("missing_series_title")
        if (
            field == "volume_count"
            and value is None
            and parent.find(mi_tag) is not None
            and not any(o.field == field and o.user_override for o in series.origins)
        ):
            raise ArchiveMetadataRenderError("propagation_volume_count_unset")
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
    return _validated_pair(
        ci,
        mi,
        story_baseline=issue,
        original_comicinfo=files.comicinfo.payload if titles_only else None,
    )
