"""Browser regressions for measured import progress and unknown ETA."""


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
