"""Browser regressions for measured import progress and unknown ETA."""

import pytest


@pytest.mark.parametrize(
    ("new_series", "files_imported", "files_failed"),
    [(0, 1, 0), (2, 12, 0), (1, 5, 2)],
)
def test_import_file_counters_survive_hydration_live_updates_and_reload(
    authed_page, seeded_server, new_series, files_imported, files_failed
):
    authed_page.goto(f"{seeded_server}/import")
    result = authed_page.evaluate(
        """({newSeries, imported, failed}) => {
            const initial = {
                status: 'importing', mode: 'import', phase: 'importing',
                progress: 25, progress_revision: 1,
                series_found: 4, series_imported: 0, series_failed: 0,
                total_files_imported: 1, total_files_failed: 0
            };
            const controller = window.importProgressData(42, 5, 'filesystem', initial, 'import');
            const footer = window.importCollectionFooterData({step: 4, progressSnapshot: initial});
            const counts = () => Object.fromEntries(footer.items.map(item => [item.label, item.value]));
            const initialCounts = counts();
            const onProgress = event => footer.applyProgressState(event.detail);
            window.addEventListener('import:collection-footer', onProgress);
            try {
                controller.hydrateFromSnapshot();
                controller.applyJobState({
                    ...initial, progress_revision: 2, progress: 80,
                    series_imported: newSeries, series_failed: failed ? 1 : 0,
                    total_files_imported: imported, total_files_failed: failed
                });
                const liveCounts = counts();
                // Partial live updates must not erase the last durable file counts.
                controller.applyJobState({status: 'importing', progress_revision: 3, progress: 90});
                const partialCounts = counts();
                controller.applyJobState(controller.jobReadToProgressState({
                    status: 'completed', progress_revision: 4,
                    series_imported: newSeries, series_failed: failed ? 1 : 0,
                    total_files_imported: imported, total_files_failed: failed
                }));
                const completedCounts = counts();
                footer.applyWizardAdvance({step: 5, jobStatus: 'completed'});
                const resultsCounts = counts();
                const reloaded = window.importCollectionFooterData({step: 4, progressSnapshot: {
                    ...initial, status: 'completed', series_imported: newSeries,
                    total_files_imported: imported, total_files_failed: failed
                }});
                controller.applyJobState({
                    status: 'rolled_back', mode: 'rollback', progress_revision: 5,
                    series_imported: 0, series_failed: 0,
                    total_files_imported: 0, total_files_failed: 0
                });
                return {
                    initial: initialCounts, live: liveCounts, partial: partialCounts,
                    completed: completedCounts, results: resultsCounts,
                    rolledBack: counts(),
                    reloaded: Object.fromEntries(reloaded.items.map(item => [item.label, item.value]))
                };
            } finally {
                window.removeEventListener('import:collection-footer', onProgress);
            }
        }""",
        {"newSeries": new_series, "imported": files_imported, "failed": files_failed},
    )
    assert result["initial"].get("files imported") == "1"
    assert result["initial"].get("files failed") == "0"
    for stage in ("live", "partial", "completed", "results", "reloaded"):
        assert result[stage]["files imported"] == str(files_imported)
        assert result[stage]["files failed"] == str(files_failed)
    assert result["rolledBack"]["files imported"] == "0"
    assert result["rolledBack"]["files failed"] == "0"


def test_scan_unknown_eta_does_not_extrapolate_weighted_progress(authed_page, seeded_server):
    authed_page.goto(f"{seeded_server}/import")
    result = authed_page.evaluate(
        """() => {
            const controller = window.importProgressData(42, 3, 'mylar3', {}, 'scan');
            controller.progress = 19;
            controller.startedAt = new Date(Date.now() - 3936000).toISOString();
            controller.captureEtaState({estimated_seconds_remaining: null, elapsed_seconds: 3936});
            return {eta: controller.etaSeconds, label: controller.formatEtaLabel()};
        }"""
    )
    assert result == {"eta": None, "label": "Estimating..."}


def test_inventory_without_total_is_indeterminate(authed_page, seeded_server):
    authed_page.goto(f"{seeded_server}/import")
    result = authed_page.evaluate(
        """() => {
            const controller = window.importProgressData(42, 3, 'filesystem', {}, 'scan');
            controller.applyExplicitCurrentItemState({
                current_item_kind: 'scan', current_item_stage: 'inventory',
                current_item_progress_pct: null
            });
            return {
                progress: controller.currentItemProgress(),
                indeterminate: typeof controller.currentItemIsIndeterminate === 'function'
                    && controller.currentItemIsIndeterminate()
            };
        }"""
    )
    assert result == {"progress": 0, "indeterminate": True}
