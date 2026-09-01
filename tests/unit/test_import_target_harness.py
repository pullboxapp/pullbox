from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from pullbox.performance.baseline import FetchResult
from pullbox.performance.import_target_harness import (
    TARGET_REPORT_SCHEMA_VERSION,
    ImportTargetBackend,
    ImportTargetCacheState,
    ImportTargetConfig,
    ImportTargetScaleProfile,
    ImportTargetSourceLane,
    TargetCommandSample,
    assert_comparable_target_reports,
    build_import_database_parity_report,
    build_import_target_report,
    build_target_workload,
    scale_shape,
)


def _config(tmp_path: Path, **overrides: object) -> ImportTargetConfig:
    values: dict[str, object] = {
        "repo_root": tmp_path,
        "seed": 20260830,
        "backend": ImportTargetBackend.SQLITE,
        "scale_profile": ImportTargetScaleProfile.CI,
        "source_lane": ImportTargetSourceLane.METADATA,
        "cache_state": ImportTargetCacheState.WARM,
        "injection_point": "none",
        "api_urls": (),
        "samples": 2,
        "timeout_seconds": 30.0,
        "environment_label": "linux-4vcpu-8g",
        "filesystem_label": "ssd-ext4",
    }
    values.update(overrides)
    return ImportTargetConfig(**values)  # type: ignore[arg-type]


def _successful_metadata_sample(*, peak_rss_bytes: int = 128 * 1024 * 1024) -> TargetCommandSample:
    return TargetCommandSample(
        report={
            "profile": "metadata_only",
            "represented_file_count": 100,
            "series_count": 25,
            "story_arc_count": 10,
            "seed_elapsed_ms": 10,
            "confirm_elapsed_ms": 5,
            "rollback_elapsed_ms": 8,
            "total_elapsed_ms": 25,
            "confirm_select_count": 4,
            "rollback_select_count": 7,
            "peak_rss_bytes": peak_rss_bytes,
            "database_bytes": 4096,
            "confirmed_arc_count": 10,
            "final_matched_series_count": 25,
            "final_no_match_file_count": 100,
            "final_confirmed_arc_count": 10,
            "provider_call_count": 0,
            "archive_payload_count": 0,
            "filesystem_scan_count": 0,
            # A malicious or accidental path field must never enter the bounded report.
            "private_file_names": ["/mnt/user/private/Secret.cbz"],
        },
        wall_elapsed_ms=30.0,
    )


def test_scale_profiles_freeze_the_required_target_shapes() -> None:
    assert scale_shape(ImportTargetScaleProfile.FILES_10K).file_count == 10_000
    target = scale_shape(ImportTargetScaleProfile.FILES_200K)
    assert target.file_count == 200_000
    assert target.series_count == 50_000
    assert target.story_arc_count == 10_000


def test_target_workload_is_deterministic_for_sqlite_and_postgresql(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        scale_profile=ImportTargetScaleProfile.FILES_10K,
    )

    workload = build_target_workload(config)

    assert workload.command == (
        "scripts/benchmark_import_metadata_scale.py",
        "--series-count",
        "2500",
        "--files-per-series",
        "4",
        "--story-arc-count",
        "500",
    )
    assert workload.capability_failures == ()

    postgres = build_target_workload(_config(tmp_path, backend=ImportTargetBackend.POSTGRESQL))
    assert postgres.command == (
        "scripts/benchmark_import_metadata_scale.py",
        "--series-count",
        "25",
        "--files-per-series",
        "4",
        "--story-arc-count",
        "10",
        "--backend",
        "postgresql",
        "--reset-dedicated-database",
    )
    assert postgres.capability_failures == ()
    assert "postgresql://" not in " ".join(postgres.command)


def test_report_is_bounded_path_free_and_preserves_required_configuration(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    samples = [_successful_metadata_sample(), _successful_metadata_sample()]

    def sample_runner(*_args: object, **_kwargs: object) -> TargetCommandSample:
        return samples.pop(0)

    report = build_import_target_report(config, sample_runner=sample_runner)

    assert report["schema_version"] == TARGET_REPORT_SCHEMA_VERSION
    assert report["settings"] == {
        "seed": 20260830,
        "backend": "sqlite",
        "scale_profile": "ci",
        "source_lane": "metadata",
        "cache_state": "warm",
        "injection_point": "none",
        "samples": 2,
        "timeout_seconds": 30.0,
        "api_urls": [],
    }
    workload = cast("dict[str, object]", report["workload"])
    assert workload["samples_completed"] == 2
    assert workload["sample_report_digests"]
    assert "sample_reports" not in workload
    assert "/mnt/user/private" not in str(report)
    assert cast("dict[str, object]", report["gate_evaluation"])["passed"] is True


def test_hard_memory_stop_returns_a_failed_gate_without_raw_report_data(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, samples=1)

    report = build_import_target_report(
        config,
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(
            peak_rss_bytes=(1024 * 1024 * 1024) + 1
        ),
    )

    gates = cast("dict[str, object]", report["gate_evaluation"])
    assert gates["passed"] is False
    assert "peak_rss_hard_stop_exceeded" in cast("list[str]", gates["hard_failures"])
    assert "/mnt/user/private" not in str(report)


def test_target_profile_fails_closed_when_release_measurements_are_missing(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        scale_profile=ImportTargetScaleProfile.FILES_200K,
        samples=1,
    )

    report = build_import_target_report(
        config,
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(),
    )

    gates = cast("dict[str, object]", report["gate_evaluation"])
    assert gates["passed"] is False
    failures = cast("list[str]", gates["hard_failures"])
    assert "target_shape_mismatch" in failures
    assert "target_api_latency_not_measured" in failures
    assert "target_wal_and_lock_metrics_not_measured" in failures
    assert "target_cancel_restart_recovery_not_measured" in failures


def test_api_5xx_is_a_hard_failure_and_urls_are_bounded(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        samples=1,
        api_urls=("Status=http://pullbox.test/api/v1/import/1",),
    )

    def fetcher(url: str, timeout: float) -> FetchResult:
        assert url == "http://pullbox.test/api/v1/import/1"
        assert timeout == 30.0
        return FetchResult(status_code=503, elapsed_ms=12.0, content_length=5)

    report = build_import_target_report(
        config,
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(),
        api_fetcher=fetcher,
    )

    gates = cast("dict[str, object]", report["gate_evaluation"])
    assert "api_5xx_response" in cast("list[str]", gates["hard_failures"])


def test_api_configuration_strips_queries_and_rejects_embedded_credentials(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        api_urls=("Status=https://pullbox.test/api/import/1?token=private#fragment",),
    )
    report = build_import_target_report(
        config,
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(),
        api_fetcher=lambda _url, _timeout: FetchResult(200, 1.0, 1),
    )

    settings = cast("dict[str, object]", report["settings"])
    assert settings["api_urls"] == ["https://pullbox.test/api/import/1"]
    assert "private" not in str(report)
    with pytest.raises(ValueError, match="credentials"):
        _config(tmp_path, api_urls=("https://admin:password@pullbox.test/ping",))


def test_report_does_not_retain_absolute_runtime_or_child_paths(tmp_path: Path) -> None:
    report = build_import_target_report(
        _config(tmp_path, samples=1),
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(),
    )

    assert str(tmp_path) not in str(report)
    workload = cast("dict[str, object]", report["workload"])
    assert workload["command"] == [
        "scripts/benchmark_import_metadata_scale.py",
        "--series-count",
        "25",
        "--files-per-series",
        "4",
        "--story-arc-count",
        "10",
    ]


def test_target_api_probe_failure_is_a_hard_gate(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        scale_profile=ImportTargetScaleProfile.FILES_200K,
        samples=1,
        api_urls=("Status=https://pullbox.test/api/import/1",),
    )

    report = build_import_target_report(
        config,
        sample_runner=lambda *_args, **_kwargs: TargetCommandSample(
            report={
                **cast("dict[str, object]", _successful_metadata_sample().report),
                "represented_file_count": 200_000,
                "series_count": 50_000,
                "wal_bytes": 0,
                "transaction_p99_ms": 1,
                "lock_wait_p99_ms": 0,
                "cancel_latency_ms": 1,
                "restart_latency_ms": 1,
                "rollback_recovery_ms": 1,
                "orphan_discrepancy_count": 0,
            },
            wall_elapsed_ms=30,
        ),
        api_fetcher=lambda _url, _timeout: (_ for _ in ()).throw(
            OSError("private transport detail")
        ),
    )

    failures = cast(
        "list[str]",
        cast("dict[str, object]", report["gate_evaluation"])["hard_failures"],
    )
    assert "api_measurement_incomplete" in failures
    assert "api_timing_not_measured" in failures
    assert "private transport detail" not in str(report)


def test_comparison_rejects_unlike_backend_hardware_or_filesystem(tmp_path: Path) -> None:
    left = build_import_target_report(
        _config(tmp_path, samples=1),
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(),
    )
    right = build_import_target_report(
        _config(tmp_path, samples=1),
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(),
    )
    assert_comparable_target_reports(left, right)

    mismatched = cast("dict[str, object]", {**right})
    mismatched_context = dict(cast("dict[str, object]", right["context"]))
    mismatched_context["filesystem_label"] = "different-device"
    mismatched["context"] = mismatched_context
    with pytest.raises(ValueError, match="filesystem_label"):
        assert_comparable_target_reports(left, mismatched)


def test_database_parity_report_requires_matching_semantic_results(tmp_path: Path) -> None:
    sqlite_report = build_import_target_report(
        _config(tmp_path, samples=1),
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(),
    )
    postgresql_report = build_import_target_report(
        _config(tmp_path, samples=1, backend=ImportTargetBackend.POSTGRESQL),
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(),
    )

    parity = build_import_database_parity_report(sqlite_report, postgresql_report)

    assert parity["passed"] is True
    assert parity["backends"] == ["postgresql", "sqlite"]
    assert parity["hard_failures"] == []
    assert "/private/" not in str(parity)

    workload = cast("dict[str, object]", postgresql_report["workload"])
    metrics = cast("dict[str, object]", workload["metrics"])
    matched = cast("dict[str, object]", metrics["final_matched_series_count"])
    matched["median"] = 24.0

    mismatch = build_import_database_parity_report(sqlite_report, postgresql_report)

    assert mismatch["passed"] is False
    assert "semantic_metric_mismatch_final_matched_series_count" in cast(
        "list[str]", mismatch["hard_failures"]
    )


def test_database_parity_is_separate_from_incomplete_release_probes(tmp_path: Path) -> None:
    sqlite_report = build_import_target_report(
        _config(tmp_path, samples=1),
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(),
    )
    postgresql_report = build_import_target_report(
        _config(tmp_path, samples=1, backend=ImportTargetBackend.POSTGRESQL),
        sample_runner=lambda *_args, **_kwargs: _successful_metadata_sample(),
    )
    for report in (sqlite_report, postgresql_report):
        report["gate_evaluation"] = {
            "passed": False,
            "hard_failures": ["target_cancel_restart_recovery_not_measured"],
            "warnings": [],
        }

    parity = build_import_database_parity_report(sqlite_report, postgresql_report)

    assert parity["passed"] is True
    assert parity["hard_failures"] == []
    assert parity["warnings"] == ["source_release_gates_not_complete"]


def test_target_harness_cli_writes_a_gate_evaluated_smoke_report(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "target-report.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_import_target.py",
            "--repo-root",
            str(repo_root),
            "--seed",
            "20260830",
            "--backend",
            "sqlite",
            "--scale-profile",
            "ci",
            "--source-lane",
            "metadata",
            "--cache-state",
            "warm",
            "--samples",
            "1",
            "--timeout",
            "30",
            "--environment-label",
            "test-host",
            "--filesystem-label",
            "test-filesystem",
            "--output",
            str(output_path),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["settings"]["seed"] == 20260830
    assert report["shape"]["file_count"] == 100
    assert report["workload"]["samples_completed"] == 1
    assert report["gate_evaluation"]["passed"] is True
