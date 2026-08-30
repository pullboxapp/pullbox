"""File type converter executor — converts comic files between formats.

Supports CBR→CBZ, CB7→CBZ, CBZ repack, PDF→CBZ conversions.
Exports a standalone convert_file() function for reuse by the
download post-processing pipeline (Sprint 9 DDL Engine).

The FileConverterExecutor wraps convert_file() for job queue integration,
adding trash management, rollback support, and batch item generation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from functools import partial
from pathlib import Path
from typing import Any, cast

import structlog
from sqlalchemy import select

from pullbox.core.file_safety import has_archive_member_path_traversal
from pullbox.core.library_root_resolution import resolve_path_inside_roots
from pullbox.core.rar_backend import RarBackendUnavailableError, configure_rarfile_backend
from pullbox.models.library import LibraryFile, LibraryFileStorageMode
from pullbox.utilities.base_executor import ExecutionMode, ItemResult, JobExecutor, ProcessedItem
from pullbox.utilities.settings import (
    move_file_to_utility_trash,
    resolve_trash_directory,
    restore_file_from_utility_trash,
)

logger = structlog.get_logger(__name__)

_SUPPORTED_TARGET_FORMATS = frozenset({"cbz"})
_SUPPORTED_SOURCE_FORMATS = frozenset({"cbr", "cb7", "cbz", "pdf"})
ConversionProgressCallback = Callable[[str, int, int, str], None]


# ── Standalone Conversion Function ─────────────────────────────


async def convert_file(
    source: Path,
    target_format: str,
    destination: Path | None = None,
    *,
    progress_callback: ConversionProgressCallback | None = None,
    allow_resource_safety_exception: bool = False,
) -> Path:
    """Convert a single comic file. Usable outside the job queue.

    Args:
        source: Path to the source file.
        target_format: Target format (currently only "cbz").
        destination: Output directory. If None, uses source's directory.

    Returns:
        Path to the converted file.

    Raises:
        FileNotFoundError: If source doesn't exist.
        FileExistsError: If target file already exists.
        ValueError: If format is unsupported or archive is invalid.
    """
    if not source.exists():
        raise FileNotFoundError(f"Source file not found: {source}")

    if source.stat().st_size == 0:
        raise ValueError(f"Source file is empty: {source}")

    if target_format not in _SUPPORTED_TARGET_FORMATS:
        raise ValueError(
            f"Unsupported target format: {target_format}. "
            f"Supported: {', '.join(sorted(_SUPPORTED_TARGET_FORMATS))}"
        )

    dest_dir = destination or source.parent
    target_path = dest_dir / f"{source.stem}.{target_format}"

    if target_path.exists():
        raise FileExistsError(f"Target file already exists: {target_path}")

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        partial(
            _convert_sync,
            source,
            target_format,
            target_path,
            progress_callback,
            allow_resource_safety_exception=allow_resource_safety_exception,
        ),
    )


def _detect_archive_type(source: Path) -> str:
    """Detect actual archive type by magic bytes, falling back to extension.

    Many .cbr files are actually ZIP archives (mislabeled). This ensures
    the correct converter is used regardless of file extension.
    """
    try:
        with open(source, "rb") as f:
            header = f.read(8)
    except OSError:
        return source.suffix.lower()

    # ZIP: PK\x03\x04
    if header[:4] == b"PK\x03\x04":
        return ".cbz"
    # RAR: Rar!\x1a\x07
    if header[:6] == b"Rar!\x1a\x07":
        return ".cbr"
    # 7z: 7z\xbc\xaf\x27\x1c
    if header[:6] == b"7z\xbc\xaf\x27\x1c":
        return ".cb7"
    # PDF: %PDF
    if header[:4] == b"%PDF":
        return ".pdf"

    return source.suffix.lower()


def _convert_sync(
    source: Path,
    target_format: str,
    target_path: Path,
    progress_callback: ConversionProgressCallback | None = None,
    *,
    pdf_quality: str = "medium",
    allow_resource_safety_exception: bool = False,
) -> Path:
    """Synchronous conversion — runs in thread pool."""
    actual_type = _detect_archive_type(source)

    if actual_type == ".cb7":
        return _convert_cb7_to_cbz(source, target_path, progress_callback=progress_callback)
    if actual_type == ".cbr":
        return _convert_cbr_to_cbz(source, target_path, progress_callback=progress_callback)
    if actual_type == ".cbz":
        return _repack_cbz(source, target_path, progress_callback=progress_callback)
    if actual_type == ".pdf":
        return _convert_pdf_to_cbz(
            source,
            target_path,
            pdf_quality=pdf_quality,
            progress_callback=progress_callback,
            allow_resource_safety_exception=allow_resource_safety_exception,
        )

    raise ValueError(f"Unsupported source format: {actual_type}")


# ── Format-Specific Converters ─────────────────────────────────


def _format_corrupt_cbr_error(source: Path, detail: str) -> str:
    detail = detail.strip() or "the archive extractor could not read the file"
    return (
        f"CBR archive appears corrupt or incomplete and could not be converted: "
        f"{source.name}. Try re-downloading or replacing the file. Details: {detail}"
    )


def _format_missing_cbr_backend_error(source: Path, detail: str) -> str:
    detail = detail.strip() or "the archive extractor is not installed"
    return (
        f"CBR archive cannot be converted because official UnRAR is unavailable: "
        f"{source.name}. Details: {detail}"
    )


def _convert_cb7_to_cbz(
    source: Path,
    target: Path,
    *,
    progress_callback: ConversionProgressCallback | None = None,
) -> Path:
    """Extract 7z archive and repack as ZIP/CBZ."""
    import py7zr

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        with py7zr.SevenZipFile(source, "r") as archive:
            names = archive.getnames()
            extractables = _archive_extractable_names(names)
            _validate_archive_member_names(extractables)
            # py7zr can raise false CRC errors when repeatedly extracting
            # individual members from the same solid archive stream.
            archive.extract(path=tmp_path, targets=extractables)
            for index, _member in enumerate(extractables, start=1):
                _emit_conversion_progress(
                    progress_callback,
                    "extracting",
                    index,
                    len(extractables),
                    "entries",
                )

        _pack_directory_as_cbz(
            tmp_path,
            target,
            progress_callback=(
                None
                if progress_callback is None
                else lambda current, total: _emit_conversion_progress(
                    progress_callback,
                    "packing",
                    current,
                    total,
                    "entries",
                )
            ),
        )

    return target


def _convert_cbr_to_cbz(
    source: Path,
    target: Path,
    *,
    progress_callback: ConversionProgressCallback | None = None,
) -> Path:
    """Extract RAR archive and repack as ZIP/CBZ."""
    import rarfile  # type: ignore[import-untyped]

    try:
        configure_rarfile_backend()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with rarfile.RarFile(source) as rf:
                names = rf.namelist()
                extractables = _archive_extractable_names(names)
                _validate_archive_member_names(extractables)
                for index, member in enumerate(extractables, start=1):
                    rf.extract(member, tmp_path)
                    _emit_conversion_progress(
                        progress_callback,
                        "extracting",
                        index,
                        len(extractables),
                        "entries",
                    )

            _pack_directory_as_cbz(
                tmp_path,
                target,
                progress_callback=(
                    None
                    if progress_callback is None
                    else lambda current, total: _emit_conversion_progress(
                        progress_callback,
                        "packing",
                        current,
                        total,
                        "entries",
                    )
                ),
            )
    except RarBackendUnavailableError as exc:
        raise ValueError(_format_missing_cbr_backend_error(source, str(exc))) from exc
    except (
        rarfile.BadRarFile,
        rarfile.NeedFirstVolume,
        rarfile.NotRarFile,
        rarfile.RarCRCError,
    ) as exc:
        raise ValueError(_format_corrupt_cbr_error(source, str(exc))) from exc

    return target


def _repack_cbz(
    source: Path,
    target: Path,
    *,
    progress_callback: ConversionProgressCallback | None = None,
) -> Path:
    """Repack an existing CBZ (normalize compression, clean up)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        with zipfile.ZipFile(source, "r") as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            _validate_archive_member_names([info.filename for info in infos])
            for index, info in enumerate(infos, start=1):
                zf.extract(info, tmp_path)
                _emit_conversion_progress(
                    progress_callback,
                    "extracting",
                    index,
                    len(infos),
                    "entries",
                )

        _pack_directory_as_cbz(
            tmp_path,
            target,
            progress_callback=(
                None
                if progress_callback is None
                else lambda current, total: _emit_conversion_progress(
                    progress_callback,
                    "packing",
                    current,
                    total,
                    "entries",
                )
            ),
        )

    return target


_PDF_QUALITY_PRESETS: dict[str, tuple[int, str, str, dict[str, int]]] = {
    # (dpi, image_format, extension, save_kwargs)
    "high": (300, "PNG", "png", {}),
    "medium": (200, "JPEG", "jpg", {"quality": 90}),
    "low": (150, "JPEG", "jpg", {"quality": 80}),
}
_PDF_RENDER_CHUNK_SIZE = 16
_PDF_RENDER_THREAD_COUNT = 1


def _convert_pdf_to_cbz(
    source: Path,
    target: Path,
    *,
    pdf_quality: str = "medium",
    progress_callback: ConversionProgressCallback | None = None,
    allow_resource_safety_exception: bool = False,
) -> Path:
    """Rasterize PDF pages and pack as CBZ.

    Large collected-edition PDFs can contain hundreds of pages, so rendering
    the whole document into one in-memory image list is a fast route to an OOM
    kill. Render and encode in bounded chunks instead.
    """
    from pdf2image import pdfinfo_from_path
    from PIL import Image

    dpi, img_format, img_ext, save_kwargs = _PDF_QUALITY_PRESETS.get(
        pdf_quality, _PDF_QUALITY_PRESETS["medium"]
    )

    with (
        _pillow_decompression_bomb_override(allow_resource_safety_exception),
        tempfile.TemporaryDirectory() as tmp_dir,
    ):
        tmp_path = Path(tmp_dir)
        try:
            total_pages = int(pdfinfo_from_path(str(source)).get("Pages") or 0)
        except Exception:
            total_pages = 0

        rendered_pages = 0
        render_progress_total = total_pages
        for page_number, raw_page_path, progress_total in _render_pdf_page_paths(
            source,
            tmp_path,
            dpi=dpi,
            total_pages=total_pages,
            progress_callback=progress_callback,
        ):
            rendered_pages = page_number
            render_progress_total = progress_total
            output_image_path = tmp_path / f"page_{page_number - 1:04d}.{img_ext}"
            with Image.open(raw_page_path) as image:
                image.save(str(output_image_path), img_format, **save_kwargs)
            raw_page_path.unlink(missing_ok=True)
            _emit_conversion_progress(
                progress_callback,
                "encoding",
                page_number,
                progress_total,
                "pages",
            )

        if rendered_pages == 0:
            raise ValueError(f"PDF has no pages: {source}")

        _pack_directory_as_cbz(
            tmp_path,
            target,
            progress_callback=(
                None
                if progress_callback is None
                else lambda current, total: _emit_conversion_progress(
                    progress_callback,
                    "packing",
                    current,
                    total or render_progress_total,
                    "pages",
                )
            ),
        )

    return target


@contextlib.contextmanager
def _pillow_decompression_bomb_override(enabled: bool) -> Iterator[None]:
    """Temporarily disable Pillow's pixel-count guard for explicit one-time approvals."""
    if not enabled:
        yield
        return

    from PIL import Image

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        yield
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


def _render_pdf_page_paths(
    source: Path,
    output_dir: Path,
    *,
    dpi: int,
    total_pages: int,
    progress_callback: ConversionProgressCallback | None = None,
) -> Iterator[tuple[int, Path, int]]:
    """Render PDF pages to temporary PPM files in bounded chunks."""
    from pdf2image import convert_from_path

    def _iter_chunk(
        page_paths: list[str],
        *,
        start_page: int,
        progress_total: int,
    ) -> Iterator[tuple[int, Path, int]]:
        for offset, page_path in enumerate(page_paths):
            page_number = start_page + offset
            _emit_conversion_progress(
                progress_callback,
                "rendering",
                page_number,
                progress_total,
                "pages",
            )
            yield (page_number, Path(page_path), progress_total)

    if total_pages > 0:
        for start_page in range(1, total_pages + 1, _PDF_RENDER_CHUNK_SIZE):
            end_page = min(start_page + _PDF_RENDER_CHUNK_SIZE - 1, total_pages)
            chunk_paths = cast(
                "list[str]",
                convert_from_path(
                    str(source),
                    dpi=dpi,
                    output_folder=str(output_dir),
                    fmt="ppm",
                    paths_only=True,
                    first_page=start_page,
                    last_page=end_page,
                    thread_count=_PDF_RENDER_THREAD_COUNT,
                ),
            )
            yield from _iter_chunk(
                chunk_paths,
                start_page=start_page,
                progress_total=total_pages,
            )
        return

    chunk_paths = cast(
        "list[str]",
        convert_from_path(
            str(source),
            dpi=dpi,
            output_folder=str(output_dir),
            fmt="ppm",
            paths_only=True,
            thread_count=_PDF_RENDER_THREAD_COUNT,
        ),
    )
    yield from _iter_chunk(
        chunk_paths,
        start_page=1,
        progress_total=len(chunk_paths),
    )


def _validate_archive_member_names(member_names: list[str]) -> None:
    """Reject archive members that could escape the extraction directory."""
    unsafe = [name for name in member_names if has_archive_member_path_traversal(name)]
    if unsafe:
        raise ValueError(f"Archive contains unsafe archive member path: {unsafe[0]}")


def _archive_extractable_names(member_names: list[str]) -> list[str]:
    return [name for name in member_names if name and not name.endswith("/")]


def _emit_conversion_progress(
    progress_callback: ConversionProgressCallback | None,
    stage: str,
    current: int,
    total: int,
    unit: str,
) -> None:
    if progress_callback is None:
        return
    progress_callback(stage, current, total, unit)


def _pack_directory_as_cbz(
    directory: Path,
    target: Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    """Pack all files in a directory into a CBZ (ZIP) archive."""
    target.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(f for f in directory.rglob("*") if f.is_file())

    if not files:
        raise ValueError(f"No files to pack from: {directory}")

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for index, file_path in enumerate(files, start=1):
            arcname = str(file_path.relative_to(directory))
            zf.write(file_path, arcname)
            if progress_callback is not None:
                progress_callback(index, len(files))


# ── Preview ────────────────────────────────────────────────────

# Lossless conversions: archive repack without rasterization
_LOSSLESS_CONVERSIONS = frozenset({("cbr", "cbz"), ("cb7", "cbz"), ("cbz", "cbz")})


def build_convert_preview(
    source_format: str,
    target_format: str,
    scope: str = "manual",
    file_paths: list[str] | None = None,
    *,
    allowed_roots: Sequence[str | Path] | None = None,
    excluded_paths: frozenset[str] = frozenset(),
) -> Any:
    """Build a preview of files that would be converted.

    Args:
        source_format: Source file format to filter by.
        target_format: Target conversion format.
        scope: Scope filter (currently only "manual" is supported).
        file_paths: Explicit file paths for manual scope.

    Returns:
        ConvertPreviewResponse with file counts, sizes, and lossless flag.
    """
    from pullbox.utilities.schemas import ConvertPreviewFileInfo, ConvertPreviewResponse

    lossless = (source_format, target_format) in _LOSSLESS_CONVERSIONS

    matching_files: list[ConvertPreviewFileInfo] = []
    total_size = 0

    if scope == "manual" and file_paths:
        for path_str in file_paths:
            try:
                if allowed_roots is not None:
                    path = resolve_path_inside_roots(path_str, allowed_roots, require_file=True)
                else:
                    # Direct helper callers are internal/test-only; API callers pass
                    # allowed_roots so user-selected paths stay inside library roots.
                    # codeql[py/path-injection]
                    path = Path(path_str).expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if not path.is_file():
                continue
            if str(path) in excluded_paths:
                continue
            size = path.stat().st_size
            total_size += size
            matching_files.append(
                ConvertPreviewFileInfo(
                    path=str(path),
                    output_path=str(path.with_suffix(f".{target_format}")),
                    size_bytes=size,
                )
            )

    total_count = len(matching_files)
    # Truncate file list to 100 for response, keep accurate total_count
    sample_files = matching_files[:100]

    return ConvertPreviewResponse(
        source_format=source_format,
        target_format=target_format,
        total_count=total_count,
        total_size_bytes=total_size,
        lossless=lossless,
        files=sample_files,
    )


# ── FileConverterExecutor ──────────────────────────────────────


class FileConverterExecutor(JobExecutor):
    """Job queue executor for batch file conversion.

    Wraps the standalone convert_file() function with trash management,
    rollback support, and batch item generation from config.
    """

    execution_mode = ExecutionMode.PROCESS

    async def build_job_context(
        self,
        session: Any,
        job_config: dict[str, Any],
    ) -> dict[str, Any]:
        _ = job_config
        referenced_paths = list(
            (
                await session.execute(
                    select(LibraryFile.file_path).where(
                        LibraryFile.storage_mode == LibraryFileStorageMode.REFERENCED
                    )
                )
            )
            .scalars()
            .all()
        )
        return {"referenced_paths": referenced_paths}

    def validate_config(self, job_config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        target = job_config.get("target_format")
        if not target:
            errors.append("target_format is required")
        elif target not in _SUPPORTED_TARGET_FORMATS:
            errors.append(
                f"Invalid target_format: {target}. "
                f"Supported: {', '.join(sorted(_SUPPORTED_TARGET_FORMATS))}"
            )

        return errors

    async def generate_items(
        self,
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Discover files to convert based on config scope.

        Config keys:
            scope: "manual" (with file_paths list) or "all"
            source_format: Filter by source extension
            file_paths: List of specific file paths (for scope=manual)
        """
        scope = job_config.get("scope", "manual")
        items: list[dict[str, Any]] = []

        if scope == "manual":
            selected_paths = [Path(path_str) for path_str in job_config.get("file_paths", [])]
            existing_paths = [path for path in selected_paths if path.exists()]
            relative_to = None
            if existing_paths:
                relative_to = Path(
                    os.path.commonpath([str(path.parent) for path in existing_paths])
                )

            for path in selected_paths:
                if path.exists():
                    items.append(
                        {
                            "file_path": str(path),
                            "operation": "convert",
                            "trash_relative_path": (
                                str(path.relative_to(relative_to))
                                if relative_to is not None
                                else path.name
                            ),
                        }
                    )

        return items

    def process_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Convert a single file with trash management."""
        start = time.monotonic()
        item_id = item_data.get("id", "unknown")
        file_path = item_data.get("file_path", "")
        source = Path(file_path)

        try:
            referenced_paths = (job_context or {}).get("referenced_paths", [])
            resolved_source = source.expanduser().resolve(strict=False)
            if isinstance(referenced_paths, list) and any(
                Path(str(referenced_path)).expanduser().resolve(strict=False) == resolved_source
                for referenced_path in referenced_paths
            ):
                return ProcessedItem(
                    item_id=item_id,
                    result=ItemResult.SKIPPED,
                    before_state={"path": str(source)},
                    after_state={"path": str(source), "reason": "referenced_file"},
                    duration_ms=int((time.monotonic() - start) * 1000),
                    warning_message="Referenced library files cannot be converted.",
                    log_entries=[
                        (
                            "WARNING",
                            f"Skipped referenced library file: {source.name}",
                            {"reason": "referenced_file"},
                        )
                    ],
                )
            if not source.exists():
                raise FileNotFoundError(f"Source file not found: {source}")

            target_format = job_config.get("target_format", "cbz")
            source_format = job_config.get("source_format", source.suffix.lstrip("."))
            pdf_quality = job_config.get("pdf_quality", "medium")
            target_path = source.with_suffix(f".{target_format}")

            # For repack (same format), write to a temp file then swap
            is_repack = source_format == target_format or target_path == source
            if is_repack:
                temp_target = source.with_name(f"{source.stem}._repack_.{target_format}")
                result_path = _convert_sync(
                    source, target_format, temp_target, pdf_quality=pdf_quality
                )
            elif target_path.exists():
                raise FileExistsError(f"Target already exists: {target_path}")
            else:
                result_path = _convert_sync(
                    source, target_format, target_path, pdf_quality=pdf_quality
                )

            # Move original to trash
            original_trash_path: str | None = None
            trash_dir = resolve_trash_directory(job_config.get("trash_folder"))
            if trash_dir:
                trash_dest = move_file_to_utility_trash(
                    source,
                    trash_dir,
                    relative_path=item_data.get("trash_relative_path"),
                )
                original_trash_path = str(trash_dest)
            elif is_repack:
                # No trash folder — delete the original so we can rename the temp
                source.unlink()

            # For repack, rename temp file to the final target path
            if is_repack and result_path.name != target_path.name:
                result_path.rename(target_path)
                result_path = target_path

            duration_ms = int((time.monotonic() - start) * 1000)

            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.COMPLETED,
                before_state={
                    "path": str(source),
                    "format": source_format,
                },
                after_state={
                    "path": str(result_path),
                    "format": target_format,
                    "original_path": original_trash_path,
                },
                duration_ms=duration_ms,
                log_entries=[
                    (
                        "INFO",
                        f"Converted {source.name} → {result_path.name}",
                        {"source": str(source), "target": str(result_path)},
                    ),
                ],
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.FAILED,
                before_state={"path": file_path},
                duration_ms=duration_ms,
                error_message=str(exc),
                log_entries=[
                    ("ERROR", f"Conversion failed for {file_path}: {exc}", {}),
                ],
            )

    def rollback_item(
        self,
        item_data: dict[str, Any],
        job_config: dict[str, Any],
        job_context: dict[str, Any] | None = None,
    ) -> ProcessedItem:
        """Restore original from trash and remove converted file."""
        start = time.monotonic()
        item_id = item_data.get("id", "unknown")

        try:
            after_state = item_data.get("after_state", {})
            if isinstance(after_state, str):
                after_state = json.loads(after_state)

            converted_path = Path(after_state.get("path", ""))
            original_trash = after_state.get("original_path", "")

            if not original_trash:
                raise FileNotFoundError("Rollback metadata missing original trash path")

            trash_path = Path(original_trash)
            before_state = item_data.get("before_state", {})
            if isinstance(before_state, str):
                before_state = json.loads(before_state)
            original_path = Path(before_state.get("path", ""))
            restore_file_from_utility_trash(
                trash_path,
                original_path,
                converted_path=converted_path,
            )

            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.COMPLETED,
                duration_ms=duration_ms,
                log_entries=[("INFO", f"Rolled back {item_id}", {})],
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ProcessedItem(
                item_id=item_id,
                result=ItemResult.FAILED,
                duration_ms=duration_ms,
                error_message=f"Rollback failed: {exc}",
                log_entries=[("ERROR", f"Rollback failed: {exc}", {})],
            )
