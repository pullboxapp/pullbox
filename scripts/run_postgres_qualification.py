"""Require real PostgreSQL install and migration outcomes on both supported majors."""

import os
import sys

import pytest

CASES = (
    "test_postgres_fresh_install::test_new_account_survives_recreating_both_containers",
    "test_postgres_pool_recovery::test_issue_navigation_and_edit_when_pool_is_full",
    "test_database_migration_target::test_fresh_postgres_migrations_reach_head",
    "test_database_migration_target::test_populated_postgres_migration_preserves_values_and_backfills",
    "test_database_migration_target::test_postgres_downgrade_cannot_lose_large_import_byte_count",
    "test_postgres_transfer_rehearsal::test_complete_verified_rehearsal",
    "test_postgres_transfer_stage::test_killed_copy_does_not_activate_partial_library",
    "test_postgres_cutover::test_cutover_refuses_drift_then_releases_original_database",
    "test_postgres_native_restore::test_native_restore_preserves_new_writes_without_touching_offline_stage",
    "test_postgres_source_handoff::test_recheck_original_source_and_reviewed_target",
)


def run_required(cases: list[str]) -> int:
    passed: set[str] = set()

    class Outcomes:
        def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
            if report.when == "call" and report.passed and not hasattr(report, "wasxfail"):
                passed.add(report.nodeid)

    result = pytest.main(["-q", "-x", "-n0", "--tb=short", *cases], plugins=[Outcomes()])
    if result:
        return int(result)
    missing = sorted(set(cases) - passed)
    if missing:
        print("Required PostgreSQL cases without a pass:", *missing, sep="\n", file=sys.stderr)
        return 1
    return 0


def required_nodes() -> list[str]:
    nodes = []
    for case in CASES:
        module, test = case.split("::")
        for major in (16, 18):
            parameter = f"{major}-browser" if module == "test_postgres_source_handoff" else major
            nodes.append(f"tests/integration/{module}.py::{test}[{parameter}]")
    module = "tests/integration/test_postgres_mylar.py::"
    for major in (16, 18):
        for provider in ("metron", "gcd"):
            nodes.append(f"{module}test_conflict_followup[{major}-{provider}]")
            for in_place in (False, True):
                nodes.append(f"{module}test_mixed_folder[{major}-True-{in_place}-{provider}]")
                nodes.append(f"{module}test_recheck_at_placement[{major}-{in_place}-{provider}]")
                for state in ("unknown", "foreign_stale"):
                    nodes.append(
                        f"{module}test_recorded_match[{major}-{state}-{in_place}-{provider}]"
                    )
    return nodes


if __name__ == "__main__":
    if not os.environ.get("PULLBOX_TEST_SQLITE_SNAPSHOT_IMAGE"):
        sys.exit("A local production Pullbox image is required.")
    os.environ.update(
        PULLBOX_TEST_MIGRATION_TARGET_DOCKER="1",
        PULLBOX_TEST_MIGRATION_BROWSER="1",
        PULLBOX_TEST_PACKAGED_POSTGRES="1",
    )
    sys.exit(run_required(required_nodes()))
