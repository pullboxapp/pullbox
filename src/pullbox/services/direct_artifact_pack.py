"""Safe extraction of separable same-series direct-download packs."""

from __future__ import annotations

from pathlib import Path

from pullbox.core.archive import ArchiveError, ArchiveReader
from pullbox.core.file_safety import has_archive_member_path_traversal
from pullbox.core.issue_numbers import format_issue_number, parse_issue_number_text
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.release_parser import parse_release_title

_COMIC_FILE_SUFFIXES = frozenset({".cbz", ".cbr", ".cb7", ".cbt", ".pdf"})


class DirectArtifactPackError(RuntimeError):
    """A stable, user-facing direct-pack post-processing failure."""

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def is_separable_issue_pack(source_path: Path) -> bool:
    """Return whether an archive safely presents multiple named comic members.

    This is deliberately structural only. Series identity and complete claimed
    coverage are enforced by ``extract_same_series_issue_files`` before import.
    """
    try:
        members = ArchiveReader(source_path).list_members()
    except ArchiveError:
        return False
    candidates = [
        member
        for member in members
        if member.is_regular_file
        and not member.is_link
        and Path(member.name).suffix.lower() in _COMIC_FILE_SUFFIXES
        and not has_archive_member_path_traversal(member.name)
        and _issue_number_from_member(member.name) is not None
    ]
    return len(candidates) >= 2


def extract_same_series_issue_files(
    source_path: Path,
    *,
    destination: Path,
    expected_issue_numbers: frozenset[str],
    expected_series_titles: frozenset[str],
) -> dict[str, Path]:
    """Extract separately packaged issues from one contiguous direct-download pack.

    A pack with page images but no nested comic files is one combined comic file.
    Pullbox intentionally will not guess page boundaries or split that file.
    """
    if len(expected_issue_numbers) < 2:
        return {}

    expected = _normalized_issue_numbers(expected_issue_numbers)
    normalized_series_titles = {
        normalized
        for title in expected_series_titles
        if (normalized := NameMatcher.normalize(title))
    }
    if not normalized_series_titles:
        raise DirectArtifactPackError(
            code="direct_pack_series_invalid",
            message="The direct-download pack has no reliable series identity.",
        )
    reader = ArchiveReader(source_path)
    try:
        members = reader.list_members()
    except ArchiveError as exc:
        raise DirectArtifactPackError(
            code="direct_pack_unreadable",
            message="Pullbox could not inspect the downloaded direct-download pack.",
        ) from exc

    candidates = [
        member
        for member in members
        if member.is_regular_file
        and not member.is_link
        and Path(member.name).suffix.lower() in _COMIC_FILE_SUFFIXES
    ]
    if not candidates:
        raise DirectArtifactPackError(
            code="direct_pack_combined_file",
            message=(
                "This direct-download pack contains multiple issues in one combined comic "
                "file. Pullbox can only import packs with separate issue files."
            ),
        )

    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    extracted: dict[str, Path] = {}
    for member in candidates:
        if has_archive_member_path_traversal(member.name):
            raise DirectArtifactPackError(
                code="direct_pack_unsafe_member",
                message="The direct-download pack contains an unsafe archive member path.",
            )
        parsed = parse_release_title(Path(member.name).name)
        issue_number = parsed.issue_number if parsed is not None else None
        issue_number_text = parsed.issue_number_text if parsed is not None else None
        if issue_number is None:
            continue
        exact_issue_number = issue_number_text or format_issue_number(issue_number)
        if exact_issue_number not in expected:
            continue
        parsed_series_title = NameMatcher.normalize(parsed.series_name or "") if parsed else ""
        if parsed_series_title not in normalized_series_titles:
            raise DirectArtifactPackError(
                code="direct_pack_mixed_series",
                message="The direct-download pack contains files for a different series.",
            )
        if exact_issue_number in extracted:
            raise DirectArtifactPackError(
                code="direct_pack_ambiguous_issue",
                message="The direct-download pack contains more than one file for the same issue.",
            )
        suffix = Path(member.name).suffix.lower()
        target = destination / f"issue-{_issue_path_token(exact_issue_number)}{suffix}"
        try:
            target.write_bytes(reader.read_file(member.name, max_bytes=member.size))
            target.chmod(0o600)
        except (ArchiveError, OSError) as exc:
            raise DirectArtifactPackError(
                code="direct_pack_extract_failed",
                message="Pullbox could not safely extract the direct-download pack.",
            ) from exc
        extracted[exact_issue_number] = target

    missing = expected - set(extracted)
    if missing:
        raise DirectArtifactPackError(
            code="direct_pack_incomplete",
            message="The direct-download pack does not contain every issue it claimed to cover.",
        )
    return extracted


def _normalized_issue_numbers(issue_numbers: frozenset[str]) -> set[str]:
    normalized: set[str] = set()
    for value in issue_numbers:
        try:
            _, exact_text = parse_issue_number_text(value)
            normalized.add(exact_text)
        except (TypeError, ValueError) as exc:
            raise DirectArtifactPackError(
                code="direct_pack_coverage_invalid",
                message="The direct-download pack has invalid issue coverage.",
            ) from exc
    return normalized


def _issue_path_token(issue_number: str | float) -> str:
    try:
        _, exact_text = parse_issue_number_text(issue_number)
    except ValueError:
        exact_text = (
            format_issue_number(issue_number)
            if isinstance(issue_number, float)
            else str(issue_number)
        )
    return exact_text.replace(".", "_")


def _issue_number_from_member(name: str) -> float | None:
    parsed = parse_release_title(Path(name).name)
    return parsed.issue_number if parsed is not None else None
