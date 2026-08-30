"""Typed, sanitized import safety diagnostics contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pullbox.services.import_safety_diagnostics import (
    ImportSafetyCategory,
    build_import_safety_diagnostics,
    classify_import_safety_failure,
    summarize_import_safety_failures,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


@pytest.mark.parametrize(
    (
        "reason",
        "kind",
        "code",
        "expected_category",
        "expected_code",
        "retryable",
        "overrideable",
    ),
    [
        (
            "PermissionError: [Errno 13] /mnt/private/Batman 001.cbz",
            None,
            None,
            ImportSafetyCategory.PERMISSION_UNREADABLE,
            "permission_denied",
            True,
            False,
        ),
        (
            "Archive could not be inspected: /mnt/private/corrupt.cbz",
            None,
            None,
            ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED,
            "archive_inspection_failed",
            True,
            False,
        ),
        (
            "File is empty (0 bytes): C:\\private\\empty.cbz",
            None,
            None,
            ImportSafetyCategory.ZERO_BYTE,
            "zero_byte_file",
            True,
            False,
        ),
        (
            "Archive decompressed size exceeds limit: /mnt/private/large.cbz",
            "archive_decompressed_size",
            None,
            ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT,
            "archive_decompressed_size_limit",
            False,
            True,
        ),
        (
            "Archive contains path traversal entry ../../secret",
            None,
            None,
            ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD,
            "dangerous_archive_path",
            False,
            False,
        ),
        (
            "File resolves outside enabled library root: /mnt/private/file.cbz",
            None,
            "source_outside_root",
            ImportSafetyCategory.OUTSIDE_APPROVED_ROOT,
            "source_outside_root",
            True,
            False,
        ),
        (
            "Unsupported file type: /mnt/private/file.rar.exe",
            None,
            None,
            ImportSafetyCategory.UNSUPPORTED_FILE_TYPE,
            "unsupported_file_type",
            False,
            False,
        ),
        (
            "Referenced file changed after scan: /mnt/private/file.cbz",
            None,
            "source_changed",
            ImportSafetyCategory.SOURCE_CHANGED,
            "source_changed",
            True,
            False,
        ),
    ],
)
def test_classify_import_safety_failure_returns_stable_typed_policy(
    reason: str,
    kind: str | None,
    code: str | None,
    expected_category: ImportSafetyCategory,
    expected_code: str,
    retryable: bool,
    overrideable: bool,
) -> None:
    result = classify_import_safety_failure(
        reason,
        kind=kind,
        code=code,
        overrideable_hint=True,
    )

    assert result.category is expected_category
    assert result.code == expected_code
    assert result.retryable is retryable
    assert result.overrideable is overrideable
    assert "/mnt/private" not in result.sanitized_reason
    assert "C:\\private" not in result.sanitized_reason


def test_dangerous_findings_never_become_overrideable_from_untrusted_hint() -> None:
    result = classify_import_safety_failure(
        "Archive contains a dangerous executable payload",
        kind="archive_decompressed_size",
        code="dangerous_payload",
        overrideable_hint=True,
    )

    assert result.category is ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD
    assert result.overrideable is False


def test_build_import_safety_diagnostics_drops_raw_paths_and_preserves_review_contract() -> None:
    diagnostics = build_import_safety_diagnostics(
        "Archive could not be inspected: /mnt/user/private/secret.cbz",
        details=["/mnt/user/private/secret.cbz", "C:\\private\\secret.cbz"],
        source="file_safety",
    )

    assert diagnostics == {
        "kind": "file_safety_blocked",
        "category": "archive_inspection_failed",
        "code": "archive_inspection_failed",
        "reason": (
            "Pullbox could not inspect this archive. It may be corrupt, incomplete, "
            "or temporarily unavailable."
        ),
        "sanitized_reason": (
            "Pullbox could not inspect this archive. It may be corrupt, incomplete, "
            "or temporarily unavailable."
        ),
        "source": "file_safety",
        "retryable": True,
        "overrideable": False,
    }
    assert "/mnt/user" not in str(diagnostics)
    assert "C:\\private" not in str(diagnostics)


def test_summarize_import_safety_failures_counts_categories_and_bounds_examples() -> None:
    size_block = build_import_safety_diagnostics(
        "Archive decompressed size exceeds limit",
        kind="archive_decompressed_size",
    )
    dangerous_block = build_import_safety_diagnostics(
        "Archive contains path traversal entries",
        overrideable_hint=True,
    )
    failures: list[tuple[str, Mapping[str, object]]] = [
        ("/mnt/private/Omnibus 1.cbz", size_block),
        ("C:\\private\\Omnibus 2.cbz", size_block),
        ("/mnt/private/Omnibus 3.cbz", size_block),
        ("/mnt/private/Omnibus 4.cbz", size_block),
        ("/mnt/private/Unsafe.cbz", dangerous_block),
    ]

    summary = summarize_import_safety_failures(failures, example_limit=3)

    assert summary == [
        {
            "category": "decompression_size_limit",
            "label": "Decompression-size limit",
            "count": 4,
            "codes": ["archive_decompressed_size_limit"],
            "reason": "The archive exceeds Pullbox's configured decompressed-size limit.",
            "retryable": False,
            "overrideable": True,
            "overrideable_count": 4,
            "bulk_overrideable": True,
            "examples": ["Omnibus 1.cbz", "Omnibus 2.cbz", "Omnibus 3.cbz"],
        },
        {
            "category": "dangerous_path_or_payload",
            "label": "Dangerous link, path, or payload",
            "count": 1,
            "codes": ["dangerous_archive_path"],
            "reason": (
                "The file contains a dangerous path, link, or payload and cannot be overridden."
            ),
            "retryable": False,
            "overrideable": False,
            "overrideable_count": 0,
            "bulk_overrideable": False,
            "examples": ["Unsafe.cbz"],
        },
    ]
    assert "/mnt/private" not in str(summary)
    assert "C:\\private" not in str(summary)


def test_summarize_import_safety_failures_exposes_partial_bulk_eligibility() -> None:
    eligible = build_import_safety_diagnostics(
        "Archive decompressed size exceeds limit",
        kind="archive_decompressed_size",
    )
    ineligible = {**eligible, "overrideable": False}

    summary = summarize_import_safety_failures(
        [
            ("/private/Eligible.cbz", eligible),
            ("/private/Ineligible.cbz", ineligible),
        ]
    )

    assert len(summary) == 1
    assert summary[0]["overrideable"] is False
    assert summary[0]["overrideable_count"] == 1
    assert summary[0]["bulk_overrideable"] is True
