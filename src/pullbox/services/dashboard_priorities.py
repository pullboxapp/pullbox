"""Dashboard priority ranking and urgency copy."""

from __future__ import annotations

from pullbox.services.dashboard_helpers import (
    age_in_days,
    days_until,
    delta_from_reference,
    format_count_delta,
    format_rate_delta,
    oldest_age_label,
    priority_score,
    priority_state,
    priority_state_label,
    rate_drop_score,
    release_time_label,
    search_yield_is_drifting,
    storage_growth_label,
    storage_imminence,
    storage_runway_label,
    storage_severity,
    storage_trend_score,
    trend_label,
    trend_score,
)
from pullbox.services.dashboard_types import DashboardPriority, DashboardSnapshot


class DashboardPriorityBuilder:
    """Build ranked dashboard priorities from a metric snapshot."""

    def build_priorities(self, snapshot: DashboardSnapshot) -> list[DashboardPriority]:
        priorities = [
            self.build_health_priority(snapshot),
            self.build_storage_priority(snapshot),
            self.build_client_failure_priority(snapshot),
            self.build_review_debt_priority(snapshot),
            self.build_release_risk_priority(snapshot),
            self.build_search_yield_priority(snapshot),
            self.build_import_failure_priority(snapshot),
            self.build_unmatched_growth_priority(snapshot),
        ]
        filtered = [priority for priority in priorities if priority is not None]
        return sorted(filtered, key=lambda item: item.score, reverse=True)

    def build_health_priority(self, snapshot: DashboardSnapshot) -> DashboardPriority | None:
        if snapshot.health.problem_count <= 0:
            return None

        severity = (
            95
            if snapshot.health.unhealthy_count > 0
            else min(75, 35 + 15 * snapshot.health.degraded_count)
        )
        trend = trend_score(snapshot.health.problem_count, snapshot.health.reference_problem_count)
        aging = 55 if snapshot.health.unhealthy_count > 0 else 30
        blast = min(100, snapshot.health.problem_count * 30)
        imminence = 90 if snapshot.health.unhealthy_count > 0 else 65
        score = priority_score(severity, trend, aging, blast, imminence)

        return DashboardPriority(
            key="health-degradation",
            title="Health checks need a pass before they snowball.",
            state=priority_state(score),
            state_label=priority_state_label(score),
            score=score,
            why_it_matters=(
                "When health checks drift, everything downstream gets harder to trust."
            ),
            evidence=(
                ", ".join(snapshot.health.component_labels)
                or "One or more components are degraded."
            ),
            trend_label=trend_label(
                snapshot.health.problem_count,
                snapshot.health.reference_problem_count,
                noun="alert",
            ),
            time_label="Check now",
            cta_label="Open health",
            cta_href="/health",
        )

    def build_storage_priority(self, snapshot: DashboardSnapshot) -> DashboardPriority | None:
        if (
            snapshot.storage.state == "healthy"
            and snapshot.storage.runway_to_degraded_days is not None
            and snapshot.storage.runway_to_degraded_days > 45
        ):
            return None

        severity = storage_severity(snapshot.storage)
        if severity <= 0:
            return None

        trend = storage_trend_score(snapshot.storage)
        aging = 45 if snapshot.storage.snapshot_count >= 2 else 15
        blast = 85
        imminence = storage_imminence(snapshot.storage)
        score = priority_score(severity, trend, aging, blast, imminence)

        runway_label = storage_runway_label(snapshot.storage)
        return DashboardPriority(
            key="storage-runway",
            title="Storage runway is getting tight.",
            state=priority_state(score),
            state_label=priority_state_label(score),
            score=score,
            why_it_matters=(
                "If disk pressure keeps climbing, downloads and imports will start to trip over it."
            ),
            evidence=f"{snapshot.storage.used_percent:.1f}% used. {runway_label}.",
            trend_label=storage_growth_label(snapshot.storage),
            time_label=(
                "This week"
                if snapshot.storage.runway_to_degraded_days
                and snapshot.storage.runway_to_degraded_days <= 7
                else "Keep watch"
            ),
            cta_label="Open library",
            cta_href="/library",
        )

    def build_client_failure_priority(
        self,
        snapshot: DashboardSnapshot,
    ) -> DashboardPriority | None:
        rate = snapshot.client_reliability.rate
        worst_client_rate = snapshot.client_reliability.worst_client_rate
        if rate is None or worst_client_rate is None:
            return None
        if worst_client_rate >= 90.0 and snapshot.client_reliability.worst_client_failures < 2:
            return None

        severity = min(
            95,
            max(
                30,
                round(100 - worst_client_rate)
                + snapshot.client_reliability.worst_client_failures * 8,
            ),
        )
        trend = rate_drop_score(rate, snapshot.client_reliability.previous_rate)
        aging = 25
        blast = min(80, snapshot.client_reliability.worst_client_failures * 18)
        imminence = 65 if snapshot.client_reliability.worst_client_failures >= 3 else 45
        score = priority_score(severity, trend, aging, blast, imminence)

        client_name = snapshot.client_reliability.worst_client_label or "A client"
        return DashboardPriority(
            key="client-failure-cluster",
            title=f"{client_name} is the weak spot right now.",
            state=priority_state(score),
            state_label=priority_state_label(score),
            score=score,
            why_it_matters=(
                "Repeated client failures slow the queue down and hide the next problem "
                "behind retries."
            ),
            evidence=(
                f"{client_name} landed "
                f"{snapshot.client_reliability.worst_client_failures} failed terminal downloads "
                "in the last 7 days."
            ),
            trend_label=format_rate_delta(
                rate,
                snapshot.client_reliability.previous_rate,
            ),
            time_label="Review this week",
            cta_label="Open downloads",
            cta_href="/downloads?tab=history",
        )

    def build_review_debt_priority(self, snapshot: DashboardSnapshot) -> DashboardPriority | None:
        if snapshot.review_debt.total <= 0:
            return None

        oldest_days = age_in_days(snapshot.review_debt.oldest_at, snapshot.computed_at)
        severity = min(95, snapshot.review_debt.total * 3 + min(35, oldest_days * 4))
        trend = trend_score(snapshot.review_debt.total, snapshot.review_debt.reference_total)
        aging = min(100, oldest_days * 10)
        blast = min(80, snapshot.review_debt.total * 2)
        imminence = 75 if snapshot.release_risk.next_72h_count > 0 else 55
        score = priority_score(severity, trend, aging, blast, imminence)

        return DashboardPriority(
            key="review-debt-aging",
            title="Review debt is starting to stick.",
            state=priority_state(score),
            state_label=priority_state_label(score),
            score=score,
            why_it_matters=(
                "Every unresolved match or unmatched file makes the next cleanup pass slower."
            ),
            evidence=(
                f"{snapshot.review_debt.pending_matches} intervention items, "
                f"{snapshot.review_debt.suggestions} suggestions, and "
                f"{snapshot.review_debt.unmatched_backlog} unmatched files are still open."
            ),
            trend_label=format_count_delta(
                snapshot.review_debt.total,
                snapshot.review_debt.reference_total,
            ),
            time_label=oldest_age_label(
                snapshot.review_debt.oldest_at,
                snapshot.computed_at,
            ),
            cta_label="Open intervention",
            cta_href="/intervention",
        )

    def build_release_risk_priority(self, snapshot: DashboardSnapshot) -> DashboardPriority | None:
        if snapshot.release_risk.next_72h_count <= 0:
            return None

        days_to_impact = days_until(
            snapshot.release_risk.nearest_release_date,
            snapshot.computed_at.date(),
        )
        severity = min(
            100,
            45 + snapshot.release_risk.next_72h_count * 12 + max(0, 25 - days_to_impact * 8),
        )
        trend = trend_score(
            snapshot.release_risk.next_72h_count,
            snapshot.release_risk.reference_count,
        )
        aging = 20
        blast = min(90, snapshot.release_risk.next_72h_count * 18)
        imminence = 95 if days_to_impact <= 1 else 75
        score = priority_score(severity, trend, aging, blast, imminence)

        return DashboardPriority(
            key="release-coverage-gap",
            title="A few releases are about to hit without coverage.",
            state=priority_state(score),
            state_label=priority_state_label(score),
            score=score,
            why_it_matters=("If these slip by, the backlog gets more expensive to catch up later."),
            evidence=(
                f"{snapshot.release_risk.next_72h_count} issues are due in the next 72 hours "
                "and still aren't owned or downloading."
            ),
            trend_label=format_count_delta(
                snapshot.release_risk.next_72h_count,
                snapshot.release_risk.reference_count,
            ),
            time_label=release_time_label(
                snapshot.release_risk.nearest_release_date,
                snapshot.computed_at.date(),
            ),
            cta_label="Open pull list",
            cta_href="/pull-list",
        )

    def build_search_yield_priority(self, snapshot: DashboardSnapshot) -> DashboardPriority | None:
        if not search_yield_is_drifting(snapshot.search_yield):
            return None

        rate = snapshot.search_yield.rate or 0.0
        previous_rate = snapshot.search_yield.previous_rate or rate
        severity = min(80, int(previous_rate - rate) * 2 + 25)
        trend = rate_drop_score(rate, previous_rate)
        aging = 20
        blast = min(70, snapshot.search_yield.searches * 6)
        imminence = 35
        score = priority_score(severity, trend, aging, blast, imminence)

        return DashboardPriority(
            key="search-yield-drop",
            title="Searches are landing less often than they were.",
            state=priority_state(score),
            state_label=priority_state_label(score),
            score=score,
            why_it_matters=(
                "Lower search yield usually means more wanted issues stay wanted for longer."
            ),
            evidence=(
                f"{snapshot.search_yield.matched_results} matched results came out of "
                f"{snapshot.search_yield.searches} recent searches."
            ),
            trend_label=format_rate_delta(rate, previous_rate),
            time_label="Last 7 days",
            cta_label="Open search history",
            cta_href="/search-history",
        )

    def build_import_failure_priority(
        self,
        snapshot: DashboardSnapshot,
    ) -> DashboardPriority | None:
        if snapshot.import_failures.total <= 0:
            return None

        severity = min(85, snapshot.import_failures.total * 6 + 20)
        trend = trend_score(
            snapshot.import_failures.total,
            float(snapshot.import_failures.previous_total),
        )
        aging = 25
        blast = min(75, snapshot.import_failures.total * 5)
        imminence = 45
        score = priority_score(severity, trend, aging, blast, imminence)

        return DashboardPriority(
            key="import-failure-cluster",
            title="Import cleanup has started to stack up.",
            state=priority_state(score),
            state_label=priority_state_label(score),
            score=score,
            why_it_matters=(
                "Failed import runs usually leave follow-up work behind in more than one place."
            ),
            evidence=(
                f"{snapshot.import_failures.failed_jobs} failed jobs and "
                f"{snapshot.import_failures.failed_files} failed file actions landed this week."
            ),
            trend_label=format_count_delta(
                snapshot.import_failures.total,
                float(snapshot.import_failures.previous_total),
            ),
            time_label="Last 7 days",
            cta_label="Open import history",
            cta_href="/import?tab=history",
        )

    def build_unmatched_growth_priority(
        self,
        snapshot: DashboardSnapshot,
    ) -> DashboardPriority | None:
        reference = snapshot.review_debt.reference_total
        unmatched_count = snapshot.review_debt.unmatched_backlog
        unmatched_delta = delta_from_reference(unmatched_count, reference)
        if unmatched_count <= 0:
            return None
        if unmatched_delta is None and unmatched_count < 6:
            return None
        if unmatched_delta is not None and unmatched_delta <= 0 and unmatched_count < 10:
            return None

        oldest_days = age_in_days(snapshot.review_debt.oldest_at, snapshot.computed_at)
        severity = min(80, unmatched_count * 3 + max(0, unmatched_delta or 0) * 3)
        trend = trend_score(unmatched_count, reference)
        aging = min(70, oldest_days * 8)
        blast = min(70, unmatched_count * 2)
        imminence = 35
        score = priority_score(severity, trend, aging, blast, imminence)

        return DashboardPriority(
            key="unmatched-growth",
            title="Unmatched files are growing faster than they should.",
            state=priority_state(score),
            state_label=priority_state_label(score),
            score=score,
            why_it_matters=(
                "Unmatched files are usually the first sign that naming or import cleanup "
                "needs a second pass."
            ),
            evidence=f"{unmatched_count} files are still unmatched in the library.",
            trend_label=format_count_delta(unmatched_count, reference),
            time_label=oldest_age_label(
                snapshot.review_debt.oldest_at,
                snapshot.computed_at,
            ),
            cta_label="Review unmatched",
            cta_href="/import?tab=follow-up",
        )
