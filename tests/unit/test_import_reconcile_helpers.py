"""Tests for Step 3 import reconciliation helper decisions."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.issue import Issue, IssueType
from pullbox.schemas.import_job import ImportReconcileDecision
from pullbox.services.import_file_match_targets import (
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND,
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD,
)


def _series() -> ImportedSeries:
    return ImportedSeries(
        id=11,
        import_job_id=4,
        raw_series_name="King Dracula",
        cv_title="King Dracula",
        cv_id=169964,
        user_selected_cv_id=169964,
        cv_issue_count=3,
    )


def _file(*, status: ImportedFileStatus = ImportedFileStatus.NO_MATCH) -> ImportedFile:
    return ImportedFile(
        id=22,
        import_job_id=4,
        import_series_id=11,
        file_path="/imports/King Dracula 04.cbz",
        file_name="King Dracula 04.cbz",
        status=status,
        parsed_issue_number=4.0,
        diagnostics={"existing": "kept"},
    )


def test_apply_reconcile_decisions_assigns_selected_issue() -> None:
    from pullbox.services.import_reconcile_helpers import apply_reconcile_decisions

    item = _series()
    imp_file = _file()
    issue = Issue(id=33, series_id=99, issue_number=4.0, comicvine_id=1116296)
    issue_options = [
        {
            "issue_cv_id": 1116296,
            "issue_number": 4.0,
            "title": "Final Sacrifice",
            "release_date": "2026-06-03",
            "cover_url": "https://example.test/cover.jpg",
            "issue_type": IssueType.ISSUE.value,
        }
    ]

    apply_reconcile_decisions(
        item=item,
        files=[imp_file],
        decisions=[
            ImportReconcileDecision(
                imported_file_id=22,
                action="assign",
                issue_cv_id=1116296,
            )
        ],
        issue_options=issue_options,
        local_issue_by_cv_id={1116296: issue},
        provisional_issue_number_for_file=lambda _item, _file, _options: None,
        provisional_issue_type_for_file=lambda _file: IssueType.ISSUE,
    )

    assert imp_file.status == ImportedFileStatus.MATCHED
    assert imp_file.matched_issue_id == 33
    assert imp_file.matched_issue_cv_id == 1116296
    assert imp_file.match_confidence == "manual"
    assert imp_file.match_method == "import_reconcile"
    assert imp_file.include_in_import is False
    assert imp_file.diagnostics["existing"] == "kept"
    assert imp_file.diagnostics["resolution"] == "assigned"
    assert imp_file.diagnostics["target_issue_summary"]["title"] == "Final Sacrifice"


def test_apply_reconcile_decisions_skips_unresolved_file() -> None:
    from pullbox.services.import_reconcile_helpers import apply_reconcile_decisions

    item = _series()
    imp_file = _file()

    apply_reconcile_decisions(
        item=item,
        files=[imp_file],
        decisions=[ImportReconcileDecision(imported_file_id=22, action="skip")],
        issue_options=[],
        local_issue_by_cv_id={},
        provisional_issue_number_for_file=lambda _item, _file, _options: None,
        provisional_issue_type_for_file=lambda _file: IssueType.ISSUE,
    )

    assert imp_file.status == ImportedFileStatus.SKIPPED
    assert imp_file.matched_issue_id is None
    assert imp_file.matched_issue_cv_id is None
    assert imp_file.match_method == "import_reconcile_skip"
    assert imp_file.include_in_import is False
    assert imp_file.diagnostics["resolution"] == "skipped"


def test_apply_reconcile_decisions_creates_provisional_target() -> None:
    from pullbox.services.import_reconcile_helpers import apply_reconcile_decisions

    item = _series()
    imp_file = _file(status=ImportedFileStatus.PENDING)

    apply_reconcile_decisions(
        item=item,
        files=[imp_file],
        decisions=[
            ImportReconcileDecision(
                imported_file_id=22,
                action="provisional",
                provisional_issue_number=4.0,
            )
        ],
        issue_options=[],
        local_issue_by_cv_id={},
        provisional_issue_number_for_file=lambda _item, _file, _options: 4.0,
        provisional_issue_type_for_file=lambda _file: IssueType.ISSUE,
    )

    assert imp_file.status == ImportedFileStatus.MATCHED
    assert imp_file.matched_issue_id is None
    assert imp_file.matched_issue_cv_id is None
    assert imp_file.match_confidence == "manual"
    assert imp_file.match_method == PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD
    assert imp_file.include_in_import is False
    assert imp_file.diagnostics["existing"] == "kept"
    assert imp_file.diagnostics["kind"] == PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND
    assert imp_file.diagnostics["target_issue_number"] == 4.0
    assert imp_file.diagnostics["target_issue_type"] == IssueType.ISSUE.value


@pytest.mark.asyncio
async def test_reconcile_options_load_local_and_provider_issues() -> None:
    from pullbox.services.import_reconcile_helpers import issue_options_for_reconcile_series

    undetailed = Issue(id=1, series_id=99, issue_number=1.0, comicvine_id=None)
    local = Issue(
        id=2,
        series_id=99,
        issue_number=2.0,
        comicvine_id=200,
        release_date=date(2026, 1, 2),
        issue_type=IssueType.ANNUAL,
    )
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [undetailed, local]))
    session = AsyncMock()
    session.execute.return_value = result
    item = _series()
    item.series_id = 99

    options, local_by_id = await issue_options_for_reconcile_series(session, item, AsyncMock())
    assert options[0]["release_date"] == "2026-01-02"
    assert options[0]["issue_type"] == "annual"
    assert local_by_id == {200: local}

    item.series_id = None
    metadata = AsyncMock()
    metadata.get_issue_summaries_for_series.return_value = [
        SimpleNamespace(provider_id="", issue_number=1.0),
        SimpleNamespace(
            provider_id="300",
            issue_number=3.0,
            title="Three",
            release_date="2026-01-03",
            cover_url=None,
            issue_type="issue",
        ),
    ]
    options, local_by_id = await issue_options_for_reconcile_series(session, item, metadata)
    assert [option["issue_cv_id"] for option in options] == [300]
    assert local_by_id == {}


@pytest.mark.asyncio
async def test_reconcile_options_require_a_selected_series() -> None:
    from pullbox.services.import_reconcile_helpers import issue_options_for_reconcile_series

    item = ImportedSeries(import_job_id=1, raw_series_name="Unknown")
    with pytest.raises(ValidationError, match="Choose a ComicVine match"):
        await issue_options_for_reconcile_series(AsyncMock(), item, AsyncMock())


def test_reconcile_issue_hint_and_type_parsing_fail_closed() -> None:
    from pullbox.services.import_reconcile_helpers import (
        archive_entry_issue_number,
        coerce_issue_type,
        provisional_issue_type_for_file,
    )

    imp_file = _file()
    imp_file.diagnostics = {
        "archive_entry_issue_hint": {"confidence": "strong", "issue_number": "bad"},
        "source_issue_type": "unknown",
        "source_metadata": {"filename_parse": {"issue_type": "annual"}},
    }
    assert archive_entry_issue_number(imp_file) is None
    assert coerce_issue_type("unknown") is None
    assert coerce_issue_type(None) is None
    assert provisional_issue_type_for_file(imp_file) == IssueType.ANNUAL


def test_provisional_issue_number_requires_unresolved_identified_non_conflict_file() -> None:
    from pullbox.services.import_reconcile_helpers import provisional_issue_number_for_file

    item = _series()
    assert (
        provisional_issue_number_for_file(item, _file(status=ImportedFileStatus.MATCHED), [])
        is None
    )

    unknown = ImportedSeries(import_job_id=4, raw_series_name="Unknown")
    assert provisional_issue_number_for_file(unknown, _file(), []) is None

    conflict = _file()
    conflict.diagnostics = {"conflict_type": "identity"}
    assert provisional_issue_number_for_file(item, conflict, []) is None


def test_provisional_issue_number_uses_requested_and_filename_fallbacks() -> None:
    from pullbox.services.import_reconcile_helpers import provisional_issue_number_for_file

    item = _series()
    requested = _file()
    requested.parsed_issue_number = None
    requested.diagnostics = {"requested_issue_number": "4"}
    assert provisional_issue_number_for_file(item, requested, [{"broken": True}]) == 4.0
    assert provisional_issue_number_for_file(item, requested, [{"issue_number": "4"}]) is None

    invalid = _file()
    invalid.file_name = "King Dracula 05.cbz"
    invalid.parsed_issue_number = None
    invalid.diagnostics = {"requested_issue_number": "bad"}
    assert provisional_issue_number_for_file(item, invalid, []) == 5.0


def test_filename_issue_number_requires_parseable_matching_series() -> None:
    from pullbox.services.import_reconcile_helpers import (
        filename_issue_number_for_selected_series,
    )

    item = _series()
    no_number = _file()
    no_number.file_name = "notes.txt"
    assert filename_issue_number_for_selected_series(item, no_number) is None

    item.cv_title = None
    item.raw_series_name = ""
    assert filename_issue_number_for_selected_series(item, _file()) is None

    item.raw_series_name = "Different"
    assert filename_issue_number_for_selected_series(item, _file()) is None


def test_apply_reconcile_decisions_rejects_stale_and_invalid_actions() -> None:
    from pullbox.services.import_reconcile_helpers import apply_reconcile_decisions

    item = _series()
    with pytest.raises(NotFoundError):
        apply_reconcile_decisions(
            item=item,
            files=[_file()],
            decisions=[ImportReconcileDecision(imported_file_id=999, action="skip")],
            issue_options=[],
            local_issue_by_cv_id={},
            provisional_issue_number_for_file=lambda *_args: None,
            provisional_issue_type_for_file=lambda _file: IssueType.ISSUE,
        )

    locked = _file(status=ImportedFileStatus.MATCHED)
    apply_reconcile_decisions(
        item=item,
        files=[locked],
        decisions=[ImportReconcileDecision(imported_file_id=22, action="skip")],
        issue_options=[],
        local_issue_by_cv_id={},
        provisional_issue_number_for_file=lambda *_args: None,
        provisional_issue_type_for_file=lambda _file: IssueType.ISSUE,
    )
    assert locked.status == ImportedFileStatus.MATCHED

    for decision, message, provisional in [
        (
            ImportReconcileDecision(imported_file_id=22, action="provisional"),
            "can only be created",
            None,
        ),
        (
            ImportReconcileDecision(
                imported_file_id=22,
                action="provisional",
                provisional_issue_number=6.0,
            ),
            "does not match",
            5.0,
        ),
        (
            ImportReconcileDecision(imported_file_id=22, action="assign"),
            "require an issue",
            None,
        ),
    ]:
        with pytest.raises(ValidationError, match=message):
            apply_reconcile_decisions(
                item=item,
                files=[_file()],
                decisions=[decision],
                issue_options=[],
                local_issue_by_cv_id={},
                provisional_issue_number_for_file=lambda *_args, value=provisional: value,
                provisional_issue_type_for_file=lambda _file: IssueType.ISSUE,
            )


def test_reconcile_rows_suggest_provider_archive_and_filename_matches() -> None:
    from pullbox.services.import_reconcile_helpers import build_reconcile_file_rows

    item = _series()
    provider = _file()
    provider.comicvine_issue_id = 101
    archive = ImportedFile(
        id=23,
        import_job_id=4,
        import_series_id=11,
        file_path="/imports/King Dracula 05.cbz",
        file_name="King Dracula 05.cbz",
        status=ImportedFileStatus.NO_MATCH,
        diagnostics={
            "archive_entry_issue_hint": {
                "confidence": "strong",
                "issue_number": 5,
            }
        },
    )
    parsed = ImportedFile(
        id=24,
        import_job_id=4,
        import_series_id=11,
        file_path="/imports/King Dracula 06.cbz",
        file_name="King Dracula 06.cbz",
        status=ImportedFileStatus.MATCHED,
        parsed_issue_number=6.0,
    )
    options = [
        {"issue_cv_id": 101, "issue_number": 4.0, "title": "Four"},
        {"issue_cv_id": 102, "issue_number": 5.0, "title": "Five"},
        {"issue_cv_id": 103, "issue_number": 6.0, "title": None},
    ]

    rows, remaining, completed = build_reconcile_file_rows(
        item, [provider, archive, parsed], options
    )
    assert [row["suggested_issue_cv_id"] for row in rows] == [101, 102, 103]
    assert remaining == 2
    assert completed == 1
