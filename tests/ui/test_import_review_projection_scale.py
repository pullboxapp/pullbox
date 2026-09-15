"""Large review jobs use saved summaries and only hydrate visible series."""

from __future__ import annotations

import os
import time

from sqlalchemy import insert

from pullbox.models.import_job import (
    ImportedFile,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.ui.import_review_context import load_import_review_context


async def test_review_projection_scales_without_opening_source_files(db_session):
    count = int(os.environ.get("PULLBOX_REVIEW_BENCHMARK_SERIES", "1000"))
    job = ImportJob(
        source_path="/nonexistent/review-benchmark",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add(job)
    await db_session.flush()
    for offset in range(0, count, 500):
        ids = list(range(offset + 1, min(offset + 500, count) + 1))
        await db_session.execute(
            insert(ImportedSeries),
            [
                {
                    "id": sid,
                    "import_job_id": job.id,
                    "raw_series_name": f"Series {sid:06}",
                    "cv_id": sid,
                    "status": "matched",
                    "files_matched": 5,
                    "files_no_match": 1,
                    "files_total": 6,
                    "file_count": 6,
                    "selected_for_import": True,
                }
                for sid in ids
            ],
        )
        await db_session.execute(
            insert(ImportedFile),
            [
                {
                    "import_job_id": job.id,
                    "import_series_id": sid,
                    "file_path": f"/nonexistent/review-benchmark/{sid}/{index}.cbz",
                    "file_name": f"Issue {index}.cbz",
                    "file_format": "cbz",
                    "status": "matched" if index < 5 else "no_match",
                    "diagnostics": {
                        "source_metadata": {"mylar3_issue": {"IssueID": sid * 6 + index}}
                    },
                }
                for sid in ids
                for index in range(6)
            ],
        )
    await db_session.flush()
    started = time.perf_counter()
    context = await load_import_review_context(db_session, job, status="decide", page=2, sort=None)
    elapsed = time.perf_counter() - started
    assert context["lane_counts"]["decide"] == count
    assert sum(context["lane_counts"].values()) == count
    assert len(context["series_items"]) == 25
    assert context["review_summary"]["selected_files_total"] == count * 5
    assert (
        sum(
            len(group["rows"])
            for groups in context["review_file_groups_by_series_id"].values()
            for group in groups
        )
        == 150
    )
    assert not db_session.dirty
    print(f"review projection: {count:,} series / {count * 6:,} files in {elapsed:.3f}s")
