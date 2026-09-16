"""ComicInfo story-arc evidence must reuse bounded archive inspection."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pullbox.core.archive import ArchiveReader
from pullbox.models.import_job import ImportedFile, ImportedSeries
from pullbox.services.import_source_metadata import (
    load_deferred_source_metadata_for_import_file,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_cached_comicinfo_story_arc_evidence_reaches_import_diagnostics_without_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = tmp_path / "Batman 001.cbz"
    file_path.write_bytes(b"cached evidence means this payload must not be opened")
    imp_series = ImportedSeries(raw_series_name="Batman", raw_year=2011)
    imp_file = ImportedFile(
        file_path=str(file_path),
        file_name=file_path.name,
        parsed_series="Batman",
        parsed_issue_number=1.0,
        parsed_year=2011,
        diagnostics={
            "archive_member_evidence": {
                "member_index_scanned": True,
                "comicinfo_entry_count": 1,
                "comicinfo_entry": "ComicInfo.xml",
                "comicinfo": {
                    "series": "Batman",
                    "number": "1",
                    "story_arc": "Batman: The Court of Owls",
                    "story_arc_number": "001.50-A",
                },
            },
            "source_metadata": {
                "archive_metadata_loaded": False,
                "archive_metadata_deferred": True,
            },
        },
    )

    def fail_archive_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("cached ComicInfo evidence must avoid reopening the archive")

    monkeypatch.setattr(ArchiveReader, "list_files", fail_archive_read)
    monkeypatch.setattr(ArchiveReader, "read_file", fail_archive_read)

    metadata = await load_deferred_source_metadata_for_import_file(imp_series, imp_file)

    assert metadata.diagnostics["archive_member_index_reused"] is True
    assert metadata.diagnostics["comicinfo"]["story_arc"] == ("Batman: The Court of Owls")
    assert metadata.diagnostics["comicinfo"]["story_arc_number"] == "001.50-A"
