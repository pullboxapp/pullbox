"""Keep resumable recovery context without retaining consumed provider catalogs."""

from typing import Any

_PREPARATION_CACHE_KEYS = frozenset(
    {
        "candidates",
        "completed",
        "matches",
        "title_candidates",
        "title_completed",
        "title_matches",
        "reference_candidates",
    }
)


def compact_recovery_state(state: dict[str, Any]) -> dict[str, Any]:
    """Discard caches only after their decisions are stored on import-file rows."""
    if state.get("state") not in {"prepared", "completed"}:
        return dict(state)
    return {key: value for key, value in state.items() if key not in _PREPARATION_CACHE_KEYS}
