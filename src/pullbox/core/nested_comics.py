"""Single-comic wrapper inspection and source-preserving normalization."""

import copy
import hashlib
import io
import re
import stat
import tempfile
import time
import unicodedata
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO, cast
from xml.etree import ElementTree

from defusedxml.common import DefusedXmlException

from pullbox.core.archive_format import ARCHIVE_SUFFIXES
from pullbox.core.comicinfo import parse_comicinfo
from pullbox.core.file_safety import DANGEROUS_EXTENSIONS, FileSafetyError
from pullbox.core.metadata_archive_source import MetadataArchiveSource, open_metadata_archive
from pullbox.core.page_sources.base import canonical_page_names
from pullbox.core.xml_security import parse_untrusted_xml

DEFAULT_LIMIT = 2000 * 1024 * 1024
_XML_LIMIT = 2 * 1024 * 1024
_ENTRY_LIMIT = 10_000
_PAGE_LIMIT = 128 * 1024 * 1024


class NestedComicError(ValueError):
    """A wrapper needs manual attention rather than automatic normalization."""


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.declared = 0
        self.entries = 0
        self.deadline = time.monotonic() + 120

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise NestedComicError("inspection_timeout")

    def validate(self, entries: list[zipfile.ZipInfo]) -> None:
        self.check()
        self.entries += len(entries)
        self.declared += sum(entry.file_size for entry in entries)
        if self.entries > _ENTRY_LIMIT or self.declared > self.limit:
            raise NestedComicError("resource_limit")
        names: set[str] = set()
        for entry in entries:
            name = entry.filename.replace("\\", "/")
            path = PurePosixPath(name)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (
                path.is_absolute()
                or PureWindowsPath(name).drive
                or ".." in path.parts
                or any(ord(char) < 32 for char in name)
                or len(name) > 1024
                or len(path.parts) > 32
                or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
                or path.suffix.casefold() in DANGEROUS_EXTENSIONS
            ):
                raise NestedComicError("unsafe_member")
            key = unicodedata.normalize("NFC", name).casefold().rstrip("/")
            if key in names:
                raise NestedComicError("duplicate_members")
            names.add(key)
            if entry.flag_bits & 1:
                raise NestedComicError("encrypted_archive")
            if entry.file_size < 0 or (
                entry.file_size > 4 * 1024 * 1024
                and entry.compress_size > 0
                and entry.file_size / entry.compress_size > 250
            ):
                raise NestedComicError("resource_limit")


def _copy_member(source: IO[bytes], target: IO[bytes], size: int, budget: _Budget) -> None:
    copied = 0
    while True:
        budget.check()
        chunk = source.read(min(1024 * 1024, size - copied + 1))
        if not chunk:
            break
        copied += len(chunk)
        if copied > size:
            raise NestedComicError("resource_limit")
        target.write(chunk)
    if copied != size:
        raise NestedComicError("incomplete_member")


def _xml(source: MetadataArchiveSource, budget: _Budget) -> bytes | None:
    entries = [
        entry
        for entry in source.entries
        if PurePosixPath(entry.filename.replace("\\", "/")).name.casefold() == "comicinfo.xml"
        and not entry.is_dir()
    ]
    if len(entries) > 1:
        raise NestedComicError("ambiguous_metadata")
    if not entries:
        return None
    entry = entries[0]
    if entry.file_size > _XML_LIMIT:
        raise NestedComicError("resource_limit")
    output = io.BytesIO()
    with source.open(entry) as stream:
        _copy_member(stream, output, entry.file_size, budget)
    payload = output.getvalue()
    root = parse_untrusted_xml(payload)
    if root.tag != "ComicInfo":
        raise NestedComicError("invalid_metadata")
    if any(len(root.findall(tag)) > 1 for tag in ("Series", "Number", "Volume", "Year", "Web")):
        raise NestedComicError("ambiguous_metadata")
    return payload


def _identity_value(tag: str, text: str) -> str:
    if tag in {"Number", "Volume", "Year"}:
        try:
            value = Decimal(text)
            if value.is_finite():
                return str(value.normalize())
        except InvalidOperation:
            pass
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _reconcile_xml(outer: bytes | None, inner: bytes | None) -> bytes | None:
    if outer is None or inner is None:
        return inner or outer
    outer_root, inner_root = parse_untrusted_xml(outer), parse_untrusted_xml(inner)
    for tag in ("Series", "Number", "Volume", "Year"):
        left, right = outer_root.findtext(tag), inner_root.findtext(tag)
        if left and right and _identity_value(tag, left) != _identity_value(tag, right):
            raise NestedComicError("metadata_conflict")
    # An exact provider identifier disagreement cannot be settled by title similarity.
    for pattern in (
        r"(?:4000-|cv_issue_id\s*:\s*)(\d+)",
        r"(?:4050-|cv_vol_id\s*:\s*)(\d+)",
    ):
        left_ids = set(re.findall(pattern, " ".join(outer_root.itertext()), re.IGNORECASE))
        right_ids = set(re.findall(pattern, " ".join(inner_root.itertext()), re.IGNORECASE))
        if (
            len(left_ids) > 1
            or len(right_ids) > 1
            or (left_ids and right_ids and left_ids != right_ids)
        ):
            raise NestedComicError("metadata_conflict")
    # Keep the inner document as the base; fill only absent outer fields.
    # Retaining the original wrapper preserves both documents for later review.
    merged = copy.deepcopy(inner_root)
    for child in outer_root:
        existing = merged.find(child.tag)
        if existing is None:
            merged.append(copy.deepcopy(child))
        elif not list(existing) and not (existing.text or "").strip():
            existing.text = child.text
    return cast("bytes", ElementTree.tostring(merged, encoding="utf-8", xml_declaration=True))


@contextmanager
def _open_nested(
    path: Path, report: dict[str, object], budget: _Budget, *, scratch_parent: Path | None = None
) -> Iterator[tuple[MetadataArchiveSource, bytes | None]]:
    with (
        path.open("rb") as stream,
        tempfile.TemporaryDirectory(prefix="pullbox-nested-", dir=scratch_parent) as scratch,
    ):
        if not stream.read(4).startswith(b"PK"):
            raise NestedComicError("unsupported_wrapper")
        stream.seek(0)
        parent = Path(scratch)
        with open_metadata_archive(
            stream,
            path,
            limit=budget.limit,
            scratch_parent=parent,
            validate=budget.validate,
            check_cancelled=budget.check,
            progress=lambda current, total: None,
        ) as outer:
            comics = [
                entry
                for entry in outer.entries
                if not entry.is_dir() and Path(entry.filename).suffix.casefold() in ARCHIVE_SUFFIXES
            ]
            report["inner_count"] = len(comics)
            if len(comics) != 1:
                raise NestedComicError("multiple_inner_comics")
            entry = comics[0]
            report["inner_name"] = entry.filename
            if any(
                not item.is_dir()
                and item != entry
                and PurePosixPath(item.filename).name.casefold() != "comicinfo.xml"
                for item in outer.entries
            ):
                raise NestedComicError("mixed_wrapper_content")
            if Path(entry.filename).suffix.casefold() not in {".cbz", ".cbr"}:
                raise NestedComicError("unsupported_inner_format")
            outer_xml = _xml(outer, budget)
            inner_path = parent / "inner.comic"
            with outer.open(entry) as incoming, inner_path.open("xb") as output:
                _copy_member(incoming, output, entry.file_size, budget)
            with inner_path.open("rb") as inner_stream:
                header = inner_stream.read(8)
                inner_stream.seek(0)
                if header.startswith(b"PK"):
                    report["inner_format"] = "cbz"
                elif header.startswith(b"Rar!\x1a\x07"):
                    report["inner_format"] = "cbr"
                else:
                    raise NestedComicError("unsupported_inner_format")
                with open_metadata_archive(
                    inner_stream,
                    inner_path,
                    limit=budget.limit,
                    scratch_parent=parent,
                    validate=budget.validate,
                    check_cancelled=budget.check,
                    progress=lambda current, total: None,
                ) as inner:
                    if any(
                        Path(item.filename).suffix.casefold() in ARCHIVE_SUFFIXES
                        for item in inner.entries
                        if not item.is_dir()
                    ):
                        raise NestedComicError("deeper_nesting")
                    names = [
                        item.filename
                        for item in inner.entries
                        if not item.is_dir() and item.file_size > 0
                    ]
                    pages = canonical_page_names(names)
                    report["page_count"] = len(pages)
                    if len(pages) < 2:
                        raise NestedComicError("insufficient_pages")
                    if any(item.file_size > _PAGE_LIMIT for item in inner.entries):
                        raise NestedComicError("resource_limit")
                    inner_xml = _xml(inner, budget)
                    report["metadata_sources"] = [
                        label
                        for label, data in (("outer", outer_xml), ("inner", inner_xml))
                        if data is not None
                    ]
                    xml = _reconcile_xml(outer_xml, inner_xml)
                    report["metadata_sha256"] = hashlib.sha256(xml or b"").hexdigest()
                    report["comicinfo"] = asdict(parse_comicinfo(xml)) if xml else None
                    report["eligible"] = True
                    report["reason"] = "ready"
                    yield inner, xml


def inspect_nested_comic(
    path: Path, *, max_bytes: int = DEFAULT_LIMIT, scratch_parent: Path | None = None
) -> dict[str, object] | None:
    """Inspect one ZIP wrapper using private spools, never extracted member paths."""
    if path.suffix.casefold() != ".cbz" or not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path) as archive:
        if not any(
            Path(entry.filename).suffix.casefold() in ARCHIVE_SUFFIXES
            for entry in archive.infolist()
            if not entry.is_dir()
        ):
            return None
    report: dict[str, object] = {"version": 1, "outer_format": "cbz", "eligible": False}
    try:
        with _open_nested(path, report, _Budget(max_bytes), scratch_parent=scratch_parent):
            pass
    except NestedComicError as exc:
        report.update(eligible=False, reason=str(exc))
    except (
        FileSafetyError,
        OSError,
        ValueError,
        RuntimeError,
        zipfile.BadZipFile,
        ElementTree.ParseError,
        DefusedXmlException,
    ):
        report.update(eligible=False, reason="inspection_failed")
    return report


def normalize_nested_comic(
    source: Path,
    target: Path,
    *,
    max_bytes: int = DEFAULT_LIMIT,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    """Write a new verified CBZ in private staging; the source is read-only."""
    report: dict[str, object] = {}
    budget = _Budget(max_bytes)
    created = False
    try:
        with _open_nested(source, report, budget, scratch_parent=target.parent) as (inner, xml):
            total = len(inner.entries)
            if progress:
                progress(0, total)
            with target.open("xb") as output:
                created = True
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for index, entry in enumerate(inner.entries, 1):
                        if (
                            entry.is_dir()
                            or PurePosixPath(entry.filename.replace("\\", "/")).name.casefold()
                            == "comicinfo.xml"
                        ):
                            continue
                        with (
                            inner.open(entry) as incoming,
                            archive.open(entry.filename, "w", force_zip64=True) as outgoing,
                        ):
                            _copy_member(incoming, outgoing, entry.file_size, budget)
                        if progress:
                            progress(index, total)
                    if xml is not None:
                        archive.writestr("ComicInfo.xml", xml)
            with zipfile.ZipFile(target) as archive:
                for entry in archive.infolist():
                    with archive.open(entry) as incoming:
                        while incoming.read(1024 * 1024):
                            budget.check()
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
