"""Preserve schema-specific values while coordinating shared archive metadata."""

from xml.etree import ElementTree as ET

from pullbox.core.archive_metadata import ArchiveMetadataFiles
from pullbox.core.archive_metadata_fields import normalize_comicinfo_aliases
from pullbox.core.metadata_xml import MetadataXmlError, parse_metadata_xml
from pullbox.core.metroninfo_schema import validate_metroninfo_xml

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


def coordinate_preserved_fields(ci: ET.Element, mi: ET.Element) -> None:
    """Build shared values from agreeing existing evidence, never attach identities."""
    for ci_tag, mi_path in (
        ("Genre", "Genres/Genre"),
        ("Tags", "Tags/Tag"),
        ("Characters", "Characters/Character"),
        ("Teams", "Teams/Team"),
        ("Locations", "Locations/Location"),
    ):
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
    for ci_tag, mi_path in (("Imprint", "Publisher/Imprint"), ("AgeRating", "AgeRating")):
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
                        raise ArchiveMetadataRenderError("unreconciled_field", ci_tag)
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
