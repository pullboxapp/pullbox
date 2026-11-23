"""Preserve schema-specific values while coordinating shared archive metadata."""

from collections import Counter, defaultdict
from copy import deepcopy
from typing import cast
from xml.etree import ElementTree as ET

from pydantic import ValidationError

from pullbox.core.archive_metadata import ArchiveMetadataFiles
from pullbox.core.archive_metadata_fields import normalize_comicinfo_aliases
from pullbox.core.comicinfo_text import descriptive_text
from pullbox.core.metadata_xml import MetadataXmlError, parse_metadata_xml
from pullbox.core.metroninfo_schema import validate_metroninfo_xml
from pullbox.schemas.issue_file_metadata import (
    FileMetadataChoices,
    FileMetadataConflict,
    FileMetadataOption,
    SharedXmlChoices,
    SharedXmlField,
)
from pullbox.schemas.metadata_issue_arcs import ARC_LIST, IssueArcEntry
from pullbox.schemas.metadata_labels import LABEL_XML_PATHS

_SCHEMA_HINT = "{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation"
_CI_FIELDS = frozenset(
    [
        "Title",
        "Series",
        "Number",
        "Count",
        "Volume",
        "AlternateSeries",
        "AlternateNumber",
        "AlternateCount",
        "Summary",
        "Notes",
        "Year",
        "Month",
        "Day",
        "Writer",
        "Penciller",
        "Inker",
        "Colorist",
        "Letterer",
        "CoverArtist",
        "Editor",
        "Translator",
        "Publisher",
        "Imprint",
        "Genre",
        "Tags",
        "Web",
        "PageCount",
        "LanguageISO",
        "Format",
        "BlackAndWhite",
        "Manga",
        "Characters",
        "Teams",
        "Locations",
        "ScanInformation",
        "StoryArc",
        "StoryArcNumber",
        "SeriesGroup",
        "AgeRating",
        "CommunityRating",
        "MainCharacterOrTeam",
        "Review",
        "GTIN",
    ]
)
_PAGE_ATTRS = frozenset(
    ["Image", "Type", "DoublePage", "ImageSize", "Key", "Bookmark", "ImageWidth", "ImageHeight"]
)


class ArchiveMetadataRenderError(ValueError):
    """Fixed error code and field locator, not untrusted metadata content."""

    def __init__(self, code: str, field: str = "") -> None:
        self.code = code
        self.field = field
        super().__init__(code)


def preserved_roots(files: ArchiveMetadataFiles) -> tuple[ET.Element, ET.Element]:
    roots = []
    for member, name in ((files.comicinfo, "ComicInfo"), (files.metroninfo, "MetronInfo")):
        if (
            member.entry_count > 1
            or member.diagnostics
            or (member.entry_count and member.payload is None)
        ):
            raise ArchiveMetadataRenderError("invalid_input", name)
        try:
            root = (
                parse_metadata_xml(member.payload, root_name=name, preserve_misc=True)
                if member.payload is not None
                else ET.Element(name)
            )
            if name == "ComicInfo":
                normalize_comicinfo_aliases(root)
                _validate_comicinfo(root)
            elif member.payload is not None:
                if root.get("version") not in {None, "1.0", "1.1"}:
                    raise ArchiveMetadataRenderError("unsupported_content", name)
                root.attrib.pop("version", None)
                root.attrib.pop(_SCHEMA_HINT, None)
                for node in root.findall("IDS/ID"):
                    source = node.get("source", "").strip().casefold()
                    label = {
                        "comicvine": "Comic Vine",
                        "comic vine": "Comic Vine",
                        "metron": "Metron",
                        "gcd": "Grand Comics Database",
                        "grand comics database": "Grand Comics Database",
                        "locg": "League of Comic Geeks",
                        "league of comic geeks": "League of Comic Geeks",
                    }.get(source)
                    if label is not None:
                        node.set("source", label)
                for node in (*root.findall("GTIN/ISBN"), *root.findall("GTIN/UPC")):
                    if node.attrib or len(node):
                        raise ArchiveMetadataRenderError("unsupported_content", "GTIN")
                validate_metroninfo_xml(ET.tostring(root, encoding="utf-8"))
        except MetadataXmlError as exc:
            raise ArchiveMetadataRenderError("invalid_input", name) from exc
        roots.append(root)
    return roots[0], roots[1]


def _validate_comicinfo(root: ET.Element) -> None:
    if set(root.attrib) - {_SCHEMA_HINT} or (root.text or "").strip():
        raise ArchiveMetadataRenderError("unsupported_content", "ComicInfo")
    seen = set()
    for node in root:
        if not isinstance(node.tag, str):
            continue
        if node.tag in seen or (node.tail or "").strip():
            raise ArchiveMetadataRenderError("unsupported_content", "ComicInfo")
        seen.add(node.tag)
        if node.tag == "Pages":
            if node.attrib or (node.text or "").strip():
                raise ArchiveMetadataRenderError("unsupported_content", "Pages")
            for page in node:
                if not isinstance(page.tag, str):
                    continue
                if (
                    page.tag != "Page"
                    or set(page.attrib) - _PAGE_ATTRS
                    or len(page)
                    or (page.text or "").strip()
                    or (page.tail or "").strip()
                ):
                    raise ArchiveMetadataRenderError("unsupported_content", "Pages")
        elif node.tag not in _CI_FIELDS or node.attrib or len(node):
            raise ArchiveMetadataRenderError("unsupported_content", "ComicInfo")


def metron_simple_text(mi: ET.Element, tag: str) -> str | None:
    node = mi.find(tag)
    if node is None:
        return None
    if len(node) or node.attrib:
        raise ValueError("This value contains an XML annotation.")
    return descriptive_text(node.text or "")


def set_text(root: ET.Element, path: str, value: object) -> None:
    node = root.find(path)
    if node is not None and "id" in node.attrib and node.text != str(value):
        raise ArchiveMetadataRenderError("resource_metadata_change", path)
    if value is None or value == "":
        if node is not None:
            if node.attrib or len(node):
                raise ArchiveMetadataRenderError("unsupported_content", path)
            root.remove(node)
        return
    if node is None:
        node = ET.SubElement(root, path)
    node.text = str(value)


def _parts(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip()) if value else ()


def embedded_labels(root: ET.Element, path: str) -> tuple[str, ...]:
    if "/" not in path:
        return _parts(root.findtext(path))
    return tuple((n.text or "").strip() for n in root.findall(path))


def set_reviewed_imprint(mi: ET.Element, value: str | None) -> None:
    publisher = mi.find("Publisher")
    if publisher is None:
        if value:
            raise ArchiveMetadataRenderError("publisher_required", "Imprint")
        return
    previous = publisher.find("Imprint")
    if previous is not None:
        if len(previous):
            raise ArchiveMetadataRenderError("unsupported_content", "Imprint")
        if value is not None and previous.text == value:
            return
        # The ID belongs to the old imprint, not to its replacement or its publisher.
        publisher.remove(previous)
    if value:
        ET.SubElement(publisher, "Imprint").text = value


def apply_issue_labels(
    ci: ET.Element, mi: ET.Element, ci_path: str, mi_path: str, values: tuple[str, ...]
) -> None:
    set_text(ci, ci_path, ", ".join(values))
    apply_metron_labels(mi, mi_path, values)


def apply_metron_labels(mi: ET.Element, mi_path: str, values: tuple[str, ...]) -> None:
    container = mi_path.split("/")[0]
    parent = mi.find(container)
    if parent is None:
        if not values:
            return
        parent = ET.SubElement(mi, container)
    _replace_list(mi, mi_path, values)
    if not len(parent) and not parent.attrib:
        mi.remove(parent)


def embedded_reprints(mi: ET.Element) -> tuple[str, ...]:
    parent = mi.find("Reprints")
    if parent is not None and any(node.tag != "Reprint" or len(node) for node in parent):
        raise ValueError("Reprints contain an XML annotation.")
    return embedded_labels(mi, "Reprints/Reprint")


SHARED_FIELD_LABELS = {
    "CommunityRating": "community rating",
    "Notes": "notes",
    "AgeRating": "age rating",
    "Genre": "genres",
    "Tags": "tags",
    "Characters": "characters",
    "Teams": "teams",
    "Locations": "locations",
    "Imprint": "imprint",
    "StoryArc": "story arcs and positions",
    "StoryArcNumber": "story-arc positions",
}
_LIST_FIELDS = tuple(LABEL_XML_PATHS.values())
_SCALAR_FIELDS = (("Imprint", "Publisher/Imprint"), ("AgeRating", "AgeRating"))
_AGE_RATINGS = frozenset(
    {"Unknown", "Everyone", "Teen", "Teen Plus", "Mature", "Explicit", "Adult"}
)


def shared_xml_choices(choices: FileMetadataChoices) -> SharedXmlChoices:
    result: SharedXmlChoices = {}
    for key, source in choices.items():
        if key.startswith("xml."):
            if source not in {"ComicInfo.xml", "MetronInfo.xml"}:
                raise ArchiveMetadataRenderError("invalid_choice", key.split(".")[1])
            result[cast("SharedXmlField", key)] = source
    return result


def review_preserved_fields(
    files: ArchiveMetadataFiles, choices: SharedXmlChoices, *, editor: bool = False
) -> list[FileMetadataConflict]:
    ci, mi = preserved_roots(files)
    return _resolve_shared_fields(ci, mi, choices, editor=editor)


def _normalize_rating(ci: ET.Element, mi: ET.Element) -> None:
    left, right = ci.findtext("AgeRating"), mi.findtext("AgeRating")
    for root, value, other in ((ci, left, right), (mi, right, left)):
        if value and value.strip().casefold() == "unknown" and other in _AGE_RATINGS:
            set_text(
                root, "AgeRating", other if other.strip().casefold() != "unknown" else "Unknown"
            )


def _arc_values(
    ci: ET.Element, mi: ET.Element
) -> tuple[tuple[tuple[str, str | None], ...], tuple[tuple[str, str | None], ...]]:
    names, numbers = _arc_parts(ci.findtext("StoryArc")), _arc_parts(ci.findtext("StoryArcNumber"))
    if numbers and (
        len(numbers) != len(names)
        or any(len(n) > 10 or not n.isascii() or not n.isdecimal() or int(n) < 1 for n in numbers)
    ):
        raise ArchiveMetadataRenderError("unsupported_content", "StoryArcNumber")
    left = tuple((name, str(int(numbers[i])) if numbers else None) for i, name in enumerate(names))
    right = tuple(
        ((a.findtext("Name") or "").strip(), a.findtext("Number")) for a in mi.findall("Arcs/Arc")
    )
    if any(not name or "," in name for name, _ in right):
        raise ArchiveMetadataRenderError("unsupported_content", "StoryArc")
    if any(
        n is not None and (len(n) > 10 or not n.isascii() or not n.isdecimal() or int(n) < 1)
        for _, n in right
    ):
        raise ArchiveMetadataRenderError("unsupported_content", "StoryArcNumber")
    right = tuple(
        (name, str(int(number)) if number is not None else None) for name, number in right
    )
    return left, right


def _arc_parts(value: str | None) -> tuple[str, ...]:
    if not value or not value.strip():
        return ()
    parts = tuple(part.strip() for part in value.split(","))
    if not all(parts):
        # Dropping empty slots could assign a later position to a different arc.
        raise ArchiveMetadataRenderError("unsupported_content", "StoryArc")
    return parts


def editor_arc_value(
    root: ET.Element, *, comicinfo: bool
) -> tuple[IssueArcEntry, ...] | str | None:
    empty = ET.Element("empty")
    try:
        if not comicinfo:
            nodes = root.findall("Arcs/Arc")
            if len(nodes) > 128 or any(
                (name := a.findtext("Name") or "") != name.strip() for a in nodes
            ):
                raise ArchiveMetadataRenderError("unsupported_content", "StoryArc")
        pairs = _arc_values(root, empty)[0] if comicinfo else _arc_values(empty, root)[1]
        if not pairs:
            return None
        if len(pairs) > 128:
            raise ArchiveMetadataRenderError("unsupported_content", "StoryArc")
        return ARC_LIST.validate_python(
            [
                {"name": name, "position": int(pos) if pos is not None else None}
                for name, pos in pairs
            ]
        )
    except (ArchiveMetadataRenderError, ValidationError):
        if comicinfo:
            names = root.findtext("StoryArc") or "(none)"
            positions = root.findtext("StoryArcNumber") or "(none)"
            evidence = f"Names: {names}\nPositions: {positions}"
        else:
            evidence = "\n".join(
                f"{(a.findtext('Name') or '(unnamed)')[:501]} "
                f"(position: {(a.findtext('Number') or 'not set')[:20]})"
                for a in root.findall("Arcs/Arc")[:129]
            )
        return evidence[:4096] + (" [truncated]" if len(evidence) > 4096 else "")


def _replace_list(mi: ET.Element, path: str, values: tuple[str, ...]) -> None:
    parent_tag, child_tag = path.split("/")
    parent = mi.find(parent_tag)
    assert parent is not None
    old = parent.findall(child_tag)
    matches_by_label: defaultdict[str, list[ET.Element]] = defaultdict(list)
    for node in old:
        matches_by_label[(node.text or "").strip()].append(node)
    counts = Counter(values)
    for label, matches in matches_by_label.items():
        if (
            counts[label]
            and (len(matches) != 1 or counts[label] != 1)
            and any("id" in node.attrib for node in matches)
        ):
            raise ArchiveMetadataRenderError("ambiguous_list_id", parent_tag)
        if any(len(node) for node in matches):
            raise ArchiveMetadataRenderError("unsupported_content", parent_tag)
    for node in old:
        parent.remove(node)
    for value in values:
        matches = matches_by_label[value]
        node = deepcopy(matches[0]) if len(matches) == 1 else ET.Element(child_tag)
        node.text = value
        parent.append(node)


def _choose_arcs(ci: ET.Element, mi: ET.Element, source: str) -> None:
    left, right = _arc_values(ci, mi)
    selected = left if source == "ComicInfo.xml" else right
    _set_arc_fields(ci, mi, selected, replace_metron=source == "ComicInfo.xml")


def _set_arc_fields(
    ci: ET.Element,
    mi: ET.Element,
    selected: tuple[tuple[str, str | None], ...],
    *,
    replace_metron: bool = True,
) -> None:
    set_text(ci, "StoryArc", ", ".join(name for name, _ in selected))
    set_text(
        ci,
        "StoryArcNumber",
        ", ".join(number or "" for _, number in selected)
        if all(number for _, number in selected)
        else None,
    )
    if replace_metron:
        parent = mi.find("Arcs")
        if parent is None:
            if not selected:
                return
            parent = ET.SubElement(mi, "Arcs")
        old = parent.findall("Arc")
        by_name: defaultdict[str, list[ET.Element]] = defaultdict(list)
        counts = Counter(name for name, _ in selected)
        for node in old:
            name = node.findtext("Name") or ""
            by_name[name].append(node)
            if any(child.tag not in {"Name", "Number"} or len(child) for child in node):
                raise ArchiveMetadataRenderError("unsupported_content", "StoryArc")
            parent.remove(node)
        for name, number in selected:
            matches = by_name[name]
            if (len(matches) > 1 or counts[name] > 1) and any("id" in m.attrib for m in matches):
                raise ArchiveMetadataRenderError("ambiguous_list_id", "Arcs")
            arc = deepcopy(matches[0]) if len(matches) == 1 else ET.Element("Arc")
            set_text(arc, "Name", name)
            set_text(arc, "Number", number)
            parent.append(arc)
        if not len(parent) and not parent.attrib:
            mi.remove(parent)


def apply_reviewed_arcs(ci: ET.Element, mi: ET.Element, entries: tuple[IssueArcEntry, ...]) -> None:
    _set_arc_fields(
        ci,
        mi,
        tuple((e.name, str(e.position) if e.position is not None else None) for e in entries),
    )


def _resolve_shared_fields(
    ci: ET.Element, mi: ET.Element, choices: SharedXmlChoices, *, editor: bool = False
) -> list[FileMetadataConflict]:
    _normalize_rating(ci, mi)
    conflicts: list[FileMetadataConflict] = []

    def review(field: str, left: str, right: str) -> str | None:
        key = cast("SharedXmlField", f"xml.{field}")
        selected = choices.get(key)
        if selected is not None and selected not in {"ComicInfo.xml", "MetronInfo.xml"}:
            raise ArchiveMetadataRenderError("invalid_choice", field)
        left_selectable = field != "AgeRating" or left in _AGE_RATINGS
        if selected == "ComicInfo.xml" and not left_selectable:
            raise ArchiveMetadataRenderError("invalid_choice", field)
        conflicts.append(
            FileMetadataConflict(
                key=key,
                label=SHARED_FIELD_LABELS[field].capitalize(),
                selected=selected,
                options=[
                    FileMetadataOption(
                        source="ComicInfo.xml", value=left, selectable=left_selectable
                    ),
                    FileMetadataOption(source="MetronInfo.xml", value=right),
                ],
                note=(
                    "This ComicInfo age rating is not supported by MetronInfo. "
                    "Choose the MetronInfo value or correct the source rating before retrying."
                )
                if not left_selectable
                else (
                    "Choose the embedded values to keep in both XML documents. "
                    "This does not change the library catalog."
                ),
            )
        )
        return selected

    for field, path in _LIST_FIELDS:
        left = _parts(ci.findtext(field))
        right = tuple((n.text or "").strip() for n in mi.findall(path))
        if any(not value or "," in value for value in right):
            key = cast("SharedXmlField", f"xml.{field}")
            if key not in choices:
                label = SHARED_FIELD_LABELS[field]
                conflicts.append(
                    FileMetadataConflict(
                        key=key,
                        label=label.capitalize(),
                        options=[],
                        note=f"These Metron {label} cannot be copied directly to ComicInfo. Open "
                        f"Edit metadata to enter the {label} to keep, one per line, or clear them.",
                    )
                )
                continue
            raise ArchiveMetadataRenderError("unsupported_content", field)
        if left and right and left != right:
            selected = review(field, ", ".join(left), ", ".join(right))
            if selected == "ComicInfo.xml":
                _replace_list(mi, path, left)
            elif selected == "MetronInfo.xml":
                set_text(ci, field, ", ".join(right))
    for field, path in _SCALAR_FIELDS:
        left_text, right_text = ci.findtext(field), mi.findtext(path)
        if field == "AgeRating" and left_text and left_text not in _AGE_RATINGS and not right_text:
            if "xml.AgeRating" in choices:
                raise ArchiveMetadataRenderError("invalid_choice", field)
            conflicts.append(
                FileMetadataConflict(
                    key="xml.AgeRating",
                    label="Age rating",
                    options=[
                        FileMetadataOption(
                            source="ComicInfo.xml", value=left_text, selectable=False
                        )
                    ],
                    note="This ComicInfo rating cannot be copied to MetronInfo. Open Edit metadata "
                    "on the issue and choose a shared rating or Clear value.",
                )
            )
            continue
        if left_text and right_text and left_text != right_text:
            selected = review(field, left_text, right_text)
            if selected == "ComicInfo.xml":
                if field == "Imprint":
                    set_reviewed_imprint(mi, left_text)
                    continue
                parent = mi.find(path.rsplit("/", 1)[0]) if "/" in path else mi
                node = mi.find(path)
                assert parent is not None and node is not None
                # A new value cannot inherit an unrelated embedded resource ID.
                parent.remove(node)
                ET.SubElement(parent, field).text = left_text
            elif selected == "MetronInfo.xml":
                set_text(ci, field, right_text)
    try:
        left_arcs, right_arcs = _arc_values(ci, mi)
    except ArchiveMetadataRenderError:
        if not editor:
            raise
        conflicts.append(
            FileMetadataConflict(
                key="xml.StoryArc",
                label="Embedded story arcs",
                options=[],
                note="Review arc names and their positions, or explicitly clear the embedded arcs.",
            )
        )
        left_arcs, right_arcs = (), ()
    if (
        left_arcs
        and right_arcs
        and (
            tuple(name for name, _ in left_arcs) != tuple(name for name, _ in right_arcs)
            or any(
                a is not None and b is not None and a != b
                for (_, a), (_, b) in zip(left_arcs, right_arcs, strict=True)
            )
        )
    ):

        def display(arcs: tuple[tuple[str, str | None], ...]) -> str:
            return "\n".join(
                f"{name} (position {number})" if number else name for name, number in arcs
            )

        selected = review("StoryArc", display(left_arcs), display(right_arcs))
        if selected is not None:
            _choose_arcs(ci, mi, selected)
    if set(choices) - {c.key for c in conflicts}:
        raise ArchiveMetadataRenderError("choices_changed")
    return conflicts


def coordinate_preserved_fields(
    ci: ET.Element, mi: ET.Element, choices: SharedXmlChoices | None = None
) -> None:
    """Build shared values from agreeing existing evidence, never attach identities."""
    for conflict in _resolve_shared_fields(ci, mi, choices or {}):
        if conflict.selected is None:
            raise ArchiveMetadataRenderError("unreconciled_field", conflict.key.split(".")[1])
    for ci_tag, mi_path in _LIST_FIELDS:
        left = _parts(ci.findtext(ci_tag))
        right = tuple((node.text or "").strip() for node in mi.findall(mi_path))
        if any(not value or "," in value for value in right):
            raise ArchiveMetadataRenderError("unsupported_content", ci_tag)
        if left and right and left != right:
            raise ArchiveMetadataRenderError("unreconciled_field", ci_tag)
        values = right or left
        if values:
            set_text(ci, ci_tag, ", ".join(values))
            if not right:
                parent_tag, child_tag = mi_path.split("/")
                parent = mi.find(parent_tag)
                if parent is None:
                    parent = ET.SubElement(mi, parent_tag)
                for value in values:
                    ET.SubElement(parent, child_tag).text = value
    for ci_tag, mi_path in _SCALAR_FIELDS:
        left_text, right_text = ci.findtext(ci_tag), mi.findtext(mi_path)
        if left_text and right_text and left_text != right_text:
            raise ArchiveMetadataRenderError("unreconciled_field", ci_tag)
        scalar = right_text or left_text
        if scalar:
            set_text(ci, ci_tag, scalar)
            if not right_text:
                if "/" in mi_path:
                    parent_tag, child_tag = mi_path.split("/")
                    parent = mi.find(parent_tag)
                    if parent is None:
                        raise ArchiveMetadataRenderError("publisher_required", ci_tag)
                    set_text(parent, child_tag, scalar)
                else:
                    set_text(mi, mi_path, scalar)
    _arcs(ci, mi)


def _arcs(ci: ET.Element, mi: ET.Element) -> None:
    names = _parts(ci.findtext("StoryArc"))
    numbers = _parts(ci.findtext("StoryArcNumber"))
    if numbers and (
        len(numbers) != len(names)
        or any(len(n) > 10 or not n.isascii() or not n.isdecimal() or int(n) < 1 for n in numbers)
    ):
        raise ArchiveMetadataRenderError("unsupported_content", "StoryArcNumber")
    arcs = mi.findall("Arcs/Arc")
    right_names = tuple((arc.findtext("Name") or "").strip() for arc in arcs)
    right_numbers = tuple(arc.findtext("Number") for arc in arcs)
    if any(not name or "," in name for name in right_names):
        raise ArchiveMetadataRenderError("unsupported_content", "StoryArc")
    if names and right_names and names != right_names:
        raise ArchiveMetadataRenderError("unreconciled_field", "StoryArc")
    if (
        numbers
        and right_numbers
        and any(
            right is not None and int(left) != int(right)
            for left, right in zip(numbers, right_numbers, strict=True)
        )
    ):
        raise ArchiveMetadataRenderError("unreconciled_field", "StoryArcNumber")
    if right_names:
        set_text(ci, "StoryArc", ", ".join(right_names))
        if numbers:
            for arc, number in zip(arcs, numbers, strict=True):
                set_text(arc, "Number", int(number))
        elif all(right_numbers):
            set_text(
                ci,
                "StoryArcNumber",
                ", ".join(str(int(number)) for number in right_numbers if number is not None),
            )
    elif names:
        parent = mi.find("Arcs")
        if parent is None:
            parent = ET.SubElement(mi, "Arcs")
        for index, name in enumerate(names):
            arc = ET.SubElement(parent, "Arc")
            ET.SubElement(arc, "Name").text = name
            if numbers:
                ET.SubElement(arc, "Number").text = str(int(numbers[index]))
