"""Search release scoring and evaluation-config helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pullbox.models.library import MatchConfidence

if TYPE_CHECKING:
    from pullbox.providers.base import ReleaseResult
    from pullbox.services.search_types import SearchEvalKwargs

DEFAULT_MIN_SIZE_MB = 50
DEFAULT_MAX_SIZE_MB = 2000
DIRECT_PROVIDER_NEUTRAL_PRIORITY = 25
PREFERRED_FORMATS: dict[str, int] = {"cbz": 100, "cbr": 80, "cb7": 60}
DEFAULT_SOURCE_PRIORITY: tuple[str, ...] = ("usenet", "torrent", "direct", "dc")

_FORMAT_RE = re.compile(r"\.(cbz|cbr|cb7|pdf|epub)\b", re.IGNORECASE)
_TAG_RE = re.compile(r"\b(cbz|cbr|cb7)\b", re.IGNORECASE)
_MB = 1024 * 1024

# Newznab category IDs for comics -- used for scoring priority.
# 7030 = Comics on most indexers (e.g. NZBgeek), 7020 = EBooks.
_COMIC_CATEGORY_IDS = frozenset({"7030"})
_BOOK_CATEGORY_IDS = frozenset({"7000", "7010", "7020", "7040"})
_MATCH_CONFIDENCE_RANK = {
    MatchConfidence.HIGH.value: 0,
    MatchConfidence.MEDIUM.value: 1,
    MatchConfidence.LOW.value: 2,
    MatchConfidence.MANUAL.value: 3,
    MatchConfidence.UNMATCHED.value: 4,
}


def match_confidence_rank(value: MatchConfidence | str | None) -> int:
    """Return a stable best-first rank for semantic match confidence."""
    normalized = value.value if isinstance(value, MatchConfidence) else value
    return _MATCH_CONFIDENCE_RANK.get(normalized or "", 99)


def normalize_source_priority(value: object) -> list[str] | None:
    """Return one complete, de-duplicated protocol order."""
    if not isinstance(value, list | tuple):
        return None

    normalized: list[str] = []
    for raw_item in value:
        if not isinstance(raw_item, str):
            continue
        item = "direct" if raw_item.strip().lower() == "ddl" else raw_item.strip().lower()
        if item in DEFAULT_SOURCE_PRIORITY and item not in normalized:
            normalized.append(item)

    if not normalized:
        return None
    normalized.extend(item for item in DEFAULT_SOURCE_PRIORITY if item not in normalized)
    return normalized


def score_release(
    release: ReleaseResult,
    indexer_priority: int | None = None,
    *,
    min_size_mb: int = DEFAULT_MIN_SIZE_MB,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
    preferred_format: str | None = None,
    seeder_tiers: tuple[int, int, int] | None = None,
    score_weights: tuple[float, float, float, float] | None = None,
    grabs_weight: int = 0,
    pack_penalty: int = -20,
    max_file_count: int = 5,
    preferred_language: str = "en",
    digital_bonus: int = 10,
) -> float:
    """Score a release candidate. Higher is better. Range roughly 0-100.

    Default weights:
        Indexer priority  40%
        Release age       30%
        File size         20%
        Format            10%

    When ``grabs_weight`` > 0, it joins the weighted sum and all weights
    are re-normalized to sum to 1.0.

    Additive modifiers (applied on top of the weighted score):
        - Category bonus: +15 Comics, +5 Books
        - Seeder bonus: +4/+8/+12/+15 tiered
        - Digital bonus: +N for digital releases
        - Pack penalty: -N for multi-issue packs
        - Language penalty: -30 for non-preferred language
    """
    raw_weights = list(score_weights or (0.40, 0.30, 0.20, 0.10))

    if grabs_weight > 0:
        if all(w < 2 for w in raw_weights):
            base = [w * 100 for w in raw_weights]
        else:
            base = list(raw_weights)
        base.append(float(grabs_weight))
        total = sum(base)
        w_priority, w_age, w_size, w_format, w_grabs = (b / total for b in base)
    else:
        w_priority, w_age, w_size, w_format = raw_weights
        w_grabs = 0.0

    resolved_priority = release.ranking_priority if indexer_priority is None else indexer_priority
    priority_score = _score_indexer_priority(resolved_priority)
    age_score = _score_age(release.age_days)
    size_score = _score_size(release.size_bytes, min_size_mb, max_size_mb)
    format_score = _score_format(release.title, preferred_format=preferred_format)
    category_bonus = _score_category(release.category)
    seeders_bonus = _score_seeders(
        release.seeders,
        release.is_torrent,
        tiers=seeder_tiers or (10, 30, 50),
    )

    weighted = (
        priority_score * w_priority
        + age_score * w_age
        + size_score * w_size
        + format_score * w_format
    )
    if w_grabs > 0:
        weighted += _score_grabs(release.grabs) * w_grabs

    return (
        weighted
        + category_bonus
        + seeders_bonus
        + _score_digital_bonus(release.title, bonus=digital_bonus)
        + _score_pack_penalty(
            release.title,
            file_count=None,
            max_file_count=max_file_count,
            penalty=pack_penalty,
        )
        + _score_language(release.title, preferred=preferred_language)
    )


def _score_indexer_priority(priority: int) -> float:
    """Score based on indexer priority (1 = best, 50 = worst)."""
    clamped = max(1, min(priority, 50))
    return ((51 - clamped) / 50) * 100


def _score_age(age_days: int | None) -> float:
    """Score based on release age with a slow comics-friendly decay."""
    if age_days is None:
        return 50.0
    return max(30.0, 100.0 - age_days * 0.02)


def _score_size(
    size_bytes: int | None,
    min_size_mb: int,
    max_size_mb: int,
) -> float:
    """Score based on file size. Ideal range scores 100."""
    if size_bytes is None:
        return 50.0

    size_mb = size_bytes / _MB

    if size_mb < min_size_mb:
        return (size_mb / min_size_mb) * 100 if min_size_mb > 0 else 0.0
    if size_mb > max_size_mb:
        overshoot = (size_mb - max_size_mb) / max_size_mb if max_size_mb > 0 else 1.0
        return max(0.0, 100.0 - overshoot * 100)
    return 100.0


def _score_format(title: str, *, preferred_format: str | None = None) -> float:
    """Score based on detected format from the release title."""
    if preferred_format:
        preferred = preferred_format.lower().lstrip(".")
        fmt_scores = {"cbz": 80, "cbr": 80, "cb7": 60}
        fmt_scores[preferred] = 100
    else:
        fmt_scores = PREFERRED_FORMATS

    ext_match = _FORMAT_RE.search(title)
    if ext_match:
        fmt = ext_match.group(1).lower()
        return float(fmt_scores.get(fmt, 40))

    tag_match = _TAG_RE.search(title)
    if tag_match:
        fmt = tag_match.group(1).lower()
        return float(fmt_scores.get(fmt, 40))

    return 40.0


def _score_category(category: str | None) -> float:
    """Bonus points based on result category."""
    if not category:
        return 0.0

    best = 0.0
    for part in category.split(","):
        part = part.strip()
        if not part:
            continue

        if part.isdigit():
            if part in _COMIC_CATEGORY_IDS:
                return 15.0
            if part in _BOOK_CATEGORY_IDS:
                best = max(best, 5.0)
            continue

        lower = part.lower()
        if "comic" in lower:
            return 15.0
        if "book" in lower or "ebook" in lower:
            best = max(best, 5.0)

    return best


def _score_seeders(
    seeders: int | None,
    is_torrent: bool,
    tiers: tuple[int, int, int] = (10, 30, 50),
) -> float:
    """Tiered bonus for well-seeded torrents. Non-torrents get 0."""
    if not is_torrent or seeders is None or seeders <= 0:
        return 0.0
    t1, t2, t3 = tiers
    if seeders <= t1:
        return 4.0
    if seeders <= t2:
        return 8.0
    if seeders <= t3:
        return 12.0
    return 15.0


def _score_grabs(grabs: int | None) -> float:
    """Tiered score based on download count (grabs)."""
    if grabs is None or grabs <= 0:
        return 30.0
    if grabs <= 10:
        return 60.0
    if grabs <= 50:
        return 80.0
    if grabs <= 100:
        return 90.0
    return 100.0


_PACK_RE = re.compile(
    r"#?\d+\s*[-\u2013]\s*#?\d+"
    r"|(?:complete|full)\s+collection"
    r"|\bpack\b",
    re.IGNORECASE,
)


def _score_pack_penalty(
    title: str,
    file_count: int | None,
    *,
    max_file_count: int = 5,
    penalty: int = -20,
) -> float:
    """Negative modifier for multi-issue packs."""
    if penalty == 0:
        return 0.0

    if _PACK_RE.search(title):
        return float(penalty)

    if file_count is not None and file_count > max_file_count:
        return float(penalty)

    return 0.0


_LANGUAGE_MAP: dict[str, str] = {
    "fr": "fr",
    "fre": "fr",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "ita": "it",
    "italian": "it",
    "portuguese": "pt",
    "japanese": "ja",
    "chinese": "zh",
    "korean": "ko",
    "dutch": "nl",
    "polish": "pl",
    "russian": "ru",
}

_LANGUAGE_RE = re.compile(
    r"\b(" + "|".join(_LANGUAGE_MAP.keys()) + r")\b",
    re.IGNORECASE,
)


def _score_language(title: str, *, preferred: str = "en") -> float:
    """Penalty for releases in a non-preferred language."""
    match = _LANGUAGE_RE.search(title)
    if not match:
        return 0.0

    detected_lang = _LANGUAGE_MAP[match.group(1).lower()]
    if detected_lang == preferred:
        return 0.0
    return -30.0


_SCAN_RE = re.compile(r"\b(?:scan|c2c)\b", re.IGNORECASE)
_DIGITAL_RE = re.compile(r"\b(?:digital|webrip)\b", re.IGNORECASE)


def _score_digital_bonus(title: str, *, bonus: int = 10) -> float:
    """Positive modifier for digital releases over scans."""
    if bonus == 0:
        return 0.0
    if _SCAN_RE.search(title):
        return 0.0
    if _DIGITAL_RE.search(title):
        return float(bonus)
    return 0.0


EVAL_CONFIG_KEYS = [
    "search_ignore_words",
    "score_weight_priority",
    "score_weight_age",
    "score_weight_size",
    "score_weight_format",
    "score_confidence_blend",
    "match_fuzzy_high_threshold",
    "match_fuzzy_low_threshold",
    "match_year_tolerance",
    "min_quality_score",
    "min_release_size_mb",
    "max_release_size_mb",
    "preferred_format",
    "search_size_warn_issue_mb",
    "search_size_warn_collection_mb",
    "indexer_failure_threshold",
    "score_seeder_tier1",
    "score_seeder_tier2",
    "score_seeder_tier3",
    "score_weight_grabs",
    "score_penalty_pack",
    "preferred_language",
    "score_bonus_digital",
    "max_file_count",
]


def _sort_by_source_priority(
    results: list[ReleaseResult],
    priority: list[str],
) -> list[ReleaseResult]:
    """Stable sort results by source protocol priority."""
    normalized = normalize_source_priority(priority)
    if normalized is None:
        return list(results)
    priority_map = {proto: idx for idx, proto in enumerate(normalized)}
    default_rank = len(normalized)

    def _rank(result: ReleaseResult) -> int:
        return priority_map.get(result.protocol.value, default_rank)

    return sorted(results, key=_rank)


def build_eval_kwargs(cfg: dict[str, str]) -> SearchEvalKwargs:
    """Build keyword arguments for ``SearchService.evaluate_results`` from config."""
    kwargs: SearchEvalKwargs = {}

    if cfg.get("search_ignore_words"):
        kwargs["ignore_words"] = [
            w.strip() for w in cfg["search_ignore_words"].split(",") if w.strip()
        ]

    if any(
        k in cfg
        for k in [
            "score_weight_priority",
            "score_weight_age",
            "score_weight_size",
            "score_weight_format",
        ]
    ):
        total = (
            int(cfg.get("score_weight_priority", "40"))
            + int(cfg.get("score_weight_age", "30"))
            + int(cfg.get("score_weight_size", "20"))
            + int(cfg.get("score_weight_format", "10"))
        )
        if total > 0:
            kwargs["score_weights"] = (
                int(cfg.get("score_weight_priority", "40")) / total,
                int(cfg.get("score_weight_age", "30")) / total,
                int(cfg.get("score_weight_size", "20")) / total,
                int(cfg.get("score_weight_format", "10")) / total,
            )

    if "score_confidence_blend" in cfg:
        kwargs["confidence_blend"] = int(cfg["score_confidence_blend"]) / 100.0

    if "match_fuzzy_high_threshold" in cfg:
        kwargs["fuzzy_high_threshold"] = int(cfg["match_fuzzy_high_threshold"]) / 100.0

    if "match_fuzzy_low_threshold" in cfg:
        kwargs["fuzzy_low_threshold"] = int(cfg["match_fuzzy_low_threshold"]) / 100.0

    if "match_year_tolerance" in cfg:
        kwargs["year_tolerance"] = int(cfg["match_year_tolerance"])

    if "min_quality_score" in cfg:
        kwargs["min_score"] = float(cfg["min_quality_score"])

    if "min_release_size_mb" in cfg:
        kwargs["min_size_mb"] = int(cfg["min_release_size_mb"])

    if "max_release_size_mb" in cfg:
        kwargs["max_size_mb"] = int(cfg["max_release_size_mb"])

    if "preferred_format" in cfg:
        kwargs["preferred_format"] = cfg["preferred_format"]

    if "score_seeder_tier1" in cfg:
        kwargs["seeder_tiers"] = (
            int(cfg.get("score_seeder_tier1", "10")),
            int(cfg.get("score_seeder_tier2", "30")),
            int(cfg.get("score_seeder_tier3", "50")),
        )

    if "search_size_warn_issue_mb" in cfg:
        kwargs["warn_issue_mb"] = int(cfg["search_size_warn_issue_mb"])

    if "search_size_warn_collection_mb" in cfg:
        kwargs["warn_collection_mb"] = int(cfg["search_size_warn_collection_mb"])

    if "score_weight_grabs" in cfg:
        kwargs["grabs_weight"] = int(cfg["score_weight_grabs"])

    if "score_penalty_pack" in cfg:
        kwargs["pack_penalty"] = int(cfg["score_penalty_pack"])

    if "preferred_language" in cfg:
        kwargs["preferred_language"] = cfg["preferred_language"]

    if "score_bonus_digital" in cfg:
        kwargs["digital_bonus"] = int(cfg["score_bonus_digital"])

    if "max_file_count" in cfg:
        kwargs["max_file_count"] = int(cfg["max_file_count"])

    return kwargs
