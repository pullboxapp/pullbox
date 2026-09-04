"""Avoid work that cannot possibly discover a duplicate or conflict."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from pullbox.services import import_file_matching as matching


def test_repeated_archive_page_titles_are_parsed_once_without_losing_consensus(monkeypatch):
    from pullbox.core import source_metadata

    original = source_metadata.parse_release_title
    calls = []

    def parse(title):
        calls.append(title)
        return original(title)

    monkeypatch.setattr(source_metadata, "parse_release_title", parse)
    names = [f"Batman 007-{page:03d}.jpg" for page in range(1, 65)]
    hint = source_metadata.archive_entry_issue_hint_from_names(names, expected_series_name="Batman")
    assert hint is not None
    assert hint["issue_number"] == 7
    assert hint["matching_entry_count"] == 64
    assert hint["parseable_image_entries"] == 64
    assert calls == ["Batman 007"]


def test_cached_page_titles_preserve_ambiguity_and_do_not_leak_between_archives():
    from pullbox.core import source_metadata

    names = [f"Batman 007-{page:03d}.jpg" for page in range(1, 17)]
    names += [f"Batman 008-{page:03d}.jpg" for page in range(1, 17)]
    assert (
        source_metadata.archive_entry_issue_hint_from_names(names, expected_series_name="Batman")
        is None
    )
    assert (
        source_metadata.archive_entry_issue_hint_from_names(
            names[:16], expected_series_name="Superman"
        )
        is None
    )


async def test_singleton_issue_cohorts_do_not_load_files_or_commit(monkeypatch):
    async def cohorts(*args, **kwargs):
        yield [SimpleNamespace(file_count=1)]

    monkeypatch.setattr(matching, "_iter_file_target_cohort_batches", cohorts)
    load = AsyncMock(return_value=[SimpleNamespace()])
    monkeypatch.setattr(matching, "_load_file_target_cohort", load)
    session = AsyncMock()
    cancel = AsyncMock()
    detect = AsyncMock(return_value=(0, 3, []))
    result = await matching._finalize_import_series_file_groups(
        session,
        SimpleNamespace(id=1),
        SimpleNamespace(id=2, raw_series_name="Test"),
        duplicate_group_counter=3,
        conflict_group_counter=4,
        detect_duplicate_copies=detect,
        detect_conflicts=lambda *args: (0, 4, []),
        log_event=AsyncMock(),
        raise_if_cancelled=cancel,
    )
    assert result == (3, 4)
    load.assert_not_called()
    session.commit.assert_not_called()
    detect.assert_not_called()
