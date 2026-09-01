"""Standard, bounded reporting contract for IU7 import target measurements.

The harness deliberately distinguishes a runnable measurement lane from a
complete release proof.  Existing deterministic benchmark scripts can supply
bounded stage/counter evidence today; target-scale reports fail closed until
the API, WAL/lock, cancellation, restart, and rollback probes are present.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pullbox.performance.baseline import (
    EndpointSpec,
    Fetcher,
    collect_context,
    default_fetcher,
    measure_http_endpoint,
    parse_endpoint_spec,
    summarize_numbers,
)
from pullbox.performance.direct_download_baseline import extract_json_report

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

TARGET_REPORT_SCHEMA_VERSION = "1.0"
_MAX_API_URLS = 20
_MAX_FAILURES = 50
_PEAK_RSS_TARGET_BYTES = 768 * 1024 * 1024
_PEAK_RSS_HARD_STOP_BYTES = 1024 * 1024 * 1024


class ImportTargetBackend(StrEnum):
    """Database backend selected for one target measurement."""

    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class ImportTargetScaleProfile(StrEnum):
    """Frozen deterministic scale profiles used by IU7."""

    CI = "ci"
    FILES_10K = "10k"
    FILES_50K = "50k"
    FILES_100K = "100k"
    FILES_200K = "200k"


class ImportTargetSourceLane(StrEnum):
    """Import lifecycle lane measured by the child benchmark."""

    METADATA = "metadata"
    FOLDER = "folder"
    MYLAR3 = "mylar3"
    MANAGED_COPY = "managed_copy"


class ImportTargetCacheState(StrEnum):
    """Declared warm/cold state for a repeatable run."""

    WARM = "warm"
    COLD = "cold"


@dataclass(frozen=True, slots=True)
class ImportTargetScaleShape:
    """Exact synthetic shape represented by one named profile."""

    file_count: int
    series_count: int
    story_arc_count: int

    @property
    def files_per_series(self) -> int:
        return self.file_count // self.series_count


_SCALE_SHAPES: dict[ImportTargetScaleProfile, ImportTargetScaleShape] = {
    ImportTargetScaleProfile.CI: ImportTargetScaleShape(100, 25, 10),
    ImportTargetScaleProfile.FILES_10K: ImportTargetScaleShape(10_000, 2_500, 500),
    ImportTargetScaleProfile.FILES_50K: ImportTargetScaleShape(50_000, 12_500, 2_500),
    ImportTargetScaleProfile.FILES_100K: ImportTargetScaleShape(100_000, 25_000, 5_000),
    ImportTargetScaleProfile.FILES_200K: ImportTargetScaleShape(200_000, 50_000, 10_000),
}


@dataclass(frozen=True, slots=True)
class ImportTargetConfig:
    """Complete non-secret input contract for one harness invocation."""

    repo_root: Path
    seed: int
    backend: ImportTargetBackend
    scale_profile: ImportTargetScaleProfile
    source_lane: ImportTargetSourceLane
    cache_state: ImportTargetCacheState
    injection_point: str
    api_urls: tuple[str, ...]
    samples: int
    timeout_seconds: float
    environment_label: str
    filesystem_label: str

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if self.samples < 1 or self.samples > 20:
            raise ValueError("samples must be between 1 and 20")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not self.environment_label.strip() or len(self.environment_label) > 100:
            raise ValueError("environment_label must contain 1 to 100 characters")
        if not self.filesystem_label.strip() or len(self.filesystem_label) > 100:
            raise ValueError("filesystem_label must contain 1 to 100 characters")
        if not self.injection_point.strip() or len(self.injection_point) > 100:
            raise ValueError("injection_point must contain 1 to 100 characters")
        if len(self.api_urls) > _MAX_API_URLS:
            raise ValueError(f"api_urls cannot contain more than {_MAX_API_URLS} entries")
        for raw in self.api_urls:
            _sanitized_endpoint_spec(raw)


@dataclass(frozen=True, slots=True)
class ImportTargetWorkload:
    """One deterministic child command and any known capability blockers."""

    command: tuple[str, ...]
    capability_failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TargetCommandSample:
    """One isolated child result retained only until it is summarized."""

    report: dict[str, object] | None
    wall_elapsed_ms: float
    error: str | None = None


class TargetSampleRunner(Protocol):
    """Injectable command boundary used by unit tests and the CLI."""

    def __call__(
        self,
        workload: ImportTargetWorkload,
        config: ImportTargetConfig,
    ) -> TargetCommandSample: ...


def scale_shape(profile: ImportTargetScaleProfile) -> ImportTargetScaleShape:
    """Return the immutable shape for one named profile."""
    return _SCALE_SHAPES[profile]


def build_target_workload(config: ImportTargetConfig) -> ImportTargetWorkload:
    """Map a complete harness configuration to one shell-free child command."""
    failures: list[str] = []
    if (
        config.backend is ImportTargetBackend.POSTGRESQL
        and config.source_lane is not ImportTargetSourceLane.METADATA
    ):
        failures.append("postgresql_source_lane_not_instrumented")
    if config.injection_point != "none":
        failures.append("cancel_restart_injection_not_instrumented")
    if failures:
        return ImportTargetWorkload(command=(), capability_failures=tuple(failures))

    shape = scale_shape(config.scale_profile)
    common = (
        "--series-count",
        str(shape.series_count),
        "--files-per-series",
        str(shape.files_per_series),
    )
    command: tuple[str, ...]
    if config.source_lane is ImportTargetSourceLane.METADATA:
        command = (
            "scripts/benchmark_import_metadata_scale.py",
            *common,
            "--story-arc-count",
            str(shape.story_arc_count),
        )
        if config.backend is ImportTargetBackend.POSTGRESQL:
            command = (
                *command,
                "--backend",
                "postgresql",
                "--reset-dedicated-database",
            )
    elif config.source_lane is ImportTargetSourceLane.FOLDER:
        command = ("scripts/benchmark_import_scan.py", *common)
    elif config.source_lane is ImportTargetSourceLane.MYLAR3:
        command = (
            "scripts/benchmark_mylar3_import.py",
            *common,
            "--annual-count",
            str(max(shape.series_count // 10, 1)),
        )
    else:
        command = (
            "scripts/benchmark_import_execute.py",
            *common,
            "--file-work-profile",
            "mixed-small",
            "--report-sample-limit",
            "3",
        )
    return ImportTargetWorkload(command=command)


def run_target_command_sample(
    workload: ImportTargetWorkload,
    config: ImportTargetConfig,
) -> TargetCommandSample:
    """Run one child measurement without shell interpolation or secret capture."""
    if not workload.command:
        return TargetCommandSample(
            report=None,
            wall_elapsed_ms=0.0,
            error="workload has unresolved capability failures",
        )
    environment = os.environ.copy()
    environment["PULLBOX_IMPORT_BENCHMARK_SEED"] = str(config.seed)
    environment["PULLBOX_IMPORT_BENCHMARK_CACHE_STATE"] = config.cache_state.value
    started_at = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, *workload.command],
            cwd=config.repo_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TargetCommandSample(
            report=None,
            wall_elapsed_ms=(time.perf_counter() - started_at) * 1000,
            error=f"timed out after {config.timeout_seconds:g}s",
        )
    except OSError as exc:
        return TargetCommandSample(
            report=None,
            wall_elapsed_ms=(time.perf_counter() - started_at) * 1000,
            error=f"child_launch_error_{type(exc).__name__}",
        )

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    if result.returncode != 0:
        # Child output may contain fixture paths. Retain only a fixed error code.
        return TargetCommandSample(
            report=None,
            wall_elapsed_ms=elapsed_ms,
            error=f"child_exit_{result.returncode}",
        )
    try:
        report = extract_json_report(result.stdout)
    except ValueError:
        return TargetCommandSample(
            report=None,
            wall_elapsed_ms=elapsed_ms,
            error="child_report_invalid",
        )
    return TargetCommandSample(report=report, wall_elapsed_ms=elapsed_ms)


_NUMERIC_FIELDS = (
    "represented_file_count",
    "series_count",
    "story_arc_count",
    "confirmed_arc_count",
    "final_matched_series_count",
    "final_no_match_file_count",
    "final_confirmed_arc_count",
    "elapsed_ms",
    "total_elapsed_ms",
    "seed_elapsed_ms",
    "confirm_elapsed_ms",
    "rollback_elapsed_ms",
    "peak_rss_bytes",
    "database_bytes",
    "wal_bytes",
    "confirm_select_count",
    "rollback_select_count",
    "query_count",
    "transaction_p95_ms",
    "transaction_p99_ms",
    "lock_wait_p95_ms",
    "lock_wait_p99_ms",
    "filesystem_scan_count",
    "archive_payload_count",
    "archive_safety_inspection_count",
    "archive_member_list_read_count",
    "archive_member_payload_read_count",
    "provider_call_count",
    "provider_search_calls",
    "provider_get_series_calls",
    "provider_issue_summary_calls",
    "provider_issue_number_calls",
    "progress_event_count",
    "progress_write_count",
    "cancel_latency_ms",
    "restart_latency_ms",
    "rollback_recovery_ms",
    "orphan_discrepancy_count",
    "source_mutation_count",
)

_PARITY_SEMANTIC_METRICS = (
    "represented_file_count",
    "series_count",
    "story_arc_count",
    "confirmed_arc_count",
    "final_matched_series_count",
    "final_no_match_file_count",
    "final_confirmed_arc_count",
    "archive_payload_count",
    "provider_call_count",
    "filesystem_scan_count",
    "confirm_select_count",
    "rollback_select_count",
)


def _numeric_value(report: Mapping[str, object], field: str) -> float | None:
    value = report.get(field)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _summarize_samples(
    workload: ImportTargetWorkload,
    samples: Sequence[TargetCommandSample],
) -> dict[str, object]:
    successful = [sample for sample in samples if sample.report is not None]
    failures = [
        sample.error or "child_report_missing" for sample in samples if sample.report is None
    ][:_MAX_FAILURES]
    metrics: dict[str, dict[str, float | int]] = {}
    for field in _NUMERIC_FIELDS:
        values = [
            numeric
            for sample in successful
            if sample.report is not None
            and (numeric := _numeric_value(sample.report, field)) is not None
        ]
        if values:
            metrics[field] = summarize_numbers(values)
    wall_values = [sample.wall_elapsed_ms for sample in successful]
    digests = [
        hashlib.sha256(
            json.dumps(sample.report, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for sample in successful
    ]
    return {
        "command": list(workload.command),
        "capability_failures": list(workload.capability_failures),
        "samples_requested": len(samples),
        "samples_completed": len(successful),
        "failure_count": len(samples) - len(successful),
        "failures": failures,
        "wall_timing_ms": summarize_numbers(wall_values) if wall_values else None,
        "metrics": metrics,
        # Raw child reports can contain path samples and grow with new fields.
        # Digests prove repeatability without retaining those payloads.
        "sample_report_digests": digests,
    }


def _sanitized_endpoint_spec(raw: str) -> EndpointSpec:
    spec = parse_endpoint_spec(raw)
    parsed = urllib.parse.urlparse(spec.target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("target API URLs must be absolute HTTP(S) URLs")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("target API URLs cannot contain credentials")
    sanitized = urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path or "/", "", "", "")
    )
    return EndpointSpec(label=spec.label[:100], target=sanitized)


def _measure_api(
    config: ImportTargetConfig,
    *,
    fetcher: Fetcher,
) -> list[dict[str, object]]:
    measurements: list[dict[str, object]] = []
    for raw in config.api_urls:
        measurement = measure_http_endpoint(
            _sanitized_endpoint_spec(raw),
            base_url="http://unused.invalid",
            samples=config.samples,
            timeout=config.timeout_seconds,
            fetcher=fetcher,
        ).to_dict()
        errors = measurement.get("errors")
        if isinstance(errors, list):
            measurement["errors"] = [
                str(error).partition(":")[0] for error in errors[:_MAX_FAILURES]
            ]
        measurements.append(measurement)
    return measurements


def _observed_shape(report: Mapping[str, object]) -> tuple[int | None, int | None]:
    raw_files = report.get("represented_file_count", report.get("expected_file_count"))
    raw_series = report.get("series_count", report.get("source_series_count"))
    series_count = (
        int(raw_series)
        if isinstance(raw_series, int) and not isinstance(raw_series, bool)
        else None
    )
    file_count = (
        int(raw_files) if isinstance(raw_files, int) and not isinstance(raw_files, bool) else None
    )
    raw_files_per_series = report.get("files_per_series")
    if (
        file_count is None
        and series_count is not None
        and isinstance(raw_files_per_series, int)
        and not isinstance(raw_files_per_series, bool)
    ):
        file_count = series_count * raw_files_per_series
    return file_count, series_count


def _elapsed_value(report: Mapping[str, object]) -> float | None:
    total = _numeric_value(report, "total_elapsed_ms")
    return total if total is not None else _numeric_value(report, "elapsed_ms")


def _evaluate_gates(
    config: ImportTargetConfig,
    workload: ImportTargetWorkload,
    samples: Sequence[TargetCommandSample],
    api_measurements: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    hard_failures: list[str] = list(workload.capability_failures)
    warnings: list[str] = []
    successful = [sample for sample in samples if sample.report is not None]
    if len(successful) != config.samples:
        hard_failures.append("workload_sample_failed")

    peak_values = [
        value
        for sample in successful
        if sample.report is not None
        and (value := _numeric_value(sample.report, "peak_rss_bytes")) is not None
    ]
    if peak_values and max(peak_values) > _PEAK_RSS_HARD_STOP_BYTES:
        hard_failures.append("peak_rss_hard_stop_exceeded")

    elapsed_values = [
        value
        for sample in successful
        if sample.report is not None and (value := _elapsed_value(sample.report)) is not None
    ]
    hard_elapsed_ms = (
        15 * 60 * 1000 if config.source_lane is ImportTargetSourceLane.METADATA else 30 * 60 * 1000
    )
    if elapsed_values and max(elapsed_values) > hard_elapsed_ms:
        hard_failures.append("wall_time_hard_stop_exceeded")

    if config.source_lane is ImportTargetSourceLane.MYLAR3:
        for sample in successful:
            assert sample.report is not None
            if _numeric_value(sample.report, "provider_call_count") not in {0.0}:
                hard_failures.append("trusted_mylar_provider_call_detected")
            if _numeric_value(sample.report, "archive_member_list_read_count") not in {0.0}:
                hard_failures.append("archive_member_index_reopened")
            if _numeric_value(sample.report, "archive_member_payload_read_count") not in {0.0}:
                hard_failures.append("archive_member_payload_reread")

    for sample in successful:
        assert sample.report is not None
        source_mutations = _numeric_value(sample.report, "source_mutation_count")
        orphan_count = _numeric_value(sample.report, "orphan_discrepancy_count")
        if source_mutations is not None and source_mutations > 0:
            hard_failures.append("source_mutation_detected")
        if orphan_count is not None and orphan_count > 0:
            hard_failures.append("orphan_discrepancy_detected")

    for measurement in api_measurements:
        samples_completed = measurement.get("samples_completed")
        if samples_completed != config.samples:
            hard_failures.append("api_measurement_incomplete")
        status_codes = measurement.get("status_codes")
        if isinstance(status_codes, Mapping) and any(
            str(code).startswith("5") and int(count) > 0
            for code, count in status_codes.items()
            if isinstance(count, int)
        ):
            hard_failures.append("api_5xx_response")
        timing = measurement.get("timing_ms")
        if isinstance(timing, Mapping):
            maximum = timing.get("max")
            if isinstance(maximum, int | float) and maximum > 5000:
                hard_failures.append("api_request_hard_stop_exceeded")
        else:
            hard_failures.append("api_timing_not_measured")

    if config.scale_profile is ImportTargetScaleProfile.FILES_200K:
        shape = scale_shape(config.scale_profile)
        if any(
            sample.report is None
            or _observed_shape(sample.report) != (shape.file_count, shape.series_count)
            for sample in samples
        ):
            hard_failures.append("target_shape_mismatch")
        if peak_values and max(peak_values) > _PEAK_RSS_TARGET_BYTES:
            hard_failures.append("target_peak_rss_exceeded")
        if len(peak_values) != len(successful):
            hard_failures.append("target_peak_rss_not_measured")
        if len(elapsed_values) != len(successful):
            hard_failures.append("target_wall_time_not_measured")
        if not api_measurements:
            hard_failures.append("target_api_latency_not_measured")
        required_database_metrics = {
            "wal_bytes",
            "transaction_p99_ms",
            "lock_wait_p99_ms",
        }
        if not successful or any(
            sample.report is None
            or any(
                _numeric_value(sample.report, metric) is None
                for metric in required_database_metrics
            )
            for sample in samples
        ):
            hard_failures.append("target_wal_and_lock_metrics_not_measured")
        required_recovery_metrics = {
            "cancel_latency_ms",
            "restart_latency_ms",
            "rollback_recovery_ms",
            "orphan_discrepancy_count",
        }
        if not successful or any(
            sample.report is None
            or any(
                _numeric_value(sample.report, metric) is None
                for metric in required_recovery_metrics
            )
            for sample in samples
        ):
            hard_failures.append("target_cancel_restart_recovery_not_measured")
    else:
        if not api_measurements:
            warnings.append("api_latency_not_sampled")
        warnings.append("smoke_profile_is_not_release_proof")

    unique_failures = list(dict.fromkeys(hard_failures))[:_MAX_FAILURES]
    return {
        "passed": not unique_failures,
        "hard_failures": unique_failures,
        "warnings": list(dict.fromkeys(warnings))[:_MAX_FAILURES],
    }


def build_import_target_report(
    config: ImportTargetConfig,
    *,
    sample_runner: TargetSampleRunner = run_target_command_sample,
    api_fetcher: Fetcher = default_fetcher,
) -> dict[str, object]:
    """Run one target lane and return a bounded, gate-evaluated report."""
    workload = build_target_workload(config)
    samples = (
        [sample_runner(workload, config) for _ in range(config.samples)]
        if workload.command
        else [
            TargetCommandSample(
                report=None,
                wall_elapsed_ms=0.0,
                error="workload_capability_missing",
            )
            for _ in range(config.samples)
        ]
    )
    api_measurements = _measure_api(config, fetcher=api_fetcher)
    context = collect_context(config.repo_root)
    context.pop("repo_root", None)
    context.update(
        {
            "environment_label": config.environment_label,
            "filesystem_label": config.filesystem_label,
            "cpu_count": os.cpu_count(),
        }
    )
    shape = scale_shape(config.scale_profile)
    return {
        "schema_version": TARGET_REPORT_SCHEMA_VERSION,
        "context": context,
        "settings": {
            "seed": config.seed,
            "backend": config.backend.value,
            "scale_profile": config.scale_profile.value,
            "source_lane": config.source_lane.value,
            "cache_state": config.cache_state.value,
            "injection_point": config.injection_point,
            "samples": config.samples,
            "timeout_seconds": config.timeout_seconds,
            "api_urls": [_sanitized_endpoint_spec(raw).target for raw in config.api_urls],
        },
        "shape": {
            "file_count": shape.file_count,
            "series_count": shape.series_count,
            "story_arc_count": shape.story_arc_count,
            "files_per_series": shape.files_per_series,
        },
        "workload": _summarize_samples(workload, samples),
        "api_measurements": api_measurements,
        "gate_evaluation": _evaluate_gates(
            config,
            workload,
            samples,
            api_measurements,
        ),
    }


def assert_comparable_target_reports(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> None:
    """Refuse scale comparisons across unlike backend/hardware/filesystem lanes."""
    left_context = left.get("context")
    right_context = right.get("context")
    left_settings = left.get("settings")
    right_settings = right.get("settings")
    if not all(
        isinstance(value, Mapping)
        for value in (left_context, right_context, left_settings, right_settings)
    ):
        raise ValueError("target reports are missing comparison context")
    assert isinstance(left_context, Mapping)
    assert isinstance(right_context, Mapping)
    assert isinstance(left_settings, Mapping)
    assert isinstance(right_settings, Mapping)
    comparisons = (
        ("environment_label", left_context, right_context),
        ("filesystem_label", left_context, right_context),
        ("platform", left_context, right_context),
        ("backend", left_settings, right_settings),
        ("source_lane", left_settings, right_settings),
        ("cache_state", left_settings, right_settings),
    )
    for key, left_values, right_values in comparisons:
        if left_values.get(key) != right_values.get(key):
            raise ValueError(f"target reports differ in {key}")


def _report_mapping(report: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = report.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"target report is missing {key}")
    return value


def _metric_median(report: Mapping[str, object], metric: str) -> float | None:
    workload = _report_mapping(report, "workload")
    metrics = workload.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    summary = metrics.get(metric)
    if not isinstance(summary, Mapping):
        return None
    median = summary.get("median")
    if isinstance(median, int | float) and not isinstance(median, bool):
        return float(median)
    return None


def build_import_database_parity_report(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> dict[str, object]:
    """Compare bounded SQLite/PostgreSQL reports for semantic parity."""
    left_settings = _report_mapping(left, "settings")
    right_settings = _report_mapping(right, "settings")
    backends = sorted(
        str(value) for value in (left_settings.get("backend"), right_settings.get("backend"))
    )
    failures: list[str] = []
    warnings: list[str] = []
    if backends != ["postgresql", "sqlite"]:
        failures.append("sqlite_and_postgresql_reports_required")
    for key in (
        "seed",
        "scale_profile",
        "source_lane",
        "cache_state",
        "injection_point",
        "samples",
    ):
        if left_settings.get(key) != right_settings.get(key):
            failures.append(f"setting_mismatch_{key}")
    left_shape = _report_mapping(left, "shape")
    right_shape = _report_mapping(right, "shape")
    if left_shape != right_shape:
        failures.append("target_shape_mismatch")
    for report in (left, right):
        settings = _report_mapping(report, "settings")
        workload = _report_mapping(report, "workload")
        if (
            workload.get("samples_completed") != settings.get("samples")
            or workload.get("failure_count") != 0
        ):
            failures.append("source_workload_incomplete")
        gates = _report_mapping(report, "gate_evaluation")
        if gates.get("passed") is not True:
            warnings.append("source_release_gates_not_complete")

    semantic_metrics: dict[str, dict[str, float | None]] = {}
    for metric in _PARITY_SEMANTIC_METRICS:
        left_value = _metric_median(left, metric)
        right_value = _metric_median(right, metric)
        semantic_metrics[metric] = {
            str(left_settings.get("backend")): left_value,
            str(right_settings.get("backend")): right_value,
        }
        if left_value is None or right_value is None:
            failures.append(f"semantic_metric_missing_{metric}")
        elif left_value != right_value:
            failures.append(f"semantic_metric_mismatch_{metric}")
    hard_failures = list(dict.fromkeys(failures))[:_MAX_FAILURES]
    return {
        "schema_version": TARGET_REPORT_SCHEMA_VERSION,
        "report_kind": "import_database_parity",
        "backends": backends,
        "scale_profile": left_settings.get("scale_profile"),
        "shape": {
            key: left_shape.get(key)
            for key in ("file_count", "series_count", "story_arc_count", "files_per_series")
        },
        "semantic_metrics": semantic_metrics,
        "passed": not hard_failures,
        "hard_failures": hard_failures,
        "warnings": list(dict.fromkeys(warnings))[:_MAX_FAILURES],
    }
