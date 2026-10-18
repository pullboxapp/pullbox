"""Bounded, read-only MetronInfo evidence; never a matching or write decision."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING

from pullbox.core.metadata_identity import (
    ExactIdentityEvidence,
    ExternalIdentityRef,
    IdentityEvidenceKind,
    IdentityNamespace,
    MetadataEntityKind,
    find_exact_identity_conflicts,
)
from pullbox.core.metadata_xml import (
    MAX_METADATA_XML_BYTES,
    MAX_METADATA_XML_DEPTH,
    MAX_METADATA_XML_NODES,
    MetadataXmlError,
    parse_metadata_xml,
)

if TYPE_CHECKING:
    from xml.etree import ElementTree

MAX_METRONINFO_BYTES = MAX_METADATA_XML_BYTES
MAX_METRONINFO_DEPTH = MAX_METADATA_XML_DEPTH
MAX_METRONINFO_NODES = MAX_METADATA_XML_NODES
MAX_METRONINFO_CREDITS = 128
MAX_METRONINFO_ROLES = 32
_MAX_FIELD_LENGTH = 4096
_CALENDAR_TIMEZONE = r"(?:Z|[+-](?:(?:0[0-9]|1[0-3]):[0-5][0-9]|14:00))?"
_SCHEMA_HINT = "{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation"
_SOURCES = {
    "metron": IdentityNamespace.METRON,
    "comic vine": IdentityNamespace.COMICVINE,
    "comicvine": IdentityNamespace.COMICVINE,
    "grand comics database": IdentityNamespace.GCD,
    "gcd": IdentityNamespace.GCD,
    "league of comic geeks": IdentityNamespace.LOCG,
    "locg": IdentityNamespace.LOCG,
}


class MetronInfoDiagnosticCode(enum.StrEnum):
    INVALID_XML = "invalid_xml"
    UNSAFE_XML = "unsafe_xml"
    TOO_LARGE = "too_large"
    COMPLEXITY_LIMIT = "complexity_limit"
    INVALID_ROOT = "invalid_root"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    INVALID_ID = "invalid_id"
    UNKNOWN_SOURCE = "unknown_source"
    INVALID_PRIMARY = "invalid_primary"
    MISSING_PRIMARY = "missing_primary"
    AMBIGUOUS_PRIMARY = "ambiguous_primary"
    INVALID_FIELD = "invalid_field"
    AMBIGUOUS_FIELD = "ambiguous_field"
    EXACT_ID_CONFLICT = "exact_id_conflict"
    UNMAPPED_CONTENT = "unmapped_content"


@dataclass(frozen=True)
class MetronInfoDiagnostic:
    code: MetronInfoDiagnosticCode
    locator: str


@dataclass(frozen=True)
class MetronInfoIdentity:
    evidence: ExactIdentityEvidence
    primary: bool = False


@dataclass(frozen=True)
class MetronInfoReleaseReference:
    """LOCG issue/variant reference, not a canonical issue attachment."""

    external_id: str
    primary: bool = False


@dataclass(frozen=True)
class MetronInfoArc:
    name: str
    number: int | None = None
    evidence: ExactIdentityEvidence | None = None


@dataclass(frozen=True)
class MetronInfoResource:
    """Descriptive resource with an opaque local ID, never an identity claim."""

    value: str
    resource_id: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class MetronInfoCredit:
    creator: MetronInfoResource
    roles: tuple[MetronInfoResource, ...] = ()


@dataclass(frozen=True)
class MetronInfoData:
    series: str | None = None
    number: str | None = None
    alternative_number: str | None = None
    publisher: str | None = None
    start_year: int | None = None
    volume: int | None = None
    series_format: str | None = None
    issue_count: int | None = None
    cover_date: date | None = None
    store_date: date | None = None
    stories: tuple[str, ...] = ()
    arcs: tuple[MetronInfoArc, ...] = ()
    primary_source: IdentityNamespace | None = None
    identities: tuple[MetronInfoIdentity, ...] = ()
    release_references: tuple[MetronInfoReleaseReference, ...] = ()
    diagnostics: tuple[MetronInfoDiagnostic, ...] = ()
    has_unmapped_content: bool = False
    sort_name: str | None = None
    language: str | None = None
    volume_count: int | None = None
    publisher_id: str | None = None
    imprint: MetronInfoResource | None = None
    alternative_names: tuple[MetronInfoResource, ...] = ()
    collection_title: str | None = None
    manga_volume: str | None = None
    summary: str | None = None
    notes: str | None = None
    page_count: int | None = None
    age_rating: str | None = None
    story_resources: tuple[MetronInfoResource, ...] = ()
    genres: tuple[MetronInfoResource, ...] = ()
    tags: tuple[MetronInfoResource, ...] = ()
    characters: tuple[MetronInfoResource, ...] = ()
    teams: tuple[MetronInfoResource, ...] = ()
    locations: tuple[MetronInfoResource, ...] = ()
    reprints: tuple[MetronInfoResource, ...] = ()
    credits: tuple[MetronInfoCredit, ...] | None = None

    @property
    def evidence(self) -> tuple[ExactIdentityEvidence, ...]:
        return tuple(item.evidence for item in self.identities)


def parse_metroninfo(payload: bytes | str) -> MetronInfoData:
    """Read local metadata only; callers retain authority and archive safety checks."""
    try:
        root = parse_metadata_xml(payload, root_name="MetronInfo")
    except MetadataXmlError as exc:
        return MetronInfoData(
            diagnostics=(MetronInfoDiagnostic(MetronInfoDiagnosticCode(exc.code), "MetronInfo"),)
        )
    return _Reader(root).read()


class _Reader:
    def __init__(self, root: ElementTree.Element) -> None:
        self.root = root
        self.diagnostics: list[MetronInfoDiagnostic] = []
        self.diagnostic_keys: set[MetronInfoDiagnostic] = set()
        self.seen: dict[ElementTree.Element, set[str]] = {root: {"version", _SCHEMA_HINT}}
        self.text_nodes: set[ElementTree.Element] = set()
        self.identities: dict[ExactIdentityEvidence, bool] = {}
        self.releases: dict[str, bool] = {}

    def diagnostic(self, code: MetronInfoDiagnosticCode, locator: str) -> None:
        item = MetronInfoDiagnostic(code, locator)
        if item not in self.diagnostic_keys:
            self.diagnostic_keys.add(item)
            self.diagnostics.append(item)

    def single(
        self, parent: ElementTree.Element | None, tag: str, locator: str
    ) -> ElementTree.Element | None:
        if parent is None:
            return None
        nodes = parent.findall(tag)
        if len(nodes) > 1:
            self.diagnostic(MetronInfoDiagnosticCode.AMBIGUOUS_FIELD, locator)
            return None
        if not nodes:
            return None
        self.seen.setdefault(nodes[0], set())
        return nodes[0]

    def read_text(self, node: ElementTree.Element | None, locator: str) -> str | None:
        if node is None:
            return None
        value = (node.text or "").strip()
        limit = 200000 if locator == "Summary" else _MAX_FIELD_LENGTH
        if len(node) or len(value) > limit:
            self.diagnostic(MetronInfoDiagnosticCode.INVALID_FIELD, locator)
            return None
        self.text_nodes.add(node)
        return value or None

    def field(self, parent: ElementTree.Element | None, tag: str, locator: str) -> str | None:
        return self.read_text(self.single(parent, tag, locator), locator)

    def attribute(self, node: ElementTree.Element | None, name: str, locator: str) -> str | None:
        if node is None or name not in node.attrib:
            return None
        value = node.attrib[name]
        if len(value) > _MAX_FIELD_LENGTH:
            self.diagnostic(MetronInfoDiagnosticCode.INVALID_FIELD, f"{locator}/@{name}")
            return None
        self.seen[node].add(name)
        return value

    def language(self, node: ElementTree.Element | None, locator: str) -> str | None:
        value = self.attribute(node, "lang", locator)
        if value is not None and not re.fullmatch(r"[a-z]{2}", value):
            self.diagnostic(MetronInfoDiagnosticCode.INVALID_FIELD, f"{locator}/@lang")
            return None
        return value

    def resource(
        self, node: ElementTree.Element | None, locator: str, *, localized: bool = False
    ) -> MetronInfoResource | None:
        if node is None:
            return None
        self.seen.setdefault(node, set())
        value = self.read_text(node, locator)
        resource_id = self.attribute(node, "id", locator)
        language = self.language(node, locator) if localized else None
        if value is None:
            self.diagnostic(MetronInfoDiagnosticCode.INVALID_FIELD, locator)
            return None
        return MetronInfoResource(value, resource_id, language)

    def resources(
        self,
        parent: ElementTree.Element | None,
        group_tag: str,
        tag: str,
        locator: str,
        *,
        localized: bool = False,
    ) -> tuple[MetronInfoResource, ...]:
        group = self.single(parent, group_tag, locator)
        if group is None:
            return ()
        result = []
        for index, node in enumerate(group.findall(tag), 1):
            item = self.resource(node, f"{locator}/{tag}[{index}]", localized=localized)
            if item is not None:
                result.append(item)
        return tuple(result)

    def read_credits(self) -> tuple[MetronInfoCredit, ...] | None:
        group = self.single(self.root, "Credits", "Credits")
        if group is None:
            return None
        diagnostic_count = len(self.diagnostics)
        nodes = group.findall("Credit")
        if len(nodes) > MAX_METRONINFO_CREDITS:
            self.diagnostic(MetronInfoDiagnosticCode.COMPLEXITY_LIMIT, "Credits")
            return ()
        credits = []
        for index, node in enumerate(nodes, 1):
            locator = f"Credits/Credit[{index}]"
            self.seen[node] = set()
            creator = self.resource(
                self.single(node, "Creator", f"{locator}/Creator"), f"{locator}/Creator"
            )
            if creator is None:
                self.diagnostic(MetronInfoDiagnosticCode.INVALID_FIELD, f"{locator}/Creator")
            roles = self.single(node, "Roles", f"{locator}/Roles")
            if roles is not None and len(roles.findall("Role")) > MAX_METRONINFO_ROLES:
                self.diagnostic(MetronInfoDiagnosticCode.COMPLEXITY_LIMIT, f"{locator}/Roles")
                return ()
            values = self.resources(node, "Roles", "Role", f"{locator}/Roles")
            if creator is not None:
                credits.append(MetronInfoCredit(creator, values))
        return tuple(credits) if len(self.diagnostics) == diagnostic_count else ()

    def integer(
        self,
        parent: ElementTree.Element | None,
        tag: str,
        locator: str,
        minimum: int = 0,
        maximum: int = 2**31 - 1,
    ) -> int | None:
        value = self.field(parent, tag, locator)
        if value is None:
            return None
        if re.fullmatch(r"\+?[0-9]{1,10}", value) and minimum <= int(value) <= maximum:
            return int(value)
        self.diagnostic(MetronInfoDiagnosticCode.INVALID_FIELD, locator)
        return None

    def year(self, parent: ElementTree.Element | None, tag: str, locator: str) -> int | None:
        value = self.field(parent, tag, locator)
        if value is None:
            return None
        match = re.fullmatch(rf"([0-9]{{4}}){_CALENDAR_TIMEZONE}", value)
        if match and int(match[1]) > 0:
            return int(match[1])
        self.diagnostic(MetronInfoDiagnosticCode.INVALID_FIELD, locator)
        return None

    def date(self, tag: str) -> date | None:
        value = self.field(self.root, tag, tag)
        if value is None:
            return None
        try:
            match = re.fullmatch(rf"([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}){_CALENDAR_TIMEZONE}", value)
            if match:
                return date.fromisoformat(match[1])
        except ValueError:
            pass
        self.diagnostic(MetronInfoDiagnosticCode.INVALID_FIELD, tag)
        return None

    def external_id(self, value: str | None, locator: str) -> str | None:
        normalized = (value or "").strip()
        if (
            len(normalized) <= 255
            and normalized.isascii()
            and normalized.isdecimal()
            and normalized.lstrip("0")
        ):
            return normalized.lstrip("0")
        self.diagnostic(MetronInfoDiagnosticCode.INVALID_ID, locator)
        return None

    def claim(
        self, namespace: IdentityNamespace, kind: MetadataEntityKind, value: str
    ) -> ExactIdentityEvidence:
        return ExactIdentityEvidence(
            ExternalIdentityRef(namespace, kind, value), IdentityEvidenceKind.METRONINFO_XML
        )

    def read_ids(self) -> IdentityNamespace | None:
        declarations: list[IdentityNamespace | None] = []
        invalid_primary = False
        groups = self.root.findall("IDS")
        if len(groups) > 1:
            self.diagnostic(MetronInfoDiagnosticCode.AMBIGUOUS_FIELD, "IDS")
            invalid_primary = True
        for group_index, group in enumerate(groups, 1):
            self.seen[group] = set()
            for index, node in enumerate(group.findall("ID"), 1):
                locator = f"IDS[{group_index}]/ID[{index}]"
                self.seen[node] = {"source", "primary"}
                namespace = _SOURCES.get(" ".join(node.get("source", "").lower().split()))
                primary = node.get("primary", "false").strip()
                if primary not in {"true", "false", "1", "0"}:
                    invalid_primary = True
                    self.diagnostic(MetronInfoDiagnosticCode.INVALID_PRIMARY, locator)
                is_primary = primary in {"true", "1"}
                if is_primary:
                    declarations.append(namespace)
                if namespace is None:
                    self.diagnostic(MetronInfoDiagnosticCode.UNKNOWN_SOURCE, locator)
                    continue
                value = self.external_id(self.read_text(node, locator), locator)
                if value is None:
                    continue
                if namespace is IdentityNamespace.LOCG:
                    self.releases[value] = self.releases.get(value, False) or is_primary
                else:
                    claim = self.claim(namespace, MetadataEntityKind.ISSUE, value)
                    self.identities[claim] = self.identities.get(claim, False) or is_primary
        if len(declarations) > 1:
            self.diagnostic(MetronInfoDiagnosticCode.AMBIGUOUS_PRIMARY, "IDS")
        if invalid_primary or len(declarations) != 1:
            return None
        return declarations[0]

    def parent_identity(
        self,
        node: ElementTree.Element | None,
        namespace: IdentityNamespace | None,
        kind: MetadataEntityKind,
        locator: str,
    ) -> ExactIdentityEvidence | None:
        if node is None or "id" not in node.attrib:
            return None
        self.seen[node].add("id")
        if namespace is None:
            self.diagnostic(MetronInfoDiagnosticCode.MISSING_PRIMARY, locator)
            return None
        value = self.external_id(node.get("id"), locator)
        return self.claim(namespace, kind, value) if value is not None else None

    def read_arcs(self, primary: IdentityNamespace | None) -> tuple[MetronInfoArc, ...]:
        group = self.single(self.root, "Arcs", "Arcs")
        if group is None:
            return ()
        arcs = []
        for index, node in enumerate(group.findall("Arc"), 1):
            self.seen[node] = set()
            locator = f"Arcs/Arc[{index}]"
            name = self.field(node, "Name", f"{locator}/Name")
            number = self.integer(node, "Number", f"{locator}/Number", minimum=1)
            identity = self.parent_identity(node, primary, MetadataEntityKind.STORY_ARC, locator)
            if name:
                arcs.append(MetronInfoArc(name, number, identity))
        return tuple(arcs)

    def read(self) -> MetronInfoData:
        compatible = self.root.get("version") in {None, "1.0", "1.1"}
        if not compatible:
            self.diagnostic(MetronInfoDiagnosticCode.UNSUPPORTED_VERSION, "MetronInfo")
        primary = self.read_ids() if compatible else None
        series = self.single(self.root, "Series", "Series")
        parent = self.parent_identity(series, primary, MetadataEntityKind.SERIES, "Series")
        if parent is not None:
            self.identities[parent] = True
        if find_exact_identity_conflicts(self.identities):
            self.diagnostic(MetronInfoDiagnosticCode.EXACT_ID_CONFLICT, "IDS")
        stories = self.resources(self.root, "Stories", "Story", "Stories")
        publisher = self.single(self.root, "Publisher", "Publisher")
        data = MetronInfoData(
            series=self.field(series, "Name", "Series/Name"),
            number=self.field(self.root, "Number", "Number"),
            alternative_number=self.field(self.root, "AlternativeNumber", "AlternativeNumber"),
            publisher=self.field(publisher, "Name", "Publisher/Name"),
            start_year=self.year(series, "StartYear", "Series/StartYear"),
            volume=self.integer(series, "Volume", "Series/Volume"),
            series_format=self.field(series, "Format", "Series/Format"),
            issue_count=self.integer(series, "IssueCount", "Series/IssueCount", minimum=1),
            cover_date=self.date("CoverDate"),
            store_date=self.date("StoreDate"),
            stories=tuple(item.value for item in stories),
            arcs=self.read_arcs(primary),
            primary_source=primary,
            identities=tuple(
                MetronInfoIdentity(claim, flag) for claim, flag in self.identities.items()
            ),
            release_references=tuple(
                MetronInfoReleaseReference(value, flag) for value, flag in self.releases.items()
            ),
            sort_name=self.field(series, "SortName", "Series/SortName"),
            language=self.language(series, "Series"),
            volume_count=self.integer(series, "VolumeCount", "Series/VolumeCount", minimum=1),
            publisher_id=self.attribute(publisher, "id", "Publisher"),
            imprint=self.resource(
                self.single(publisher, "Imprint", "Publisher/Imprint"), "Publisher/Imprint"
            ),
            alternative_names=self.resources(
                series,
                "AlternativeNames",
                "AlternativeName",
                "Series/AlternativeNames",
                localized=True,
            ),
            collection_title=self.field(self.root, "CollectionTitle", "CollectionTitle"),
            manga_volume=self.field(self.root, "MangaVolume", "MangaVolume"),
            summary=self.field(self.root, "Summary", "Summary"),
            notes=self.field(self.root, "Notes", "Notes"),
            page_count=self.integer(self.root, "PageCount", "PageCount"),
            age_rating=self.field(self.root, "AgeRating", "AgeRating"),
            story_resources=stories,
            genres=self.resources(self.root, "Genres", "Genre", "Genres"),
            tags=self.resources(self.root, "Tags", "Tag", "Tags"),
            characters=self.resources(self.root, "Characters", "Character", "Characters"),
            teams=self.resources(self.root, "Teams", "Team", "Teams"),
            locations=self.resources(self.root, "Locations", "Location", "Locations"),
            reprints=self.resources(self.root, "Reprints", "Reprint", "Reprints"),
            credits=self.read_credits(),
        )
        unmapped = any(
            node not in self.seen
            or set(node.attrib) - self.seen[node]
            or (node not in self.text_nodes and (node.text or "").strip())
            or (node.tail or "").strip()
            for node in self.root.iter()
        )
        if unmapped:
            self.diagnostic(MetronInfoDiagnosticCode.UNMAPPED_CONTENT, "MetronInfo")
        return replace(data, diagnostics=tuple(self.diagnostics), has_unmapped_content=unmapped)
