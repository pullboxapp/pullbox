from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast


def _run_benchmark(
    repo_root: Path,
    script: str,
    *extra_args: str,
    files_per_series: int = 1,
) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            script,
            "--series-count",
            "1",
            "--files-per-series",
            str(files_per_series),
            *extra_args,
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for start, char in enumerate(result.stdout):
        if char != "{":
            continue
        try:
            parsed = json.loads(result.stdout[start:])
        except json.JSONDecodeError:
            continue
        assert isinstance(parsed, dict), result.stdout
        return cast("dict[str, object]", parsed)
    raise AssertionError(result.stdout)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_import_scan_benchmark_exits_cleanly() -> None:
    report = _run_benchmark(_repo_root(), "scripts/benchmark_import_scan.py")

    assert report["final_status"] == "review"
    assert report["series_count"] == 1
    assert report["files_per_series"] == 1
    assert report["total_files_matched"] == 1
    assert report["archive_entry_issue_hint_count"] == 1


def test_import_scan_benchmark_uses_stable_provider_ids_for_multi_series() -> None:
    report = _run_benchmark(
        _repo_root(),
        "scripts/benchmark_import_scan.py",
        "--series-count",
        "4",
        files_per_series=4,
    )

    assert report["series_found"] == 4
    assert report["series_matched"] == 4
    assert report["total_files_matched"] == 16
    assert report["total_files_conflict"] == 0
    assert report["archive_entry_issue_hint_count"] == 16


def test_import_scan_benchmark_uses_trusted_folder_metadata_without_provider_calls() -> None:
    report = _run_benchmark(
        _repo_root(),
        "scripts/benchmark_import_scan.py",
        "--trusted-comicinfo",
        "--series-count",
        "4",
        files_per_series=4,
    )

    assert report["trusted_comicinfo"] is True
    assert report["series_matched"] == 4
    assert report["total_files_matched"] == 16
    assert report["archive_read_count"] == 4
    assert report["provider_search_calls"] == 0
    assert report["provider_get_series_calls"] == 0
    assert report["provider_issue_summary_calls"] == 0
    assert report["provider_issue_number_calls"] == 0


def test_import_execute_benchmark_exits_cleanly() -> None:
    report = _run_benchmark(_repo_root(), "scripts/benchmark_import_execute.py")

    assert report["final_status"] == "completed"
    assert report["series_count"] == 1
    assert report["files_per_series"] == 1


def test_import_execute_mixed_file_work_profile_uses_real_registration() -> None:
    report = _run_benchmark(
        _repo_root(),
        "scripts/benchmark_import_execute.py",
        "--file-work-profile",
        "mixed-small",
        files_per_series=3,
    )

    assert report["final_status"] == "completed"
    assert report["file_work_profile"] == "mixed-small"
    assert report["real_file_work"] is True
    assert report["total_files_imported"] == 3
    assert report["total_files_failed"] == 0
    assert report["register_calls"] == 0
    assert report["library_file_count"] == 3
    assert report["source_format_counts"] == {"cb7": 1, "cbr": 1, "cbz": 1}
    assert report["library_format_counts"] == {"cbz": 3}
    library_file_names = cast("list[Any]", report["library_file_names"])
    assert all(str(name).endswith(".cbz") for name in library_file_names)


def test_issue_search_benchmark_exits_cleanly() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_issue_search.py",
            "--result-count",
            "25",
            "--indexer-count",
            "2",
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["final_status"] == "completed"
    assert report["indexer_count"] == 2
    assert report["result_count_per_query"] == 25
    assert report["query_count"] > 0
    assert report["indexer_request_count"] >= report["query_count"]
    assert report["raw_results_count"] > 0
    assert report["filtered_results_count"] == report["raw_results_count"]
    assert report["matched_count"] >= 1
    assert report["rejected_count"] >= 1
    assert report["best_release_title"].startswith("Benchmark Series")


def test_download_progress_benchmark_exits_cleanly() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_download_progress.py",
            "--updates",
            "100",
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["final_status"] == "completed"
    assert report["progress_update_count"] == 100
    assert report["in_memory_write_count"] == 100
    assert report["database_write_count"] == 0


def test_file_transfer_benchmark_exits_cleanly() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_file_transfer.py",
            "--size-mib",
            "1",
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["final_status"] == "completed"
    assert report["bytes_transferred"] == 1024 * 1024
    assert report["progress_callback_count"] >= 2
    assert report["progress_monotonic"] is True
    assert report["cancel_supported"] is False
    assert report["idle_detection_supported"] is False
