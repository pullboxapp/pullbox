"""Benchmark import series-directory classification without filesystem I/O.

Usage:
  .venv/bin/python scripts/benchmark_import_directory_classification.py \
    --series-count 50000 --publisher-count 500
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from pullbox.core.collection_scanner import CollectionScanner
from pullbox.performance.baseline import current_process_peak_rss_bytes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--series-count", type=int, default=10_000)
    parser.add_argument("--publisher-count", type=int, default=100)
    args = parser.parse_args()

    if args.series_count < 1:
        parser.error("--series-count must be at least 1")
    if args.publisher_count < 1:
        parser.error("--publisher-count must be at least 1")

    root = Path("/synthetic-import-root")
    publishers = [root / f"Publisher {index:05d}" for index in range(args.publisher_count)]
    dir_files = {publisher: [publisher / "Publisher Special 001.cbz"] for publisher in publishers}
    for index in range(args.series_count):
        publisher = publishers[index % len(publishers)]
        series = publisher / f"Series {index:06d} (2026)"
        dir_files[series] = [series / "Issue 001.cbz"]

    scanner = CollectionScanner()
    started_at = time.perf_counter()
    series_dirs = scanner._identify_series_dirs(dir_files)
    elapsed = time.perf_counter() - started_at

    if len(series_dirs) != args.series_count:
        msg = f"expected {args.series_count} series directories, found {len(series_dirs)}"
        raise RuntimeError(msg)

    print(
        json.dumps(
            {
                "profile": "directory_classification_only",
                "series_count": args.series_count,
                "publisher_count": args.publisher_count,
                "input_comic_directory_count": len(dir_files),
                "series_directory_count": len(series_dirs),
                "classification_seconds": elapsed,
                "directories_per_second": len(dir_files) / elapsed if elapsed else None,
                "peak_rss_bytes": current_process_peak_rss_bytes(),
                "filesystem_scan_count": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
