"""Typed, sanitized diagnostics for actionable import safety failures."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


class ImportSafetyCategory(enum.StrEnum):
    """Stable review categories for import safety and source-integrity failures."""

    PERMISSION_UNREADABLE = "permission_unreadable"
    ARCHIVE_INSPECTION_FAILED = "archive_inspection_failed"
    ZERO_BYTE = "zero_byte"
    ARCHIVE_NO_PAGES = "archive_no_pages"
    SINGLE_PAGE_COMIC = "single_page_comic"
    DECOMPRESSION_SIZE_LIMIT = "decompression_size_limit"
    DANGEROUS_PATH_OR_PAYLOAD = "dangerous_path_or_payload"
    OUTSIDE_APPROVED_ROOT = "outside_approved_root"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    SOURCE_CHANGED = "source_changed"
    UNKNOWN = "unknown"


_CATEGORY_LABELS: dict[ImportSafetyCategory, str] = {
    ImportSafetyCategory.PERMISSION_UNREADABLE: "Unreadable or permission denied",
    ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED: "Archive inspection failed",
    ImportSafetyCategory.ZERO_BYTE: "Zero-byte file",
    ImportSafetyCategory.ARCHIVE_NO_PAGES: "No comic pages",
    ImportSafetyCategory.SINGLE_PAGE_COMIC: "Possible cover-only file",
    ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT: "Decompression-size limit",
    ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD: "Dangerous link, path, or payload",
    ImportSafetyCategory.OUTSIDE_APPROVED_ROOT: "Outside approved root",
    ImportSafetyCategory.UNSUPPORTED_FILE_TYPE: "Unsupported file type",
    ImportSafetyCategory.SOURCE_CHANGED: "Source changed or unavailable",
    ImportSafetyCategory.UNKNOWN: "Other safety failure",
}

_SANITIZED_REASONS: dict[ImportSafetyCategory, str] = {
    ImportSafetyCategory.PERMISSION_UNREADABLE: (
        "Pullbox could not read this file. Check its permissions and try again."
    ),
    ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED: (
        "Pullbox could not inspect this archive. It may be corrupt, incomplete, "
        "or temporarily unavailable."
    ),
    ImportSafetyCategory.ZERO_BYTE: (
        "The file is empty (zero bytes). Replace it with a complete file and retry."
    ),
    ImportSafetyCategory.ARCHIVE_NO_PAGES: (
        "The archive contains no non-empty comic image pages. Metadata alone is not a comic. "
        "Replace the file or skip it; its series identity is preserved."
    ),
    ImportSafetyCategory.SINGLE_PAGE_COMIC: (
        "The archive contains only one image page and may be an alternate cover. "
        "Review the source and allow once only if this is intentionally a one-page comic."
    ),
    ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT: (
        "The archive exceeds Pullbox's configured decompressed-size limit."
    ),
    ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD: (
        "The file contains a dangerous path, link, or payload and cannot be overridden."
    ),
    ImportSafetyCategory.OUTSIDE_APPROVED_ROOT: (
        "The file is outside the approved import or library root."
    ),
    ImportSafetyCategory.UNSUPPORTED_FILE_TYPE: ("The file type is not supported for import."),
    ImportSafetyCategory.SOURCE_CHANGED: (
        "The source changed or became unavailable after scanning. Rescan before retrying."
    ),
    ImportSafetyCategory.UNKNOWN: (
        "Pullbox blocked this file because its safety inspection did not complete safely."
    ),
}

_RETRYABLE_CATEGORIES = frozenset(
    {
        ImportSafetyCategory.PERMISSION_UNREADABLE,
        ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED,
        ImportSafetyCategory.ZERO_BYTE,
        ImportSafetyCategory.ARCHIVE_NO_PAGES,
        ImportSafetyCategory.OUTSIDE_APPROVED_ROOT,
        ImportSafetyCategory.SOURCE_CHANGED,
    }
)

_PERMISSION_CODES = frozenset({"permission_denied", "source_unreadable", "unreadable"})
_INSPECTION_CODES = frozenset({"archive_inspection_failed", "corrupt_archive", "inspection_failed"})
_ZERO_BYTE_CODES = frozenset({"zero_byte", "zero_byte_file", "empty_file"})
_CONTENT_CODES = frozenset({"archive_no_pages", "single_page_comic"})
_SIZE_CODES = frozenset(
    {
        "archive_decompressed_size",
        "archive_decompressed_size_limit",
        "decompression_size_limit",
        "pillow_decompression_bomb",
    }
)
_DANGEROUS_CODES = frozenset(
    {
        "dangerous_archive_path",
        "dangerous_path_or_payload",
        "dangerous_payload",
        "path_traversal",
        "source_path_unsafe",
        "unsafe_path_mapping",
        "invalid_path_text",
    }
)
_OUTSIDE_ROOT_CODES = frozenset(
    {
        "outside_approved_root",
        "source_outside_root",
        "outside_root",
        "source_root_ambiguous",
    }
)
_UNSUPPORTED_CODES = frozenset({"unsupported_file_type", "unsupported_extension"})
_SOURCE_CHANGED_CODES = frozenset(
    {
        "source_changed",
        "source_missing",
        "source_signature_missing",
        "source_signature_unsupported",
        "source_unavailable",
        "source_root_unconfirmed",
        "source_root_changed",
    }
)


@dataclass(frozen=True, slots=True)
class ImportSafetyClassification:
    """Pure classification result safe to persist or expose in review UI."""

    category: ImportSafetyCategory
    code: str
    sanitized_reason: str
    retryable: bool
    overrideable: bool


def _normalized_token(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _contains_any(value: str, markers: Sequence[str]) -> bool:
    return any(marker in value for marker in markers)


def classify_import_safety_failure(
    reason: str,
    *,
    kind: str | None = None,
    code: str | None = None,
    overrideable_hint: bool | None = None,
) -> ImportSafetyClassification:
    """Classify raw failure evidence without returning raw paths or exception text.

    Exact machine codes take precedence over human text. The override hint can
    make a size-limit block stricter, but can never make a dangerous or
    containment failure overrideable.
    """
    normalized_reason = reason.strip().lower()
    normalized_kind = _normalized_token(kind)
    normalized_code = _normalized_token(code)
    evidence_tokens = {token for token in (normalized_code, normalized_kind) if token}
    stable_reason_tokens = (
        _PERMISSION_CODES
        | _INSPECTION_CODES
        | _ZERO_BYTE_CODES
        | _CONTENT_CODES
        | _SIZE_CODES
        | _DANGEROUS_CODES
        | _OUTSIDE_ROOT_CODES
        | _UNSUPPORTED_CODES
        | _SOURCE_CHANGED_CODES
    )
    if _normalized_token(reason) in stable_reason_tokens:
        evidence_tokens.add(_normalized_token(reason))

    category: ImportSafetyCategory
    stable_code: str
    if evidence_tokens & _DANGEROUS_CODES or _contains_any(
        normalized_reason,
        (
            "path traversal",
            "dangerous executable",
            "dangerous file",
            "dangerous payload",
            "unsafe path component",
            "unsafe archive member",
            "symbolic link",
            "symlink",
        ),
    ):
        category = ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD
        if normalized_code in _DANGEROUS_CODES:
            stable_code = normalized_code
        elif "path traversal" in normalized_reason or "unsafe path" in normalized_reason:
            stable_code = "dangerous_archive_path"
        else:
            stable_code = "dangerous_payload"
    elif evidence_tokens & _OUTSIDE_ROOT_CODES or _contains_any(
        normalized_reason,
        (
            "outside approved root",
            "outside enabled library root",
            "outside its library root",
            "outside the import root",
            "outside configured root",
            "escapes the approved root",
        ),
    ):
        category = ImportSafetyCategory.OUTSIDE_APPROVED_ROOT
        stable_code = normalized_code or "outside_approved_root"
    elif evidence_tokens & _PERMISSION_CODES or _contains_any(
        normalized_reason,
        ("permissionerror", "permission denied", "not readable", "unreadable"),
    ):
        category = ImportSafetyCategory.PERMISSION_UNREADABLE
        stable_code = normalized_code or "permission_denied"
    elif (
        evidence_tokens & _ZERO_BYTE_CODES
        or _contains_any(
            normalized_reason,
            ("zero-byte", "zero byte", "file is empty", "empty file"),
        )
        or re.search(r"(?<![\w,])0 bytes?\b", normalized_reason) is not None
    ):
        category = ImportSafetyCategory.ZERO_BYTE
        stable_code = normalized_code or "zero_byte_file"
    elif evidence_tokens & _CONTENT_CODES:
        stable_code = (
            "archive_no_pages" if "archive_no_pages" in evidence_tokens else "single_page_comic"
        )
        category = ImportSafetyCategory(stable_code)
    elif evidence_tokens & _SIZE_CODES or _contains_any(
        normalized_reason,
        (
            "decompressed size",
            "decompressionbomberror",
            "decompression bomb",
            "image size exceeds limit",
            "safe rasterization limit",
        ),
    ):
        category = ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT
        stable_code = (
            normalized_code
            if normalized_code in _SIZE_CODES
            else (
                "pillow_decompression_bomb"
                if "decompressionbomb" in normalized_reason
                or "image size exceeds limit" in normalized_reason
                else "archive_decompressed_size_limit"
            )
        )
    elif evidence_tokens & _INSPECTION_CODES or _contains_any(
        normalized_reason,
        (
            "could not be inspected",
            "inspection failed",
            "invalid archive",
            "corrupt archive",
            "bad zip file",
            "badzipfile",
            "not a zip file",
        ),
    ):
        category = ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED
        stable_code = normalized_code or "archive_inspection_failed"
    elif evidence_tokens & _UNSUPPORTED_CODES or _contains_any(
        normalized_reason,
        ("unsupported file type", "unsupported extension", "file type is not supported"),
    ):
        category = ImportSafetyCategory.UNSUPPORTED_FILE_TYPE
        stable_code = normalized_code or "unsupported_file_type"
    elif evidence_tokens & _SOURCE_CHANGED_CODES or _contains_any(
        normalized_reason,
        (
            "changed after scan",
            "changed after it was scanned",
            "source changed",
            "source is unavailable",
            "source became unavailable",
            "signature mismatch",
            "stale source",
        ),
    ):
        category = ImportSafetyCategory.SOURCE_CHANGED
        stable_code = normalized_code or "source_changed"
    else:
        category = ImportSafetyCategory.UNKNOWN
        stable_code = normalized_code or normalized_kind or "unknown_safety_failure"

    overrideable = (
        category
        in {ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT, ImportSafetyCategory.SINGLE_PAGE_COMIC}
        and overrideable_hint is not False
    )
    return ImportSafetyClassification(
        category=category,
        code=stable_code,
        sanitized_reason=_SANITIZED_REASONS[category],
        retryable=category in _RETRYABLE_CATEGORIES,
        overrideable=overrideable,
    )


def build_import_safety_diagnostics(
    reason: str,
    *,
    details: Sequence[str] = (),
    kind: str | None = None,
    code: str | None = None,
    source: str = "file_safety",
    overrideable_hint: bool | None = None,
) -> dict[str, object]:
    """Build a sanitized persisted/review payload from raw safety evidence.

    ``details`` is accepted so callers can pass existing exception evidence,
    but is deliberately not copied into the result because it commonly holds
    host/container absolute paths or unsafe archive member names.
    """
    del details
    classification = classify_import_safety_failure(
        reason,
        kind=kind,
        code=code,
        overrideable_hint=overrideable_hint,
    )
    return {
        "kind": kind or "file_safety_blocked",
        "category": classification.category.value,
        "code": classification.code,
        "reason": classification.sanitized_reason,
        "sanitized_reason": classification.sanitized_reason,
        "source": source,
        "retryable": classification.retryable,
        "overrideable": classification.overrideable,
    }


def normalize_import_safety_diagnostics(
    diagnostics: Mapping[str, object],
    *,
    default_kind: str = "file_safety_blocked",
    default_source: str = "file_safety",
) -> dict[str, object]:
    """Normalize new or legacy safety diagnostics to the safe typed contract."""
    raw_reason = diagnostics.get("sanitized_reason") or diagnostics.get("reason") or ""
    raw_kind = diagnostics.get("kind") or default_kind
    raw_code = diagnostics.get("code") or diagnostics.get("category")
    raw_source = diagnostics.get("source") or default_source
    overrideable_hint = diagnostics.get("overrideable")
    return build_import_safety_diagnostics(
        str(raw_reason),
        kind=str(raw_kind),
        code=str(raw_code) if raw_code is not None else None,
        source=str(raw_source),
        overrideable_hint=(overrideable_hint if isinstance(overrideable_hint, bool) else None),
    )


def _safe_example_name(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    leaf = normalized.rsplit("/", maxsplit=1)[-1]
    safe_leaf = "".join(character for character in leaf if character >= " " and character != "\x7f")
    return safe_leaf or "File"


@dataclass(slots=True)
class _ImportSafetySummaryBucket:
    category: ImportSafetyCategory
    reason: object
    retryable: bool
    overrideable: bool
    count: int = 0
    overrideable_count: int = 0
    codes: set[str] = field(default_factory=set)
    examples: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImportSafetyFailureSummaryAccumulator:
    """Accumulate a complete safety summary with bounded retained detail."""

    example_limit: int = 3
    code_limit: int = 16
    _buckets: dict[ImportSafetyCategory, _ImportSafetySummaryBucket] = field(
        init=False,
        default_factory=dict,
    )

    def add(self, example: str, raw_diagnostics: Mapping[str, object]) -> None:
        """Add one failure while retaining only bounded codes and examples."""
        diagnostics = normalize_import_safety_diagnostics(raw_diagnostics)
        try:
            category = ImportSafetyCategory(str(diagnostics["category"]))
        except ValueError:
            category = ImportSafetyCategory.UNKNOWN
        bucket = self._buckets.get(category)
        if bucket is None:
            bucket = _ImportSafetySummaryBucket(
                category=category,
                reason=diagnostics["sanitized_reason"],
                retryable=bool(diagnostics["retryable"]),
                overrideable=bool(diagnostics["overrideable"]),
            )
            self._buckets[category] = bucket

        bucket.count += 1
        code = str(diagnostics["code"])
        if code in bucket.codes or len(bucket.codes) < max(self.code_limit, 0):
            bucket.codes.add(code)
        safe_example = _safe_example_name(example)
        if (
            len(bucket.examples) < max(self.example_limit, 0)
            and safe_example not in bucket.examples
        ):
            bucket.examples.append(safe_example)
        bucket.retryable = bucket.retryable and bool(diagnostics["retryable"])
        bucket.overrideable = bucket.overrideable and bool(diagnostics["overrideable"])
        if bool(diagnostics["overrideable"]):
            bucket.overrideable_count += 1

    def summaries(self) -> list[dict[str, object]]:
        """Return deterministic category summaries for every accumulated row."""
        summaries: list[dict[str, object]] = []
        for category in ImportSafetyCategory:
            bucket = self._buckets.get(category)
            if bucket is None:
                continue
            summaries.append(
                {
                    "category": category.value,
                    "label": _CATEGORY_LABELS[category],
                    "count": bucket.count,
                    "codes": sorted(bucket.codes),
                    "reason": bucket.reason,
                    "retryable": bucket.retryable,
                    "overrideable": bucket.overrideable,
                    "overrideable_count": bucket.overrideable_count,
                    "examples": bucket.examples,
                    "bulk_overrideable": (
                        category is ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT
                        and bucket.overrideable_count > 0
                    ),
                }
            )
        return summaries


def summarize_import_safety_failures(
    failures: Iterable[tuple[str, Mapping[str, object]]],
    *,
    example_limit: int = 3,
) -> list[dict[str, object]]:
    """Return deterministic category counts with bounded basename-only examples."""
    accumulator = ImportSafetyFailureSummaryAccumulator(example_limit=example_limit)
    for example, raw_diagnostics in failures:
        accumulator.add(example, raw_diagnostics)
    return accumulator.summaries()
