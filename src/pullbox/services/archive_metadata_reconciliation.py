"""Compare embedded evidence without selecting identities or mutating archives."""

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit
from xml.etree import ElementTree

from pydantic import ValidationError

from pullbox.core.archive_metadata import ArchiveMetadataFiles, MetadataFile
from pullbox.core.archive_metadata_fields import (
    COMICINFO_ROLES,
    canonical_archive_format,
    comicinfo_credits,
    normalize_comicinfo_aliases,
)
from pullbox.core.comicinfo_sanitizer import references_stale_retailer
from pullbox.core.issue_numbers import normalize_issue_number_text
from pullbox.core.metadata_identity import (
    ExactIdentityConflict,
    ExactIdentityEvidence,
    ExternalIdentityRef,
    IdentityEvidenceKind,
    IdentityNamespace,
    MetadataEntityKind,
    find_exact_identity_conflicts,
)
from pullbox.core.metadata_xml import MetadataXmlError, parse_metadata_xml
from pullbox.core.metroninfo import MetronInfoData, parse_metroninfo
from pullbox.schemas.metadata_credits import MetadataCredit
from pullbox.schemas.metadata_snapshot import MetadataValues

_CI_ROLES = COMICINFO_ROLES
_CV_HOSTS = frozenset(
    {"comicvine.gamespot.com", "www.comicvine.gamespot.com", "comicvine.com", "www.comicvine.com"}
)
_CV_NOTES = (
    (re.compile(r"\[(?:cv_vol_id|cvid):([^\]]*)\]", re.IGNORECASE), MetadataEntityKind.SERIES),
    (re.compile(r"\[cv_issue_id:([^\]]*)\]", re.IGNORECASE), MetadataEntityKind.ISSUE),
)


@dataclass(frozen=True)
class ArchiveMetadataDiagnostic:
    document: str
    code: str
    locator: str


@dataclass(frozen=True)
class EmbeddedMetadataValues:
    series: MetadataValues = field(default_factory=MetadataValues)
    issue: MetadataValues = field(default_factory=MetadataValues)
    evidence: tuple[ExactIdentityEvidence, ...] = ()
    diagnostics: tuple[ArchiveMetadataDiagnostic, ...] = ()
    publication_date_parts: tuple[int | None, int | None, int | None] = (None, None, None)
    metron: MetronInfoData | None = None


@dataclass(frozen=True)
class ArchiveMetadataDifference:
    entity: str
    field: str
    comicinfo: object
    metroninfo: object


@dataclass(frozen=True)
class ArchiveMetadataReconciliation:
    files: ArchiveMetadataFiles
    comicinfo: EmbeddedMetadataValues = field(default_factory=EmbeddedMetadataValues)
    metroninfo: EmbeddedMetadataValues = field(default_factory=EmbeddedMetadataValues)
    series: MetadataValues = field(default_factory=MetadataValues)
    issue: MetadataValues = field(default_factory=MetadataValues)
    evidence: tuple[ExactIdentityEvidence, ...] = ()
    identity_conflicts: tuple[ExactIdentityConflict, ...] = ()
    differences: tuple[ArchiveMetadataDifference, ...] = ()
    diagnostics: tuple[ArchiveMetadataDiagnostic, ...] = ()

    @property
    def requires_review(self) -> bool:
        return bool(self.identity_conflicts or self.differences or self.diagnostics)


def reconcile_archive_metadata(files: ArchiveMetadataFiles) -> ArchiveMetadataReconciliation:
    """Return shared values and explicit disagreements, never authority or write permission."""
    metron = _read_document(files.metroninfo, "MetronInfo.xml")
    comic = _read_document(files.comicinfo, "ComicInfo.xml", metron.issue.credits)
    differences: list[ArchiveMetadataDifference] = []
    series = _shared_values(comic.series, metron.series, "series", differences)
    issue = _shared_values(comic.issue, metron.issue, "issue", differences)
    if metron.issue.cover_date is not None:
        incoming = metron.issue.cover_date
        parts = (incoming.year, incoming.month, incoming.day)
        if any(
            left is not None and left != right
            for left, right in zip(comic.publication_date_parts, parts, strict=True)
        ):
            if not any(item.field == "cover_date" for item in differences):
                differences.append(
                    ArchiveMetadataDifference(
                        "issue", "cover_date", comic.publication_date_parts, incoming
                    )
                )
            issue = issue.model_copy(update={"cover_date": None})
    evidence = tuple(dict.fromkeys((*comic.evidence, *metron.evidence)))
    return ArchiveMetadataReconciliation(
        files,
        comic,
        metron,
        series,
        issue,
        evidence,
        find_exact_identity_conflicts(evidence),
        tuple(differences),
        (*comic.diagnostics, *metron.diagnostics),
    )


def _shared_values(
    comic: MetadataValues,
    metron: MetadataValues,
    entity: str,
    differences: list[ArchiveMetadataDifference],
) -> MetadataValues:
    values: dict[str, object] = {}
    for name in MetadataValues.model_fields:
        left, right = getattr(comic, name), getattr(metron, name)
        if (
            name == "credits"
            and comic.credits is not None
            and metron.credits is not None
            and comic.credits == comicinfo_credits(metron.credits)
        ):
            values[name] = metron.credits
            continue
        if left is not None and right is not None and left != right:
            differences.append(ArchiveMetadataDifference(entity, name, left, right))
        else:
            values[name] = left if left is not None else right
    return MetadataValues.model_validate(values)


def _values(
    fields: dict[str, tuple[object, str]],
    document: str,
    diagnostics: list[ArchiveMetadataDiagnostic],
) -> MetadataValues:
    values: dict[str, object] = {}
    for name, (value, locator) in fields.items():
        if value is None:
            continue
        try:
            if name == "issue_number_text" and isinstance(value, str):
                value = normalize_issue_number_text(value)
            elif name == "series_type" and isinstance(value, str):
                value = canonical_archive_format(value)
            normalized = MetadataValues.model_validate({name: value})
        except (ValidationError, ValueError):
            diagnostics.append(ArchiveMetadataDiagnostic(document, "invalid_field", locator))
        else:
            values[name] = getattr(normalized, name)
    return MetadataValues.model_validate(values)


def _read_document(
    member: MetadataFile,
    document: str,
    structured_credits: tuple[MetadataCredit, ...] | None = None,
) -> EmbeddedMetadataValues:
    diagnostics = [
        ArchiveMetadataDiagnostic(document, item.value, document) for item in member.diagnostics
    ]
    if member.entry_count > 1 and not any(item.code == "duplicate_entries" for item in diagnostics):
        diagnostics.append(ArchiveMetadataDiagnostic(document, "duplicate_entries", document))
    if member.payload is None:
        if member.entry_count and not diagnostics:
            diagnostics.append(ArchiveMetadataDiagnostic(document, "unreadable", document))
        return EmbeddedMetadataValues(diagnostics=tuple(diagnostics))
    if document == "MetronInfo.xml":
        return _metron_values(parse_metroninfo(member.payload), diagnostics)
    try:
        root = parse_metadata_xml(member.payload, root_name="ComicInfo")
    except MetadataXmlError as exc:
        diagnostics.append(ArchiveMetadataDiagnostic(document, exc.code, "ComicInfo"))
        return EmbeddedMetadataValues(diagnostics=tuple(diagnostics))
    normalize_comicinfo_aliases(root)
    return _ComicInfoReader(root, diagnostics, structured_credits).read()


def _metron_values(
    data: MetronInfoData, diagnostics: list[ArchiveMetadataDiagnostic]
) -> EmbeddedMetadataValues:
    document = "MetronInfo.xml"
    diagnostics.extend(
        ArchiveMetadataDiagnostic(document, item.code.value, item.locator)
        for item in data.diagnostics
    )
    series = _values(
        {
            "title": (data.series, "Series/Name"),
            "sort_title": (data.sort_name, "Series/SortName"),
            "publisher": (data.publisher, "Publisher/Name"),
            "language": (data.language, "Series/@lang"),
            "year_start": (data.start_year, "Series/StartYear"),
            "volume": (str(data.volume) if data.volume is not None else None, "Series/Volume"),
            "series_type": (data.series_format, "Series/Format"),
            "issue_count": (data.issue_count, "Series/IssueCount"),
        },
        document,
        diagnostics,
    )
    credits = None
    if data.credits is not None and not any(
        item.locator.startswith("Credits") for item in data.diagnostics
    ):
        credits = tuple(
            {"name": item.creator.value, "role": ", ".join(role.value for role in item.roles)}
            for item in data.credits
        )
    issue = _values(
        {
            "issue_number_text": (data.number, "Number"),
            "title": (data.stories[0] if len(data.stories) == 1 else None, "Stories"),
            "description": (data.summary, "Summary"),
            "page_count": (data.page_count, "PageCount"),
            "cover_date": (data.cover_date, "CoverDate"),
            "store_date": (data.store_date, "StoreDate"),
            "credits": (credits, "Credits"),
        },
        document,
        diagnostics,
    )
    return EmbeddedMetadataValues(series, issue, data.evidence, tuple(diagnostics), metron=data)


class _ComicInfoReader:
    def __init__(
        self,
        root: ElementTree.Element,
        diagnostics: list[ArchiveMetadataDiagnostic],
        structured_credits: tuple[MetadataCredit, ...] | None = None,
    ) -> None:
        self.root = root
        self.diagnostics = diagnostics
        self.seen = {root}
        self.evidence: list[ExactIdentityEvidence] = []
        self.structured_credits = structured_credits

    def credit_names(self, value: str, role: str) -> list[str]:
        parts = [part.strip() for part in value.split(",")]
        names = [
            credit.name
            for credit in self.structured_credits or ()
            if role in credit.role.split(", ")
        ]
        # ComicInfo has no name boundaries. Match complete paired names per role,
        # longest first (e.g. John and John, Jr.), never unordered name fragments.
        remaining = sorted(
            ([part.strip() for part in name.split(",")] for name in names),
            key=len,
            reverse=True,
        )
        offset = 0
        while remaining:
            match = next(
                (name for name in remaining if parts[offset : offset + len(name)] == name), None
            )
            if match is None:
                return parts
            offset += len(match)
            remaining.remove(match)
        return names if names and offset == len(parts) else parts

    def diagnostic(self, code: str, locator: str) -> None:
        item = ArchiveMetadataDiagnostic("ComicInfo.xml", code, locator)
        if item not in self.diagnostics:
            self.diagnostics.append(item)

    def read_field(self, tag: str) -> str | None:
        nodes = self.root.findall(tag)
        self.seen.update(nodes)
        if len(nodes) > 1:
            self.diagnostic("ambiguous_field", tag)
            return None
        if not nodes:
            return None
        node = nodes[0]
        value = (node.text or "").strip()
        if len(node) or len(value) > (200000 if tag == "Summary" else 4096):
            self.diagnostic("invalid_field", tag)
            return None
        return value or None

    def integer(self, tag: str, minimum: int, maximum: int) -> int | None:
        value = self.read_field(tag)
        if value is None:
            return None
        if re.fullmatch(r"\+?[0-9]{1,10}", value) and minimum <= int(value) <= maximum:
            return int(value)
        self.diagnostic("invalid_field", tag)
        return None

    def identity(self, kind: MetadataEntityKind, value: str, locator: str) -> None:
        try:
            identity = ExternalIdentityRef(IdentityNamespace.COMICVINE, kind, value)
        except ValueError:
            self.diagnostic("invalid_id", locator)
        else:
            self.evidence.append(
                ExactIdentityEvidence(identity, IdentityEvidenceKind.COMICINFO_XML)
            )

    def identities(self) -> None:
        # Inspect every explicit assertion even when a duplicate field is ambiguous.
        for tag in ("Notes", "Web"):
            self.read_field(tag)
            for node in self.root.findall(tag):
                value = (node.text or "").strip()
                if len(node) or len(value) > 4096 or references_stale_retailer(value):
                    continue
                if tag == "Notes":
                    for pattern, kind in _CV_NOTES:
                        for match in pattern.finditer(value):
                            self.identity(kind, match[1], tag)
                else:
                    for raw in value.split():
                        self.url_identity(raw)

    def url_identity(self, value: str) -> None:
        try:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or url.hostname not in _CV_HOSTS
                or url.username is not None
                or url.password is not None
                or url.port not in {None, 80, 443}
            ):
                return
        except ValueError:
            return
        match = re.fullmatch(
            r"/(?:(?!(?:4050|4000)-[0-9]+/)[^/]+/)?(4050|4000)-([0-9]{1,255})/?", url.path
        )
        if match:
            self.identity(
                MetadataEntityKind.SERIES if match[1] == "4050" else MetadataEntityKind.ISSUE,
                match[2],
                "Web",
            )

    def read(self) -> EmbeddedMetadataValues:
        document = "ComicInfo.xml"
        series = _values(
            {
                "title": (self.read_field("Series"), "Series"),
                "publisher": (self.read_field("Publisher"), "Publisher"),
                "language": (self.read_field("LanguageISO"), "LanguageISO"),
                "series_type": (self.read_field("Format"), "Format"),
                "issue_count": (self.integer("Count", 0, 1000000), "Count"),
            },
            document,
            self.diagnostics,
        )
        year = self.integer("Year", 1, 9999)
        month = self.integer("Month", 1, 12)
        day = self.integer("Day", 1, 31)
        cover_date = None
        if year is not None and month is not None and day is not None:
            try:
                cover_date = date(year, month, day)
            except ValueError:
                self.diagnostic("invalid_field", "CoverDate")
        credits: list[dict[str, str]] = []
        credits_present = False
        for tag, role in _CI_ROLES.items():
            credits_present = credits_present or bool(self.root.findall(tag))
            value = self.read_field(tag)
            if value:
                credits.extend(
                    {"name": name, "role": role} for name in self.credit_names(value, role)
                )
        invalid_credits = any(item.locator in _CI_ROLES for item in self.diagnostics)
        issue = _values(
            {
                "issue_number_text": (self.read_field("Number"), "Number"),
                "title": (self.read_field("Title"), "Title"),
                "description": (self.read_field("Summary"), "Summary"),
                "page_count": (self.integer("PageCount", 0, 1000000), "PageCount"),
                "cover_date": (cover_date, "CoverDate"),
                "credits": (
                    tuple(credits) if credits_present and not invalid_credits else None,
                    "Credits",
                ),
            },
            document,
            self.diagnostics,
        )
        self.identities()
        schema_hint = "{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation"
        if any(
            node not in self.seen
            or (set(node.attrib) - ({schema_hint} if node is self.root else set()))
            or (node is self.root and (node.text or "").strip())
            or (node.tail or "").strip()
            for node in self.root.iter()
        ):
            self.diagnostic("unmapped_content", "ComicInfo")
        return EmbeddedMetadataValues(
            series,
            issue,
            tuple(dict.fromkeys(self.evidence)),
            tuple(self.diagnostics),
            (year, month, day),
        )
