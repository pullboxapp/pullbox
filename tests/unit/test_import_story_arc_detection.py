"""Conservative story-arc classification for folder-import evidence."""

from __future__ import annotations

from pullbox.services.import_story_arc_detection import (
    FolderArcClassification,
    FolderArcFileEvidence,
    detect_folder_story_arc,
)


def _file(
    relative_path: str,
    series: str,
    *,
    arc: str | None = None,
    order: str | None = None,
) -> FolderArcFileEvidence:
    return FolderArcFileEvidence(
        relative_path=relative_path,
        series=series,
        issue_number="1",
        story_arc=arc,
        story_arc_number=order,
    )


def test_consistent_comicinfo_arc_identity_is_strong_evidence() -> None:
    result = detect_folder_story_arc(
        folder_label="Batman - The Court of Owls",
        files=(
            _file("001 - Batman 001.cbz", "Batman", arc="Court of Owls", order="1"),
            _file(
                "002 - Nightwing 001.cbz",
                "Nightwing",
                arc="Court of Owls",
                order="2",
            ),
        ),
    )

    assert result.classification == FolderArcClassification.STORY_ARC
    assert result.proposed_name == "Court of Owls"
    assert result.provider_calls_required is False


def test_conflicting_exact_comicinfo_arc_names_require_review() -> None:
    result = detect_folder_story_arc(
        folder_label="Mixed",
        files=(
            _file("001 - Batman.cbz", "Batman", arc="Court of Owls", order="1"),
            _file("002 - Nightwing.cbz", "Nightwing", arc="Night of Owls", order="2"),
        ),
    )

    assert result.classification == FolderArcClassification.NEEDS_REVIEW
    assert result.reason == "conflicting_exact_arc_names"


def test_unlabelled_mixed_series_folder_is_not_silently_an_arc() -> None:
    result = detect_folder_story_arc(
        folder_label="Incoming",
        files=(
            _file("Batman 001.cbz", "Batman"),
            _file("Nightwing 001.cbz", "Nightwing"),
        ),
    )

    assert result.classification == FolderArcClassification.NORMAL_MIXED_FOLDER
    assert result.proposed_name is None


def test_reading_order_prefixes_alone_are_review_evidence_not_arc_identity() -> None:
    result = detect_folder_story_arc(
        folder_label="Court of Owls",
        files=(
            _file("001 - Batman 001.cbz", "Batman", order="1"),
            _file("002 - Nightwing 001.cbz", "Nightwing", order="2"),
        ),
    )

    assert result.classification == FolderArcClassification.NEEDS_REVIEW
    assert result.reason == "ordered_mixed_folder_requires_confirmation"


def test_confirmed_order_pattern_promotes_folder_without_provider_calls() -> None:
    result = detect_folder_story_arc(
        folder_label="Court of Owls",
        files=(
            _file("001 - Batman 001.cbz", "Batman", order="1"),
            _file("002 - Nightwing 001.cbz", "Nightwing", order="2"),
        ),
        confirmed_order_pattern=True,
    )

    assert result.classification == FolderArcClassification.STORY_ARC
    assert result.proposed_name == "Court of Owls"
    assert result.provider_calls_required is False


def test_duplicate_comicinfo_orders_remain_reviewable() -> None:
    result = detect_folder_story_arc(
        folder_label="Court of Owls",
        files=(
            _file("Batman 001.cbz", "Batman", arc="Court of Owls", order="1"),
            _file("Nightwing 001.cbz", "Nightwing", arc="Court of Owls", order="1"),
        ),
    )

    assert result.classification == FolderArcClassification.NEEDS_REVIEW
    assert result.reason == "duplicate_arc_order"
