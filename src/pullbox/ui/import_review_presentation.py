"""Presentation vocabulary for review; never changes matching or selection."""

LANE_DESCRIPTIONS = {
    "decide": "Choose a match, a copy, or what to do with an unusual file.",
    "confirm": "Pullbox suggested a keeper. Accept it or choose another copy.",
    "fix_source": "Fix the source, then recheck. Ready files do not have to wait.",
    "blocked": "These files cannot import safely. Replace them or leave them out.",
    "ready": "Matched files ready for your library. Choose what to import.",
    "info": "Resolve stale file references by pairing, rechecking, or skipping them.",
    "story_arcs": "Optional reading lists. Canonical comics import independently.",
}

FILTER_LABELS = {
    "needs_series": "Series match",
    "series_conflict": "Series choice",
    "needs_issue": "Issue match",
    "same_comic_review": "Same comic?",
    "single_page_comic": "One-page archives",
    "decompression_size_limit": "Large files",
    "duplicate_copy_confirm": "Duplicate copies",
    "source_missing": "Missing references",
}

REASON_DESCRIPTIONS = {
    "needs_series": "No confident series match. Search ComicVine or choose a candidate.",
    "series_conflict": "More than one series fits. Choose the right one.",
    "needs_issue": "The series is known, but some files still need an issue.",
    "same_comic_review": "Titles or years disagree. Check before choosing a keeper.",
    "single_page_comic": "Cover art, a damaged download, or an intentional one-pager.",
    "decompression_size_limit": "The archive exceeds your configured safety limit.",
    "duplicate_copy_confirm": "Multiple copies of one issue. Keep one for this import.",
    "permission_unreadable": "Pullbox could not read this source. Check access, then recheck.",
    "archive_inspection_failed": (
        "The source could not be inspected. Repair or replace it, then recheck."
    ),
    "source_changed": "The source no longer matches the scan. Verify it again before importing.",
    "outside_approved_root": "Register or map the source in import setup before continuing.",
    "zero_byte": "An empty file cannot contain a comic. Replace it or skip it.",
    "archive_no_pages": "No readable comic pages were found in this archive.",
    "nested_comic_archive": "Review nested comics for source-preserving repair into CBZ.",
    "dangerous_path_or_payload": "Unsafe archive content cannot be allowed.",
    "unsupported_file_type": (
        "This file type is not supported. Convert or replace it, then recheck it."
    ),
    "unknown": (
        "Pullbox could not establish that this file is safe to import. Repair or replace it, "
        "then recheck it."
    ),
    "source_missing": (
        "The saved filename is not on disk. Pair a proven replacement or skip the reference."
    ),
    "ready": "The saved issue matches are ready. Original files remain protected.",
    "preparing_match": "Your decision was saved. Matching continues in the background.",
}
