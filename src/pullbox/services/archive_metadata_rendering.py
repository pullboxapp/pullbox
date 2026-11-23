"""Render coordinated metadata without authorizing filesystem or identity changes."""

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from pullbox.core.archive_metadata import ArchiveMetadataFiles, MetadataFile
from pullbox.core.archive_metadata_fields import (
    COMICINFO_ONLY_FORMATS as _COMICINFO_ONLY_FORMATS,
)
from pullbox.core.archive_metadata_fields import COMICINFO_ROLES, comicinfo_credits
from pullbox.core.archive_metadata_fields import archive_format_label as _format_text
from pullbox.core.comicinfo_text import (
    COMICINFO_ONLY_TAGS,
    FLAG_CHOICES,
    METRON_ONLY_TAGS,
    comicinfo_text_ids,
    embedded_comicinfo_value,
)
from pullbox.core.issue_community_rating import annotated_rating, embedded_score
from pullbox.core.issue_notes import UneditableNotesError, document_notes
from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    IdentityNamespace,
    MetadataEntityKind,
)
from pullbox.core.metadata_xml import MetadataXmlError, parse_metadata_xml
from pullbox.core.metroninfo_schema import validate_metroninfo_xml
from pullbox.schemas.issue_file_metadata import SharedXmlChoices
from pullbox.schemas.issue_metadata_editor import EditorXmlField
from pullbox.schemas.metadata_issue_arcs import comicinfo_arcs
from pullbox.schemas.metadata_labels import LABEL_XML_PATHS
from pullbox.schemas.metadata_snapshot import MetadataSnapshot, MetadataValues
from pullbox.services.archive_metadata_preservation import (
    ArchiveMetadataRenderError as ArchiveMetadataRenderError,
)
from pullbox.services.archive_metadata_preservation import (
    apply_issue_labels,
    apply_metron_labels,
    apply_reviewed_arcs,
    coordinate_preserved_fields,
    editor_arc_value,
    embedded_labels,
    embedded_reprints,
    metron_simple_text,
    preserved_roots,
    set_reviewed_imprint,
    set_text,
)
from pullbox.services.archive_metadata_reconciliation import (
    ArchiveMetadataReconciliation,
    reconcile_archive_metadata,
)

_SOURCES = {
    IdentityNamespace.COMICVINE: "Comic Vine",
    IdentityNamespace.METRON: "Metron",
    IdentityNamespace.GCD: "Grand Comics Database",
}
_SERIES_PATHS = {
    "title": ("Series", "Name"),
    "sort_title": (None, "SortName"),
    "year_start": (None, "StartYear"),
    "volume": (None, "Volume"),
    "series_type": ("Format", "Format"),
    "issue_count": ("Count", "IssueCount"),
}
_ISSUE_PATHS = {
    "issue_number_text": ("Number", "Number"),
    "description": ("Summary", "Summary"),
    "page_count": ("PageCount", "PageCount"),
    "store_date": (None, "StoreDate"),
}


@dataclass(frozen=True)
class RenderedArchiveMetadata:
    comicinfo: bytes
    metroninfo: bytes


def render_archive_metadata(
    series: MetadataSnapshot,
    issue: MetadataSnapshot,
    files: ArchiveMetadataFiles,
    *,
    primary_identity: ExternalIdentityRef | None = None,
    previous_series: MetadataSnapshot | None = None,
    previous_issue: MetadataSnapshot | None = None,
    replace_managed: bool = False,
    shared_choices: SharedXmlChoices | None = None,
    reviewed_xml_fields: frozenset[EditorXmlField] = frozenset(),
    propagation_fields: frozenset[str] | None = None,
) -> RenderedArchiveMetadata:
    """Render both documents from bound snapshots; this is not write permission.

    The caller must independently bind the issue and its parent, assemble local
    metadata into these snapshots, and authorize the eventual file operation.
    Previous managed baselines authorize refresh only while XML is unchanged.
    """
    if propagation_fields is not None:
        from pullbox.services.series_metadata_propagation_rendering import render_series_propagation

        return render_series_propagation(series, issue, files, propagation_fields)
    from pullbox.services.archive_series_metadata import archive_render_series

    series = archive_render_series(series)
    if previous_series is not None:
        previous_series = archive_render_series(previous_series)
    if (
        series.entity_kind is not MetadataEntityKind.SERIES
        or issue.entity_kind is not MetadataEntityKind.ISSUE
    ):
        raise ArchiveMetadataRenderError("invalid_snapshot")
    for current, previous in ((series, previous_series), (issue, previous_issue)):
        if previous is not None and (
            previous.entity_kind != current.entity_kind
            or not set(previous.identities) <= set(current.identities)
        ):
            raise ArchiveMetadataRenderError("invalid_baseline")
    archive = reconcile_archive_metadata(files)
    _identities(series, issue, archive)
    if any(item.code != "unmapped_content" for item in archive.diagnostics):
        raise ArchiveMetadataRenderError("invalid_input")
    ci, mi = preserved_roots(files)
    primary = _primary(issue, archive, primary_identity)
    _namespace_guard(mi, archive, primary)
    _guard_values(series, issue, archive, previous_series, previous_issue, replace_managed)
    for field, tag in (("series_type", "Format"), ("issue_count", "Count")):
        text = ci.findtext(tag)
        old_value: str | int | None = text
        if text and field == "issue_count":
            if len(text) > 10 or not text.isascii() or not text.isdecimal():
                raise ArchiveMetadataRenderError("invalid_input", tag)
            old_value = int(text)
        comparison = getattr(series.values, field)
        if field == "series_type":
            old_value, comparison = _format_text(text), _format_text(series.values.series_type)
        if (
            text
            and old_value != comparison
            and not _authorized(series, previous_series, field, old_value, replace_managed)
        ):
            raise ArchiveMetadataRenderError("unreconciled_field", field)
    _render_core(series, issue, ci, mi)
    _render_ids(series, issue, ci, mi, primary)
    _render_credits(issue, archive, ci, mi)
    protected = {o.field for o in issue.origins if o.user_override}
    if not reviewed_xml_fields <= protected:
        raise ArchiveMetadataRenderError("invalid_choice")
    if protected.intersection(COMICINFO_ONLY_TAGS):
        _render_comicinfo_fields(ci, issue.values, protected, reviewed_xml_fields)
    for metron_field, tag in METRON_ONLY_TAGS.items():
        if metron_field not in protected:
            continue
        try:
            existing = metron_simple_text(mi, tag)
        except ValueError as exc:
            raise ArchiveMetadataRenderError("label_override_changed", tag) from exc
        desired = getattr(issue.values, metron_field)
        reviewed = metron_field in reviewed_xml_fields
        if existing != desired or (reviewed and mi.find(tag) is not None and desired is None):
            if not reviewed:
                raise ArchiveMetadataRenderError("label_override_changed", tag)
            set_text(mi, tag, desired)
    if "reprints" in protected:
        try:
            reprints = embedded_reprints(mi)
        except ValueError as exc:
            raise ArchiveMetadataRenderError("label_override_changed", "Reprints") from exc
        desired_reprints = issue.values.reprints or ()
        if "reprints" in reviewed_xml_fields:
            if reprints != desired_reprints or not desired_reprints:
                apply_metron_labels(mi, "Reprints/Reprint", desired_reprints)
        elif reprints != desired_reprints:
            raise ArchiveMetadataRenderError("label_override_changed", "Reprints")
    if "community_rating" in protected:
        _render_community_rating(
            ci, mi, issue.values.community_rating, "community_rating" in reviewed_xml_fields
        )
    if "notes" in protected:
        for root in (ci, mi):
            try:
                note = document_notes(root)
                if note.prose == issue.values.notes:
                    continue
                if "notes" not in reviewed_xml_fields:
                    raise ArchiveMetadataRenderError("label_override_changed", "Notes")
                replacement = note.replace(issue.values.notes)
            except UneditableNotesError as exc:
                code = (
                    "unsafe_note_edit"
                    if "notes" in reviewed_xml_fields
                    else "label_override_changed"
                )
                raise ArchiveMetadataRenderError(code, "Notes") from exc
            set_text(root, "Notes", replacement)
    if "age_rating" in reviewed_xml_fields:
        for root in (ci, mi):
            set_text(root, "AgeRating", issue.values.age_rating)
    elif "age_rating" in protected and any(
        root.findtext("AgeRating") != issue.values.age_rating for root in (ci, mi)
    ):
        # A saved correction is not fresh approval to replace externally changed metadata.
        raise ArchiveMetadataRenderError("rating_override_changed", "AgeRating")
    if "imprint" in reviewed_xml_fields:
        set_reviewed_imprint(mi, issue.values.imprint)
        set_text(ci, "Imprint", issue.values.imprint)
    elif "imprint" in protected and (
        ci.findtext("Imprint") != issue.values.imprint
        or mi.findtext("Publisher/Imprint") != issue.values.imprint
    ):
        raise ArchiveMetadataRenderError("label_override_changed", "Imprint")
    for field, (ci_path, mi_path) in LABEL_XML_PATHS.items():
        values = getattr(issue.values, field) or ()
        if field in reviewed_xml_fields:
            apply_issue_labels(ci, mi, ci_path, mi_path, values)
        elif field in protected and (
            embedded_labels(ci, ci_path) != values or embedded_labels(mi, mi_path) != values
        ):
            raise ArchiveMetadataRenderError("label_override_changed", ci_path)
    arcs = issue.values.story_arcs or ()
    if "story_arcs" in reviewed_xml_fields:
        apply_reviewed_arcs(ci, mi, arcs)
    elif "story_arcs" in protected and (
        (editor_arc_value(ci, comicinfo=True) or ()) != comicinfo_arcs(arcs)
        or (editor_arc_value(mi, comicinfo=False) or ()) != arcs
    ):
        raise ArchiveMetadataRenderError("label_override_changed", "StoryArc")
    coordinate_preserved_fields(ci, mi, shared_choices)
    return _validated_pair(ci, mi)


def _render_comicinfo_fields(
    ci: ET.Element, values: MetadataValues, protected: set[str], reviewed: frozenset[EditorXmlField]
) -> None:
    def document_ids() -> tuple[set[str], set[str]]:
        # Wrapper matching reads all text together, so an ID may span neighboring elements.
        # The nested reader ignores comments and processing instructions.
        root = ci
        if any(not isinstance(node.tag, str) for node in ci.iter()):
            try:
                root = parse_metadata_xml(ET.tostring(ci, encoding="utf-8"), root_name="ComicInfo")
            except MetadataXmlError as exc:
                raise ArchiveMetadataRenderError("invalid_output") from exc
        return comicinfo_text_ids(" ".join(root.itertext()))

    for field, tag in COMICINFO_ONLY_TAGS.items():
        if field not in protected:
            continue
        try:
            raw = ci.findtext(tag)
            existing = embedded_comicinfo_value(field, raw)
        except ValueError as exc:
            raise ArchiveMetadataRenderError("label_override_changed", tag) from exc
        desired = getattr(values, field)
        explicit_empty = field in reviewed and (
            (desired is None and raw is not None) or (field in FLAG_CHOICES and raw == "")
        )
        if existing == desired and not explicit_empty:
            continue
        if field not in reviewed:
            raise ArchiveMetadataRenderError("label_override_changed", tag)
        before = document_ids()
        set_text(ci, tag, ", ".join(desired) if field == "series_groups" and desired else desired)
        if before != document_ids():
            raise ArchiveMetadataRenderError("comicinfo_text_identity_change", tag)


def _render_community_rating(
    ci: ET.Element, mi: ET.Element, score: str | None, reviewed: bool
) -> None:
    if not reviewed:
        if any(embedded_score(root) != score for root in (ci, mi)):
            raise ArchiveMetadataRenderError("label_override_changed", "CommunityRating")
        return
    if annotated_rating(mi):
        raise ArchiveMetadataRenderError("unsupported_content", "CommunityRating")
    if embedded_score(ci) != score:
        set_text(ci, "CommunityRating", score)
    if embedded_score(mi) == score:
        return
    node = mi.find("CommunityRating")
    if node is not None:
        mi.remove(node)
    if score is not None:
        # A changed average is a correction, not the result of the old votes.
        ET.SubElement(ET.SubElement(mi, "CommunityRating"), "AverageRating").text = score


def _identities(
    series: MetadataSnapshot, issue: MetadataSnapshot, archive: ArchiveMetadataReconciliation
) -> None:
    verified = {identity for snapshot in (series, issue) for identity in snapshot.identities}
    if archive.identity_conflicts:
        raise ArchiveMetadataRenderError("identity_conflict")
    for item in archive.evidence:
        identity = item.identity
        if any(
            (identity.namespace, identity.entity_kind) == (target.namespace, target.entity_kind)
            and identity != target
            for target in verified
        ):
            raise ArchiveMetadataRenderError("identity_conflict")
        if identity not in verified:
            raise ArchiveMetadataRenderError("unverified_identity")


def _primary(
    issue: MetadataSnapshot,
    archive: ArchiveMetadataReconciliation,
    requested: ExternalIdentityRef | None,
) -> ExternalIdentityRef | None:
    if any(identity.namespace not in _SOURCES for identity in issue.identities):
        raise ArchiveMetadataRenderError("unsupported_identity")
    if requested is not None:
        if requested not in issue.identities:
            raise ArchiveMetadataRenderError("invalid_primary_identity")
        return requested
    data = archive.metroninfo.metron
    if data is not None:
        for item in data.identities:
            if item.primary and item.evidence.identity in issue.identities:
                return item.evidence.identity
    if len(issue.identities) > 1:
        raise ArchiveMetadataRenderError("primary_identity_required")
    return next(iter(issue.identities), None)


def _namespace_guard(
    mi: ET.Element, archive: ArchiveMetadataReconciliation, primary: ExternalIdentityRef | None
) -> None:
    old = archive.metroninfo.metron
    namespace = primary.namespace if primary is not None else (old.primary_source if old else None)
    if (
        old is not None
        and old.primary_source != namespace
        and any("id" in node.attrib for node in mi.iter() if node.tag != "Series")
    ):
        raise ArchiveMetadataRenderError("resource_namespace_change")


def _authorized(
    snapshot: MetadataSnapshot,
    previous: MetadataSnapshot | None,
    field: str,
    existing: object,
    replace_managed: bool,
    *,
    project_credits: bool = False,
) -> bool:
    if any(origin.field == field and origin.user_override for origin in snapshot.origins):
        return True
    baseline = getattr(previous.values, field) if previous is not None else None
    if field == "series_type" and previous is not None:
        baseline = _format_text(previous.values.series_type)
    if project_credits and previous is not None and previous.values.credits is not None:
        baseline = comicinfo_credits(previous.values.credits)
    return bool(
        replace_managed
        and previous is not None
        and baseline == existing
        and any(origin.field == field and origin.source is not None for origin in previous.origins)
        and any(origin.field == field and origin.source is not None for origin in snapshot.origins)
    )


def _guard_values(
    series: MetadataSnapshot,
    issue: MetadataSnapshot,
    archive: ArchiveMetadataReconciliation,
    previous_series: MetadataSnapshot | None,
    previous_issue: MetadataSnapshot | None,
    replace_managed: bool,
) -> None:
    for document, values in (("ComicInfo", archive.comicinfo), ("MetronInfo", archive.metroninfo)):
        for snapshot, existing, previous in (
            (series, values.series, previous_series),
            (issue, values.issue, previous_issue),
        ):
            for field in MetadataValues.model_fields:
                old, new = getattr(existing, field), getattr(snapshot.values, field)
                if old is None:
                    continue
                if field == "issue_number_text":
                    try:
                        equal = new is not None and normalize_issue_number_text(
                            old
                        ) == normalize_issue_number_text(new)
                    except ValueError:
                        equal = False
                    if not equal:
                        raise ArchiveMetadataRenderError("issue_number_conflict")
                    continue
                if (
                    field == "credits"
                    and document == "ComicInfo"
                    and snapshot.values.credits is not None
                ):
                    new = comicinfo_credits(snapshot.values.credits)
                if field == "series_type":
                    old, new = (
                        _format_text(existing.series_type),
                        _format_text(snapshot.values.series_type),
                    )
                if (
                    field in {"title", "sort_title"}
                    and snapshot.entity_kind is MetadataEntityKind.SERIES
                    and isinstance(old, str)
                    and isinstance(new, str)
                    and (
                        old.strip() == new.strip()
                        or (
                            field == "title"
                            and _series_separator_key(old) == _series_separator_key(new)
                            and any(item.identity in issue.identities for item in archive.evidence)
                        )
                    )
                ):
                    # XML readers trim both names; only verified titles may change separators.
                    continue
                if old != new and not _authorized(
                    snapshot,
                    previous,
                    field,
                    old,
                    replace_managed,
                    project_credits=field == "credits" and document == "ComicInfo",
                ):
                    raise ArchiveMetadataRenderError("unreconciled_field", field)
    parts = archive.comicinfo.publication_date_parts
    current = issue.values.cover_date
    if (
        current is None
        and archive.comicinfo.issue.cover_date is None
        and not any(
            origin.field == "cover_date" and origin.user_override for origin in issue.origins
        )
    ):
        return
    if any(part is not None for part in parts):
        new_parts = (
            (current.year, current.month, current.day)
            if current is not None
            else (None, None, None)
        )
        if any(old is not None and old != new for old, new in zip(parts, new_parts, strict=True)):
            previous_date = previous_issue.values.cover_date if previous_issue is not None else None
            old_parts = (
                (previous_date.year, previous_date.month, previous_date.day)
                if previous_date is not None
                else (None, None, None)
            )
            if not _authorized(
                issue, previous_issue, "cover_date", previous_date, replace_managed
            ) or not (
                any(
                    origin.field == "cover_date" and origin.user_override
                    for origin in issue.origins
                )
                or all(
                    part is None or part == old for part, old in zip(parts, old_parts, strict=True)
                )
            ):
                raise ArchiveMetadataRenderError("unreconciled_field", "cover_date")


def _series_separator_key(value: str) -> str:
    value = re.sub(r"\s+[-\u2013\u2014]\s+", " : ", value)
    return " ".join(value.replace(":", " : ").casefold().split())


def _render_core(
    series: MetadataSnapshot, issue: MetadataSnapshot, ci: ET.Element, mi: ET.Element
) -> None:
    if not series.values.title or not series.values.title.strip():
        raise ArchiveMetadataRenderError("missing_series_title")
    parent = mi.find("Series")
    if parent is None:
        parent = ET.SubElement(mi, "Series")
    for field, (ci_tag, mi_tag) in _SERIES_PATHS.items():
        value = getattr(series.values, field)
        if field == "series_type":
            value = _format_text(series.values.series_type)
        if ci_tag is not None:
            set_text(ci, ci_tag, value)
        if field == "series_type" and series.values.series_type in _COMICINFO_ONLY_FORMATS:
            value = None
        if field == "year_start" and series.values.year_start is not None:
            value = f"{series.values.year_start:04d}"
        set_text(parent, mi_tag, None if field == "issue_count" and value == 0 else value)
    language = series.values.language
    set_text(ci, "LanguageISO", language)
    if language:
        parent.set("lang", language)
    else:
        parent.attrib.pop("lang", None)
    publisher = mi.find("Publisher")
    if (
        publisher is not None
        and "id" in publisher.attrib
        and publisher.findtext("Name") != series.values.publisher
    ):
        raise ArchiveMetadataRenderError("resource_metadata_change", "publisher")
    if series.values.publisher:
        if publisher is None:
            publisher = ET.SubElement(mi, "Publisher")
        set_text(publisher, "Name", series.values.publisher)
    elif publisher is not None:
        if publisher.attrib or publisher.find("Imprint") is not None:
            raise ArchiveMetadataRenderError("unsupported_content", "Publisher")
        mi.remove(publisher)
    set_text(ci, "Publisher", series.values.publisher)
    for field, (ci_tag, mi_tag) in _ISSUE_PATHS.items():
        value = getattr(issue.values, field)
        if ci_tag is not None:
            set_text(ci, ci_tag, value)
        set_text(mi, mi_tag, value)
    cover = issue.values.cover_date
    set_text(mi, "CoverDate", cover)
    if (
        cover is not None
        or all(ci.findtext(tag) for tag in ("Year", "Month", "Day"))
        or any(origin.field == "cover_date" and origin.user_override for origin in issue.origins)
    ):
        for tag, value in zip(
            ("Year", "Month", "Day"),
            (cover.year, cover.month, cover.day) if cover else (None, None, None),
            strict=True,
        ):
            set_text(ci, tag, value)
    title = issue.values.title
    stories = mi.find("Stories")
    if stories is not None and len(stories.findall("Story")) > 1:
        if title:
            raise ArchiveMetadataRenderError("unreconciled_field", "title")
    elif title:
        if stories is None:
            stories = ET.SubElement(mi, "Stories")
        set_text(stories, "Story", title)
    elif stories is not None:
        _assert_container_replaceable(stories, "title")
        mi.remove(stories)
    set_text(ci, "Title", title)


def _render_ids(
    series: MetadataSnapshot,
    issue: MetadataSnapshot,
    ci: ET.Element,
    mi: ET.Element,
    primary: ExternalIdentityRef | None,
) -> None:
    ids = mi.find("IDS")
    if ids is None and issue.identities:
        ids = ET.SubElement(mi, "IDS")
    if ids is not None:
        for child in ids:
            if child.get("source", "").casefold() in {"league of comic geeks", "locg"}:
                child.set("source", "League of Comic Geeks")
                if primary is not None:
                    child.attrib.pop("primary", None)
        for identity in sorted(
            issue.identities, key=lambda item: (item.namespace, item.external_id)
        ):
            nodes = [
                node
                for node in ids.findall("ID")
                if node.get("source") == _SOURCES[identity.namespace]
            ]
            if not nodes:
                nodes = [ET.SubElement(ids, "ID", {"source": _SOURCES[identity.namespace]})]
            for child in nodes:
                child.text = identity.external_id
                child.attrib.pop("primary", None)
            if identity == primary:
                nodes[0].set("primary", "true")
    parent = mi.find("Series")
    assert parent is not None
    if primary is not None:
        parent.attrib.pop("id", None)
        for identity in series.identities:
            if identity.namespace == primary.namespace:
                parent.set("id", identity.external_id)
    notes = ci.findtext("Notes") or ""
    for snapshot, marker in ((series, "cv_vol_id"), (issue, "cv_issue_id")):
        for identity in snapshot.identities:
            if identity.namespace is IdentityNamespace.COMICVINE:
                token = f"[{marker}:{identity.external_id}]"
                if token not in notes:
                    notes = f"{notes} {token}".strip()
    if notes:
        set_text(ci, "Notes", notes)


def _render_credits(
    issue: MetadataSnapshot, archive: ArchiveMetadataReconciliation, ci: ET.Element, mi: ET.Element
) -> None:
    credits = issue.values.credits
    if credits is None:
        if any(origin.field == "credits" and origin.user_override for origin in issue.origins):
            existing = mi.find("Credits")
            if existing is not None:
                _assert_container_replaceable(existing, "credits")
                mi.remove(existing)
            for tag in COMICINFO_ROLES:
                set_text(ci, tag, None)
        return
    for tag, role in COMICINFO_ROLES.items():
        names = [credit.name for credit in credits if role in credit.role.split(", ")]
        set_text(ci, tag, ", ".join(names))
    if not comicinfo_credits(credits):
        ET.SubElement(ci, "Writer")
    if archive.metroninfo.issue.credits == credits:
        return
    existing = mi.find("Credits")
    if existing is not None:
        _assert_container_replaceable(existing, "credits")
        mi.remove(existing)
    parent = ET.SubElement(mi, "Credits")
    for credit in credits:
        node = ET.SubElement(parent, "Credit")
        ET.SubElement(node, "Creator").text = credit.name
        if credit.role:
            roles = ET.SubElement(node, "Roles")
            for role in credit.role.split(", "):
                ET.SubElement(roles, "Role").text = role.title()


def _assert_container_replaceable(node: ET.Element, field: str) -> None:
    if any("id" in child.attrib for child in node.iter()):
        raise ArchiveMetadataRenderError("resource_metadata_change", field)
    if any(not isinstance(child.tag, str) for child in node.iter()):
        raise ArchiveMetadataRenderError("unsupported_content", field)


def _validated_pair(ci: ET.Element, mi: ET.Element) -> RenderedArchiveMetadata:
    result = RenderedArchiveMetadata(
        ET.tostring(ci, encoding="utf-8", xml_declaration=True),
        ET.tostring(mi, encoding="utf-8", xml_declaration=True),
    )
    try:
        parse_metadata_xml(result.comicinfo, root_name="ComicInfo")
        validate_metroninfo_xml(result.metroninfo)
    except MetadataXmlError as exc:
        raise ArchiveMetadataRenderError("invalid_output") from exc
    check = reconcile_archive_metadata(
        ArchiveMetadataFiles(
            MetadataFile("ComicInfo.xml", 1, result.comicinfo),
            MetadataFile("MetronInfo.xml", 1, result.metroninfo),
        )
    )
    if (
        check.differences
        or check.identity_conflicts
        or any(item.code != "unmapped_content" for item in check.diagnostics)
    ):
        locator = next(
            (item.locator for item in check.diagnostics if item.code != "unmapped_content"),
            check.differences[0].field if check.differences else "",
        )
        raise ArchiveMetadataRenderError("output_disagreement", locator)
    return result
