"""Complete, read-only Mylar path-mapping analysis for Import Step 1."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from pullbox.core.filesystem_policy import is_sensitive_path, resolve_preview_source
from pullbox.core.mylar3_path_mapping import (
    has_conflicting_overlapping_mappings,
    normalize_mylar3_path_map,
    normalize_mylar3_path_mapping_items,
    ordered_mylar3_path_map_items,
)
from pullbox.models.import_job import ImportFileHandlingMode, ImportSourceType
from pullbox.models.library import LibraryRoot
from pullbox.schemas.import_mylar3_path_preflight import (
    MylarIdentityGroupPreview,
    MylarPathExample,
    MylarPathMappingDraft,
    MylarPathMappingPreview,
    MylarPathOutcome,
    MylarPathPreviewResponse,
    MylarPathResolutionCounts,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

_MAX_LOCATIONS = 100_000
_MAX_EXAMPLES = 3


@dataclass(frozen=True, slots=True)
class _RootBoundary:
    root_id: int
    name: str
    lexical: Path
    resolved: Path
    device: int
    inode: int


@dataclass(slots=True)
class _MutableCounts:
    locations: int = 0
    identity_resolved: int = 0
    mapped_existing: int = 0
    mapped_missing: int = 0
    unmapped: int = 0
    outside_root: int = 0
    unreadable: int = 0
    ambiguous: int = 0
    invalid: int = 0

    def response(self) -> MylarPathResolutionCounts:
        return MylarPathResolutionCounts(**asdict(self))


@dataclass(slots=True)
class _MappingState:
    stored_prefix: str
    pullbox_prefix: str
    provenance: Literal["automatic", "manual"]
    root: _RootBoundary | None = None
    counts: _MutableCounts = field(default_factory=_MutableCounts)
    examples: list[MylarPathExample] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _IdentityState:
    root: _RootBoundary
    counts: _MutableCounts = field(default_factory=_MutableCounts)
    examples: list[MylarPathExample] = field(default_factory=list)


class Mylar3PathPreflightAnalyzer:
    """Analyze all bounded Mylar series locations without creating a job."""

    async def analyze(
        self,
        session: AsyncSession,
        source_path: str | Path,
        *,
        auto_detect: bool,
        mappings: list[MylarPathMappingDraft],
        file_handling_mode: ImportFileHandlingMode = ImportFileHandlingMode.MANAGED_COPY,
    ) -> MylarPathPreviewResponse:
        selected = resolve_preview_source(source_path)
        database = selected / "mylar.db" if selected.is_dir() else selected
        database = resolve_preview_source(database)
        if not database.is_file():
            raise ValueError("Mylar path preview requires a mylar.db file")

        root_query = select(LibraryRoot).where(LibraryRoot.enabled.is_(True))
        if file_handling_mode == ImportFileHandlingMode.IN_PLACE:
            root_query = root_query.where(LibraryRoot.allow_referenced_registrations.is_(True))
        root_models = list(
            (await session.execute(root_query.order_by(LibraryRoot.id.asc()))).scalars().all()
        )
        roots, root_warnings = await asyncio.to_thread(self._snapshot_roots, root_models)
        locations, partial = await asyncio.to_thread(self._read_locations, database)

        supplied_map = normalize_mylar3_path_mapping_items(
            (mapping.stored_prefix, mapping.pullbox_prefix) for mapping in mappings
        )
        ambiguous_auto_locations: set[str] = set()
        if auto_detect and not supplied_map:
            supplied_map, ambiguous_auto_locations = await asyncio.to_thread(
                self._auto_detect_map,
                locations,
                roots,
            )
        provenance: Literal["automatic", "manual"] = "automatic" if auto_detect else "manual"

        total = _MutableCounts(locations=len(locations))
        require_library_root = file_handling_mode == ImportFileHandlingMode.IN_PLACE
        mapping_states = self._mapping_states(
            supplied_map,
            provenance,
            roots,
            require_library_root=require_library_root,
        )
        identity_states: dict[int, _IdentityState] = {}
        ordered_map = ordered_mylar3_path_map_items(supplied_map)

        for location in locations:
            if location in ambiguous_auto_locations:
                total.ambiguous += 1
                continue
            outcome, root, mapping_state, relative = self._resolve_location(
                location,
                roots,
                mapping_states,
                ordered_map,
                require_library_root=require_library_root,
            )
            setattr(total, _count_field(outcome), getattr(total, _count_field(outcome)) + 1)
            if outcome == "identity" and root is not None:
                identity = identity_states.setdefault(root.root_id, _IdentityState(root=root))
                identity.counts.locations += 1
                identity.counts.identity_resolved += 1
                _append_example(identity.examples, relative, outcome)
            elif mapping_state is not None:
                mapping_state.counts.locations += 1
                count_field = _count_field(outcome)
                setattr(
                    mapping_state.counts,
                    count_field,
                    getattr(mapping_state.counts, count_field) + 1,
                )
                _append_example(mapping_state.examples, relative, outcome)

        warnings = list(root_warnings)
        if not roots and require_library_root:
            warnings.append("no_enabled_library_roots")
        if total.unmapped:
            warnings.append("unmapped_locations")
        if ambiguous_auto_locations:
            warnings.append("ambiguous_mapping_candidates")
        if partial:
            warnings.append("partial_preview")
        identity_group_total = sum(state.counts.locations for state in identity_states.values())
        if total.identity_resolved > identity_group_total:
            warnings.append("external_identity_sources")

        for state in mapping_states.values():
            if state.counts.locations == 0:
                state.blockers.append("mapping_does_not_improve_coverage")
        blocking_counts = total.outside_root + total.unreadable + total.ambiguous + total.invalid
        mapping_blocked = any(state.blockers for state in mapping_states.values())
        root_contract_blocked = (require_library_root or provenance == "automatic") and any(
            warning in {"library_root_alias", "nested_library_roots_ambiguous"}
            for warning in root_warnings
        )
        automatic_evidence_blocked = provenance == "automatic" and bool(
            total.mapped_missing
            or total.unmapped
            or any(
                warning in {"library_root_unavailable", "library_root_unreadable"}
                for warning in root_warnings
            )
        )
        if automatic_evidence_blocked:
            warnings.append("automatic_mapping_incomplete")
        can_confirm = bool(locations) and not any(
            (
                partial,
                blocking_counts,
                mapping_blocked,
                root_contract_blocked,
                automatic_evidence_blocked,
            )
        )
        return MylarPathPreviewResponse(
            source_type=ImportSourceType.MYLAR3,
            resolution=total.response(),
            identity_groups=[
                MylarIdentityGroupPreview(
                    stored_prefix=str(state.root.lexical),
                    library_root_id=state.root.root_id,
                    library_root_name=state.root.name,
                    resolution=state.counts.response(),
                    examples=state.examples,
                )
                for state in identity_states.values()
            ],
            mappings=[self._mapping_response(state) for state in mapping_states.values()],
            path_map=supplied_map,
            requires_confirmation=bool(supplied_map),
            can_confirm=can_confirm,
            partial=partial,
            warnings=list(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _read_locations(database: Path) -> tuple[list[str], bool]:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            try:
                connection.execute("PRAGMA query_only = ON")
                comics_columns = _sqlite_table_columns(connection, "comics")
                if "ComicLocation" not in comics_columns:
                    raise sqlite3.DatabaseError("comics.ComicLocation is unavailable")
                inventory_queries = [
                    "SELECT ComicLocation AS stored_location, "
                    "0 AS source_kind, rowid AS source_rowid FROM comics "
                    "WHERE ComicLocation IS NOT NULL AND ComicLocation != ''"
                ]
                issues_columns = _sqlite_table_columns(connection, "issues")
                if {"ComicID", "Location"}.issubset(issues_columns) and "ComicID" in (
                    comics_columns
                ):
                    inventory_queries.append(
                        "SELECT issue.Location AS stored_location, "
                        "1 AS source_kind, issue.rowid AS source_rowid FROM issues AS issue "
                        "WHERE issue.Location IS NOT NULL AND issue.Location != '' "
                        "AND substr(issue.Location, 1, 1) = '/' "
                        "AND EXISTS ("
                        "SELECT 1 FROM comics AS comic "
                        "WHERE comic.ComicID = issue.ComicID"
                        ")"
                    )
                query = (
                    "SELECT stored_location FROM ("
                    + " UNION ALL ".join(inventory_queries)
                    + ") ORDER BY stored_location, source_kind, source_rowid LIMIT ?"
                )
                cursor = connection.execute(query, (_MAX_LOCATIONS + 1,))
                rows = cursor.fetchmany(_MAX_LOCATIONS + 1)
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise ValueError("Mylar database does not expose ComicLocation values") from exc
        partial = len(rows) > _MAX_LOCATIONS
        return [str(row[0]) for row in rows[:_MAX_LOCATIONS]], partial

    @staticmethod
    def _snapshot_roots(
        roots: list[LibraryRoot],
    ) -> tuple[list[_RootBoundary], list[str]]:
        snapshots: list[_RootBoundary] = []
        warnings: list[str] = []
        for root in roots:
            try:
                lexical = Path(root.path).expanduser().absolute()
                resolved = Path(root.path).expanduser().resolve(strict=True)
                stat_result = resolved.stat()
            except (OSError, RuntimeError, ValueError):
                warnings.append("library_root_unavailable")
                continue
            if not resolved.is_dir() or not os.access(resolved, os.R_OK | os.X_OK):
                warnings.append("library_root_unreadable")
                continue
            snapshots.append(
                _RootBoundary(
                    root_id=root.id,
                    name=root.name,
                    lexical=lexical,
                    resolved=resolved,
                    device=stat_result.st_dev,
                    inode=stat_result.st_ino,
                )
            )

        for index, left in enumerate(snapshots):
            for right in snapshots[index + 1 :]:
                if (left.device, left.inode) == (right.device, right.inode):
                    warnings.append("library_root_alias")
                if _paths_nested(left.lexical, right.lexical) or _paths_nested(
                    left.resolved, right.resolved
                ):
                    warnings.append("nested_library_roots_ambiguous")
        return snapshots, list(dict.fromkeys(warnings))

    def _mapping_states(
        self,
        path_map: dict[str, str],
        provenance: Literal["automatic", "manual"],
        roots: list[_RootBoundary],
        *,
        require_library_root: bool,
    ) -> dict[str, _MappingState]:
        states: dict[str, _MappingState] = {}
        for stored_prefix, pullbox_prefix in path_map.items():
            state = _MappingState(
                stored_prefix=stored_prefix,
                pullbox_prefix=pullbox_prefix,
                provenance=provenance,
            )
            try:
                target = Path(pullbox_prefix)
                resolved = target.resolve(strict=True)
                containing = _containing_roots(target.absolute(), resolved, roots)
                if not target.is_absolute() or is_sensitive_path(resolved):
                    state.blockers.append("mapping_target_unsafe")
                elif not resolved.is_dir():
                    state.blockers.append("mapping_target_unavailable")
                elif not os.access(resolved, os.R_OK | os.X_OK):
                    state.blockers.append("mapping_target_unreadable")
                elif len(containing) == 1:
                    state.root = containing[0]
                elif not containing and require_library_root:
                    state.blockers.append("mapping_target_outside_enabled_root")
                elif len(containing) > 1:
                    state.blockers.append("mapping_target_root_ambiguous")
            except (OSError, RuntimeError, ValueError):
                state.blockers.append("mapping_target_unavailable")
            states[stored_prefix] = state
        return states

    def _resolve_location(
        self,
        raw_location: str,
        roots: list[_RootBoundary],
        mapping_states: dict[str, _MappingState],
        ordered_map: list[tuple[str, str]],
        *,
        require_library_root: bool,
    ) -> tuple[
        MylarPathOutcome,
        _RootBoundary | None,
        _MappingState | None,
        str,
    ]:
        if (
            not raw_location
            or len(raw_location) > 4096
            or not raw_location.isprintable()
            or ".." in Path(raw_location).parts
        ):
            return "invalid", None, None, "Unavailable example"
        location = Path(raw_location)
        if not location.is_absolute():
            return "invalid", None, None, "Unavailable example"

        identity_outside_root = False
        try:
            resolved_identity = location.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            resolved_identity = None
        if resolved_identity is not None and _supported_location_kind(resolved_identity):
            if not _location_is_readable(resolved_identity):
                return "unreadable", None, None, location.name
            containing = _containing_roots(location.absolute(), resolved_identity, roots)
            if len(containing) == 1:
                root = containing[0]
                return "identity", root, None, _safe_relative(location, root.lexical)
            if len(containing) > 1:
                return "ambiguous", None, None, location.name
            if require_library_root:
                identity_outside_root = True
            else:
                return "identity", None, None, location.name

        for stored_prefix, pullbox_prefix in ordered_map:
            stored_root = Path(stored_prefix)
            try:
                relative = location.relative_to(stored_root)
            except ValueError:
                continue
            state = mapping_states[stored_prefix]
            mapped = Path(pullbox_prefix) / relative
            try:
                mapped_root = Path(pullbox_prefix).resolve(strict=True)
                resolved_mapped = mapped.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                return "mapped_missing", state.root, state, str(relative)
            if not resolved_mapped.is_relative_to(mapped_root):
                return "outside_root", None, state, str(relative)
            if not _supported_location_kind(resolved_mapped):
                return "mapped_missing", state.root, state, str(relative)
            if not _location_is_readable(resolved_mapped):
                return "unreadable", state.root, state, str(relative)
            containing = _containing_roots(mapped.absolute(), resolved_mapped, roots)
            if len(containing) == 1 and state.root is not None:
                return "mapped", containing[0], state, str(relative)
            if len(containing) > 1:
                return "ambiguous", None, state, str(relative)
            if require_library_root:
                return "outside_root", None, state, str(relative)
            return "mapped", None, state, str(relative)
        if identity_outside_root:
            return "outside_root", None, None, location.name
        return "unmapped", None, None, location.name

    def _auto_detect_map(
        self,
        locations: list[str],
        roots: list[_RootBoundary],
    ) -> tuple[dict[str, str], set[str]]:
        candidates_by_location: dict[str, set[tuple[str, str]]] = {}
        support: dict[tuple[str, str], int] = {}
        for raw_location in locations:
            location = Path(raw_location)
            if not location.is_absolute() or ".." in location.parts:
                continue
            try:
                identity = location.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                identity = None
            if identity is not None and _supported_location_kind(identity):
                continue
            location_candidates: set[tuple[str, str]] = set()
            for root in roots:
                for candidate_stored_prefix in _stored_prefixes(location):
                    relative = location.relative_to(candidate_stored_prefix)
                    translated = root.lexical / relative
                    try:
                        resolved = translated.resolve(strict=True)
                    except (OSError, RuntimeError, ValueError):
                        continue
                    if (
                        _supported_location_kind(resolved)
                        and _location_is_readable(resolved)
                        and resolved.is_relative_to(root.resolved)
                    ):
                        pair = (str(candidate_stored_prefix), str(root.lexical))
                        location_candidates.add(pair)
                        break
            if location_candidates:
                candidates_by_location[raw_location] = location_candidates
                for candidate in location_candidates:
                    support[candidate] = support.get(candidate, 0) + 1

        selected: dict[str, str] = {}
        ambiguous: set[str] = set()
        for raw_location, candidates in candidates_by_location.items():
            best_score = max(support[candidate] for candidate in candidates)
            best = sorted(candidate for candidate in candidates if support[candidate] == best_score)
            if len(best) != 1:
                ambiguous.add(raw_location)
                continue
            stored_prefix, pullbox_prefix = best[0]
            existing = selected.get(stored_prefix)
            if existing is not None and existing != pullbox_prefix:
                ambiguous.add(raw_location)
                continue
            selected[stored_prefix] = pullbox_prefix

        if has_conflicting_overlapping_mappings(selected):
            return {}, set(candidates_by_location)
        return normalize_mylar3_path_map(selected), ambiguous

    @staticmethod
    def _mapping_response(state: _MappingState) -> MylarPathMappingPreview:
        blocked = bool(state.blockers)
        needs_review = state.counts.mapped_missing > 0 or state.counts.unmapped > 0
        return MylarPathMappingPreview(
            stored_prefix=state.stored_prefix,
            pullbox_prefix=state.pullbox_prefix,
            library_root_id=state.root.root_id if state.root is not None else None,
            library_root_name=state.root.name if state.root is not None else None,
            provenance=state.provenance,
            status="blocked" if blocked else "review" if needs_review else "ready",
            resolution=state.counts.response(),
            examples=state.examples,
            warnings=state.warnings,
            blocking_reasons=state.blockers,
        )


def _count_field(outcome: MylarPathOutcome) -> str:
    return {
        "identity": "identity_resolved",
        "mapped": "mapped_existing",
        "mapped_missing": "mapped_missing",
        "unmapped": "unmapped",
        "outside_root": "outside_root",
        "unreadable": "unreadable",
        "ambiguous": "ambiguous",
        "invalid": "invalid",
    }[outcome]


def _append_example(
    examples: list[MylarPathExample],
    relative: str,
    outcome: MylarPathOutcome,
) -> None:
    if len(examples) >= _MAX_EXAMPLES:
        return
    examples.append(MylarPathExample(relative_path=relative, outcome=outcome))


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _containing_roots(
    lexical_path: Path,
    resolved_path: Path,
    roots: Iterable[_RootBoundary],
) -> list[_RootBoundary]:
    return [
        root
        for root in roots
        if lexical_path.is_relative_to(root.lexical) and resolved_path.is_relative_to(root.resolved)
    ]


def _paths_nested(left: Path, right: Path) -> bool:
    return left != right and (left.is_relative_to(right) or right.is_relative_to(left))


def _stored_prefixes(location: Path) -> list[Path]:
    parts = location.parts
    if len(parts) < 3 or parts[0] != "/":
        return []
    segments = parts[1:]
    return [Path("/", *segments[:index]) for index in range(len(segments) - 1, 0, -1)]


def _sqlite_table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """Return columns for one fixed Mylar table without interpolating user input."""
    statements = {
        "comics": "PRAGMA table_info(comics)",
        "issues": "PRAGMA table_info(issues)",
    }
    statement = statements.get(table)
    if statement is None:
        raise ValueError("Unsupported Mylar path-inventory table")
    return {str(row[1]) for row in connection.execute(statement)}


def _supported_location_kind(path: Path) -> bool:
    """Return whether a Mylar location resolves to a series directory or issue file."""
    return path.is_dir() or path.is_file()


def _location_is_readable(path: Path) -> bool:
    """Require read access for files and read/traverse access for directories."""
    required_access = os.R_OK | os.X_OK if path.is_dir() else os.R_OK
    return os.access(path, required_access)
