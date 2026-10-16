"""Runtime configuration and routing policy for search workflows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select

from pullbox.models.config import SystemConfig
from pullbox.services.release_validator import validator_kwargs_from_eval_kwargs
from pullbox.services.search_scoring import (
    EVAL_CONFIG_KEYS,
    build_eval_kwargs,
    normalize_source_priority,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.indexer import IndexerConfig
    from pullbox.models.issue import IssueType
    from pullbox.models.library import MatchConfidence
    from pullbox.providers.base import ProviderRegistry
    from pullbox.services.direct_search_coordinator import DirectSearchProvider
    from pullbox.services.search_types import SearchEvalKwargs, ValidatorKwargs

    EvalKwargsBuilder = Callable[[dict[str, str]], SearchEvalKwargs]


class RegistryBuilder(Protocol):
    """Callable shape for building provider registries for search runtime."""

    def __call__(
        self,
        session: AsyncSession,
        *,
        include_download_clients: bool = True,
    ) -> Awaitable[tuple[ProviderRegistry, dict[int, IndexerConfig]] | None]: ...


@dataclass(frozen=True)
class SearchRuntime:
    """Configuration and provider state shared across one search execution."""

    registry: ProviderRegistry
    indexer_configs: dict[int, IndexerConfig]
    source_priority: list[str] | None
    eval_kwargs: SearchEvalKwargs
    validator_kwargs: ValidatorKwargs
    type_thresholds: dict[str, str]
    failure_threshold: int
    two_pass_enabled: bool = True
    direct_providers: tuple[DirectSearchProvider, ...] = ()


# Per-type auto-grab thresholds. "never" means always route to intervention.
DEFAULT_TYPE_THRESHOLDS: dict[str, str] = {
    "issue": "medium",
    "annual": "high",
    "tpb": "high",
    "hc": "high",
    "omnibus": "high",
    "gn": "high",
    "ogn": "high",
    "one_shot": "high",
    "special": "high",
    "deluxe": "high",
    "compendium": "high",
}


def should_auto_grab(
    confidence: MatchConfidence,
    issue_type: IssueType,
    type_thresholds: dict[str, str],
    default_threshold: str = "high",
) -> bool:
    """Return True when confidence meets the configured auto-grab threshold."""
    threshold = type_thresholds.get(issue_type.value, default_threshold)

    if threshold == "never":
        return False

    confidence_order = {"high": 3, "medium": 2, "low": 1}
    return confidence_order.get(confidence.value, 0) >= confidence_order.get(threshold, 3)


async def build_search_runtime(
    session: AsyncSession,
    *,
    include_download_clients: bool = True,
    registry_builder: RegistryBuilder | None = None,
    default_type_thresholds: dict[str, str] | None = None,
    eval_kwargs_builder: EvalKwargsBuilder = build_eval_kwargs,
    allow_empty_registry: bool = False,
    include_direct_providers: bool = False,
) -> SearchRuntime | None:
    """Build shared search runtime state for one request or task cycle."""
    if registry_builder is None:
        from pullbox.composition.providers import build_registry

        builder: RegistryBuilder = build_registry
    else:
        builder = registry_builder

    built = await builder(
        session,
        include_download_clients=include_download_clients,
    )
    if include_direct_providers:
        from pullbox.services.direct_search_coordinator import load_direct_search_providers

        direct_providers = await load_direct_search_providers(session)
    else:
        direct_providers = ()
    if built is None:
        if not allow_empty_registry and not direct_providers:
            return None
        from pullbox.providers.base import ProviderRegistry

        registry = ProviderRegistry()
        indexer_configs: dict[int, IndexerConfig] = {}
    else:
        registry, indexer_configs = built
    cfg_rows = await session.execute(
        select(SystemConfig.key, SystemConfig.value).where(
            SystemConfig.key.in_(
                [
                    *EVAL_CONFIG_KEYS,
                    "search_type_thresholds",
                    "source_priority",
                    "indexer_failure_threshold",
                    "search_two_pass_enabled",
                ]
            )
        )
    )
    cfg: dict[str, str] = dict(cfg_rows.all())

    try:
        source_priority_raw = json.loads(cfg.get("source_priority", "[]"))
        source_priority = normalize_source_priority(source_priority_raw)
    except (ValueError, TypeError):
        source_priority = None

    cfg_thresholds = json.loads(cfg.get("search_type_thresholds", "{}"))
    threshold_defaults = (
        DEFAULT_TYPE_THRESHOLDS if default_type_thresholds is None else default_type_thresholds
    )
    type_thresholds: dict[str, str] = {**threshold_defaults, **cfg_thresholds}
    eval_kwargs = eval_kwargs_builder(cfg)
    validator_kwargs = validator_kwargs_from_eval_kwargs(eval_kwargs)
    failure_threshold = int(cfg.get("indexer_failure_threshold", "3"))
    two_pass_enabled = str(cfg.get("search_two_pass_enabled", "true")).lower() != "false"

    return SearchRuntime(
        registry=registry,
        indexer_configs=indexer_configs,
        source_priority=source_priority,
        eval_kwargs=eval_kwargs,
        validator_kwargs=validator_kwargs,
        type_thresholds=type_thresholds,
        failure_threshold=failure_threshold,
        two_pass_enabled=two_pass_enabled,
        direct_providers=direct_providers,
    )
