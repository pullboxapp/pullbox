"""Attach bounded automatic AirDC++ evaluation without queue mutation."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from pullbox.composition.airdcpp import (
    get_airdcpp_search_coordinator,
    get_airdcpp_supervisor_registry,
    load_airdcpp_search_clients,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.search_targets import IssueSearchOutcome
    from pullbox.services.search_types import ValidatorKwargs


async def attach_automatic_airdcpp_search(
    session: AsyncSession,
    outcome: IssueSearchOutcome,
    *,
    validator_kwargs: ValidatorKwargs | None = None,
) -> IssueSearchOutcome:
    """Evaluate opted-in exact clients once; cooldown deferrals never queue."""
    if outcome.dc_outcome is not None:
        return outcome
    registry = get_airdcpp_supervisor_registry()
    coordinator = get_airdcpp_search_coordinator()
    if registry is None or coordinator is None:
        return outcome

    clients = await load_airdcpp_search_clients(
        session,
        registry,
        automatic=True,
    )
    # Never retain the configuration read transaction across remote search.
    await session.commit()
    if not clients:
        return outcome

    dc_outcome = await coordinator.search(
        clients,
        outcome.target,
        manual=False,
        validator_kwargs=validator_kwargs,
    )
    details = dict(outcome.search_details)
    details.update(
        {
            "dc_results_count": len(dc_outcome.matched) + len(dc_outcome.rejected),
            "dc_raw_count": dc_outcome.raw_count,
            "dc_dropped_count": dc_outcome.dropped_count,
            "dc_elapsed_ms": dc_outcome.elapsed_ms,
            "dc_partial": dc_outcome.partial,
        }
    )
    return replace(outcome, dc_outcome=dc_outcome, search_details=details)
