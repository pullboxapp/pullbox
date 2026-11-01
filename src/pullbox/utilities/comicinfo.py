"""ComicInfo.xml generation and embedding for comic archives.

Generates ComicInfo.xml metadata files following the standard schema
and embeds them into CBZ archives. Reused by the mass convert pipeline
(UT-2) and DDL metadata embedding (Sprint 9).

ComicInfo.xml spec: https://anansi-project.github.io/docs/comicinfo/schemas/v2.1
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import structlog
from defusedxml.common import DefusedXmlException

from pullbox.core.comicinfo_sanitizer import scrub_stale_retailer_value
from pullbox.core.xml_security import parse_untrusted_xml

logger = structlog.get_logger(__name__)

# Fields and their expected types. Order follows the ComicInfo schema convention.
_STRING_FIELDS = (
    "Series",
    "Number",
    "Title",
    "Summary",
    "Writer",
    "Penciller",
    "Inker",
    "Colorist",
    "Letterer",
    "CoverArtist",
    "Editor",
    "Publisher",
    "Imprint",
    "Genre",
    "Tags",
    "Web",
    "Notes",
    "Format",
    "LanguageISO",
    "AgeRating",
    "StoryArc",
    "SeriesGroup",
    "AlternateSeries",
)

_INT_FIELDS = (
    "Year",
    "Month",
    "Day",
    "PageCount",
    "Count",
    "Volume",
    "AlternateCount",
)

_KNOWN_FIELDS = frozenset(_STRING_FIELDS + _INT_FIELDS)
_PULLBOX_AUTHORITATIVE_FIELDS = frozenset(
    {
        "Series",
        "Number",
        "Title",
        "Summary",
        "Writer",
        "Penciller",
        "Inker",
        "Colorist",
        "Letterer",
        "CoverArtist",
        "Editor",
        "Publisher",
        "Year",
        "Month",
        "Day",
        "PageCount",
        "Count",
        "Volume",
        "Web",
        "Notes",
    }
)
ComicInfoProgressCallback = Callable[[str, int, int, str], None]
_COMICINFO_MATERIALIZE_CHUNK_SIZE = 1024 * 1024
_RETAILER_SCRUBBED_FIELDS = frozenset({"Web", "Notes"})


def _normalize_comicinfo_value(
    field: str,
    value: Any,
    *,
    log_invalid: bool = True,
) -> str | None:
    """Normalize a ComicInfo field value for XML output."""
    if field in _STRING_FIELDS:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    if field in _INT_FIELDS:
        if value is None or str(value).strip() == "":
            return None
        try:
            int_val = int(value)
        except (ValueError, TypeError):
            if log_invalid:
                logger.warning(
                    "comicinfo_invalid_numeric",
                    field=field,
                    value=value,
                )
            return None

        if field == "Month" and (int_val < 1 or int_val > 12):
            if log_invalid:
                logger.warning("comicinfo_invalid_month", value=int_val)
            return None
        if field == "Day" and (int_val < 1 or int_val > 31):
            if log_invalid:
                logger.warning("comicinfo_invalid_day", value=int_val)
            return None

        return str(int_val)

    return None


def _build_comicinfo_tree(
    data: dict[str, Any],
    *,
    extra_nodes: list[ET.Element] | None = None,
) -> ET.Element:
    """Build a ComicInfo XML tree from known field data plus preserved extras."""
    root = ET.Element("ComicInfo")

    for field in _STRING_FIELDS:
        normalized = _normalize_comicinfo_value(field, data.get(field))
        if normalized is not None:
            ET.SubElement(root, field).text = normalized

    for field in _INT_FIELDS:
        normalized = _normalize_comicinfo_value(field, data.get(field))
        if normalized is not None:
            ET.SubElement(root, field).text = normalized

    for node in extra_nodes or []:
        root.append(deepcopy(node))

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return root


def _parse_existing_comicinfo(
    xml_content: str,
) -> tuple[dict[str, str], list[ET.Element]]:
    """Parse existing ComicInfo.xml into known-field data plus extra nodes."""
    root = parse_untrusted_xml(xml_content)
    existing_data: dict[str, str] = {}
    extra_nodes: list[ET.Element] = []

    for child in list(root):
        tag = child.tag if isinstance(child.tag, str) else None
        if not tag:
            continue
        if tag in _KNOWN_FIELDS:
            normalized = _normalize_comicinfo_value(tag, child.text, log_invalid=False)
            if normalized is not None:
                existing_data[tag] = normalized
        else:
            extra_nodes.append(child)

    return existing_data, extra_nodes


def _merge_comicinfo_data(
    existing_data: dict[str, str],
    incoming_data: dict[str, Any],
) -> dict[str, str]:
    """Merge existing archive metadata with Pullbox metadata."""
    merged: dict[str, str] = {}

    for field in _STRING_FIELDS + _INT_FIELDS:
        existing_value = _normalize_comicinfo_value(
            field,
            existing_data.get(field),
            log_invalid=False,
        )
        incoming_value = _normalize_comicinfo_value(field, incoming_data.get(field))

        if field in _RETAILER_SCRUBBED_FIELDS:
            scrubbed_existing = scrub_stale_retailer_value(existing_value)
            scrubbed_incoming = scrub_stale_retailer_value(incoming_value)
            existing_value = scrubbed_existing if isinstance(scrubbed_existing, str) else None
            incoming_value = scrubbed_incoming if isinstance(scrubbed_incoming, str) else None

        if field in _PULLBOX_AUTHORITATIVE_FIELDS:
            final_value = incoming_value or existing_value
        else:
            final_value = existing_value or incoming_value

        if final_value is not None:
            merged[field] = final_value

    return merged


def generate_comicinfo_xml(data: dict[str, Any]) -> str:
    """Generate a ComicInfo.xml string from a metadata dict.

    Fields with empty string or None values are omitted. Numeric fields
    are validated (Month 1-12, Day 1-31) and omitted with a warning if
    invalid. HTML entities in values are automatically escaped by
    ElementTree.

    Args:
        data: Dict of ComicInfo field names to values.

    Returns:
        XML string with declaration, ready to write to a file.
    """
    root = _build_comicinfo_tree(_scrub_retailer_fields(data))
    xml_str = '<?xml version="1.0" encoding="utf-8"?>\n'
    xml_str += ET.tostring(root, encoding="unicode")
    return xml_str


def _scrub_retailer_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Remove stale retailer values from ComicInfo Web/Notes fields."""
    cleaned = dict(data)
    for field in _RETAILER_SCRUBBED_FIELDS:
        cleaned[field] = scrub_stale_retailer_value(cleaned.get(field))
    return cleaned


def _serialize_comicinfo_root(root: ET.Element) -> str:
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def _merged_comicinfo_xml_from_existing(
    existing_xml: str | None,
    data: dict[str, Any],
    *,
    archive_path: Path,
) -> tuple[str, str, bool]:
    """Return merged ComicInfo XML, merge mode, and whether existing XML was unchanged."""
    if existing_xml is None:
        return generate_comicinfo_xml(data), "generated", False

    try:
        existing_data, extra_nodes = _parse_existing_comicinfo(existing_xml)
        merged_data = _merge_comicinfo_data(existing_data, data)
        merged_root = _build_comicinfo_tree(merged_data, extra_nodes=extra_nodes)
        xml_content = _serialize_comicinfo_root(merged_root)
        current_root = _build_comicinfo_tree(existing_data, extra_nodes=extra_nodes)
        unchanged = _serialize_comicinfo_root(current_root) == xml_content
        return xml_content, "merged", unchanged
    except (ET.ParseError, DefusedXmlException):
        logger.warning("comicinfo_existing_malformed", path=str(archive_path))
        return generate_comicinfo_xml(data), "replaced_malformed", False


def _find_comicinfo_member(source: zipfile.ZipFile) -> zipfile.ZipInfo | None:
    for item in source.infolist():
        if item.filename.lower() == "comicinfo.xml":
            return item
    return None


def _copy_permission_bits(reference: Path, target: Path) -> None:
    """Give a rewritten archive the permission bits of the file it stands in for.

    Rewrites go through ``tempfile.mkstemp``, which creates the file as 0600. Left
    alone, an archive that was readable by a media server before the rewrite is
    owner-only afterwards. Best effort: filesystems that reject chmod, such as some
    SMB/CIFS mounts, must not fail the rewrite, but the failure is logged so an
    owner-only archive can be traced back to it.
    """
    try:
        os.chmod(target, stat.S_IMODE(reference.stat().st_mode))
    except OSError as exc:
        logger.warning(
            "comicinfo_permission_bits_not_restored",
            reference=str(reference),
            target=str(target),
            error=str(exc),
        )


def embed_comicinfo_in_cbz(
    cbz_path: Path,
    data: dict[str, Any],
    *,
    temp_path: Path | None = None,
    progress_callback: ComicInfoProgressCallback | None = None,
) -> bool:
    """Embed or merge ComicInfo.xml in a CBZ archive.

    If a ComicInfo.xml already exists, Pullbox merges into it instead of
    replacing it blindly. Pullbox-authoritative fields win when Pullbox has a
    value; existing values for other fields are preserved. Malformed existing
    ComicInfo.xml is replaced with a fresh Pullbox-generated document.
    Archive integrity (all other files) is preserved.

    Args:
        cbz_path: Path to the CBZ file.
        data: Dict of ComicInfo field names to values.

    Raises:
        FileNotFoundError: If cbz_path doesn't exist.
    """
    if not cbz_path.exists():
        raise FileNotFoundError(f"CBZ file not found: {cbz_path}")

    existing_xml: str | None = None
    merge_mode = "generated"
    with zipfile.ZipFile(cbz_path, "r") as src:
        comicinfo_item = _find_comicinfo_member(src)
        if comicinfo_item is not None:
            existing_xml = src.read(comicinfo_item.filename).decode("utf-8", errors="replace")

    xml_content, merge_mode, unchanged = _merged_comicinfo_xml_from_existing(
        existing_xml,
        data,
        archive_path=cbz_path,
    )
    if unchanged:
        _emit_comicinfo_progress(
            progress_callback,
            "rewriting",
            1,
            1,
            "entries",
        )
        logger.info(
            "comicinfo_embed_skipped_noop",
            path=str(cbz_path),
            series=data.get("Series", ""),
        )
        return False

    # Create a temp file alongside the original to avoid corruption
    if temp_path is None:
        tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".cbz", dir=str(cbz_path.parent))
    else:
        tmp_fd = -1
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        if temp_path.exists():
            temp_path.unlink()
        tmp_path_str = str(temp_path)

    try:
        tmp_path = Path(tmp_path_str)

        with (
            zipfile.ZipFile(cbz_path, "r") as src,
            zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as dst,
        ):
            entries = [item for item in src.infolist() if item.filename.lower() != "comicinfo.xml"]
            total_entries = len(entries) + 1
            # Copy all entries except existing ComicInfo.xml
            for index, item in enumerate(entries, start=1):
                dst.writestr(item, src.read(item.filename))
                _emit_comicinfo_progress(
                    progress_callback,
                    "rewriting",
                    index,
                    total_entries,
                    "entries",
                )

            # Write the new ComicInfo.xml
            dst.writestr("ComicInfo.xml", xml_content.encode("utf-8"))
            _emit_comicinfo_progress(
                progress_callback,
                "rewriting",
                total_entries,
                total_entries,
                "entries",
            )

        # Atomic replace
        _copy_permission_bits(cbz_path, tmp_path)
        shutil.move(str(tmp_path), str(cbz_path))

        logger.info(
            "comicinfo_embedded",
            path=str(cbz_path),
            series=data.get("Series", ""),
            merge_mode=merge_mode,
            existing_found=existing_xml is not None,
        )
        return True
    except Exception:
        # Clean up temp file on failure
        tmp_path = Path(tmp_path_str)
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    finally:
        # Close the file descriptor if still open
        if tmp_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(tmp_fd)


def materialize_cbz_with_comicinfo(
    source_path: Path,
    target_path: Path,
    data: dict[str, Any],
    *,
    transfer_method: str,
    temp_path: Path | None = None,
    progress_callback: ComicInfoProgressCallback | None = None,
) -> bool:
    """Copy/move a source CBZ to a target CBZ while writing authoritative ComicInfo.xml."""
    if source_path.suffix.lower() != ".cbz" or target_path.suffix.lower() != ".cbz":
        raise ValueError("ComicInfo materialization requires source and target CBZ paths.")
    if transfer_method not in {"move", "copy"}:
        raise ValueError(f"Unsupported ComicInfo materialization method: {transfer_method}")
    if not source_path.exists():
        raise FileNotFoundError(f"CBZ source not found: {source_path}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if temp_path is None:
        tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".cbz", dir=str(target_path.parent))
    else:
        tmp_fd = -1
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        if temp_path.exists():
            temp_path.unlink()
        tmp_path_str = str(temp_path)

    try:
        temp_output_path = Path(tmp_path_str)
        with zipfile.ZipFile(source_path, "r") as src:
            comicinfo_item = _find_comicinfo_member(src)
            existing_xml = (
                src.read(comicinfo_item.filename).decode("utf-8", errors="replace")
                if comicinfo_item is not None
                else None
            )
            xml_content, merge_mode, _unchanged = _merged_comicinfo_xml_from_existing(
                existing_xml,
                data,
                archive_path=source_path,
            )
            entries = [item for item in src.infolist() if item.filename.lower() != "comicinfo.xml"]
            total_transfer_bytes = max(sum(item.file_size for item in entries), 1)
            transferred_bytes = 0
            with zipfile.ZipFile(temp_output_path, "w", zipfile.ZIP_DEFLATED) as dst:
                _emit_comicinfo_progress(
                    progress_callback,
                    "transferring",
                    0,
                    total_transfer_bytes,
                    "bytes",
                )
                for item in entries:
                    transferred_bytes = _copy_zip_member_with_progress(
                        src,
                        dst,
                        item,
                        transferred_bytes=transferred_bytes,
                        total_transfer_bytes=total_transfer_bytes,
                        progress_callback=progress_callback,
                    )
                _emit_comicinfo_progress(
                    progress_callback,
                    "transferring",
                    total_transfer_bytes,
                    total_transfer_bytes,
                    "bytes",
                )
                _emit_comicinfo_progress(
                    progress_callback,
                    "rewriting",
                    0,
                    1,
                    "entries",
                )
                dst.writestr("ComicInfo.xml", xml_content.encode("utf-8"))
                _emit_comicinfo_progress(
                    progress_callback,
                    "rewriting",
                    1,
                    1,
                    "entries",
                )

        # A plain move or copy would carry the source's mode to the target.
        _copy_permission_bits(source_path, temp_output_path)
        shutil.move(str(temp_output_path), str(target_path))
        if transfer_method == "move" and source_path.resolve(strict=False) != target_path.resolve(
            strict=False
        ):
            try:
                source_path.unlink(missing_ok=True)
            except Exception:
                # The library target is a duplicate until the source is removed.
                # Keep move semantics atomic from the caller's perspective.
                try:
                    target_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception(
                        "comicinfo_materialized_target_cleanup_failed",
                        source=str(source_path),
                        target=str(target_path),
                    )
                raise

        logger.info(
            "comicinfo_materialized_cbz",
            source=str(source_path),
            target=str(target_path),
            transfer_method=transfer_method,
            series=data.get("Series", ""),
            merge_mode=merge_mode,
            existing_found=existing_xml is not None,
        )
        return True
    except Exception:
        temp_output_path = Path(tmp_path_str)
        if temp_output_path.exists():
            temp_output_path.unlink()
        raise
    finally:
        if tmp_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(tmp_fd)


def _copy_zip_member_with_progress(
    src: zipfile.ZipFile,
    dst: zipfile.ZipFile,
    item: zipfile.ZipInfo,
    *,
    transferred_bytes: int,
    total_transfer_bytes: int,
    progress_callback: ComicInfoProgressCallback | None,
) -> int:
    with src.open(item, "r") as src_member, dst.open(item, "w") as dst_member:
        while True:
            chunk = src_member.read(_COMICINFO_MATERIALIZE_CHUNK_SIZE)
            if not chunk:
                break
            dst_member.write(chunk)
            transferred_bytes += len(chunk)
            _emit_comicinfo_progress(
                progress_callback,
                "transferring",
                min(transferred_bytes, total_transfer_bytes),
                total_transfer_bytes,
                "bytes",
            )
    return transferred_bytes


def _emit_comicinfo_progress(
    progress_callback: ComicInfoProgressCallback | None,
    stage: str,
    current: int,
    total: int,
    unit: str,
) -> None:
    if progress_callback is None:
        return
    progress_callback(stage, current, total, unit)
