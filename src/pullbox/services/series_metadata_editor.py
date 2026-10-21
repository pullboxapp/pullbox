"""Canonical series corrections with retained, independently guarded sidecar work."""

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast as type_cast

from pydantic import TypeAdapter
from sqlalchemy import JSON, Text, cast, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.metadata_identity import MetadataEntityKind
from pullbox.core.series_sidecar import read_series_sidecar, render_series_sidecar
from pullbox.core.source_sidecars import parse_source_sidecar
from pullbox.models import Series
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.series import SeriesStatus, SeriesType
from pullbox.schemas.metadata_snapshot import (
    FieldOrigin,
    MetadataSnapshot,
    MetadataValues,
    field_domain,
)
from pullbox.schemas.series_metadata_editor import (
    EditableSeriesField,
    SeriesMetadataEditorField,
    SeriesMetadataEditorOption,
    SeriesMetadataEditorPreview,
    SeriesMetadataEditorReview,
    SeriesMetadataEditorSource,
)
from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError
from pullbox.services.library_mutation_coordination import (
    finish_short_mutation,
    lock_file_mutation_admission,
)
from pullbox.services.metadata_assembly import assemble_metadata
from pullbox.services.metadata_baselines import MetadataBaselineWrite, save_metadata_baselines
from pullbox.services.metadata_entity_values import apply_series_metadata_values
from pullbox.services.metadata_series_refresh_state import (
    SeriesRefreshState,
    read_series_refresh_state,
)
from pullbox.services.metadata_writer_identity import metadata_write_scope
from pullbox.services.provider_artwork import allowed_artwork_url
from pullbox.services.series_sidecar import (
    SidecarBinding,
    SidecarTarget,
    inspect_series_sidecar_binding,
    read_series_sidecar_binding,
    write_editor_sidecar_target,
)
from pullbox.utilities.models import ItemState, JobType, UtilityJob, UtilityJobItem

FIELDS = {
    "title": ("Title", "text"),
    "sort_title": ("Sort title", "text"),
    "publisher": ("Publisher", "text"),
    "description": ("Description", "textarea"),
    "year_start": ("Start year", "number"),
    "year_end": ("End year", "number"),
    "status": ("Status", "select"),
    "series_type": ("Type", "select"),
    "archive_format": ("Publication format", "select"),
    "volume": ("Volume", "text"),
    "language": ("Language", "text"),
}
REQUIRED = frozenset({"title", "sort_title", "status", "series_type"})
_MAX_PLAN_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class SeriesEditorPlan:
    state: SeriesRefreshState
    snapshot: MetadataSnapshot
    binding: SidecarBinding | None
    targets: tuple[SidecarTarget, ...]
    edited_fields: frozenset[str]
    mode: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedSeriesEditor:
    state: SeriesRefreshState
    snapshot: MetadataSnapshot
    binding: SidecarBinding | None
    targets: tuple[SidecarTarget, ...]
    preview: SeriesMetadataEditorPreview


PLAN = TypeAdapter(SeriesEditorPlan)


def _display(value: object) -> str:
    return "" if value is None else str(value)


def _library_snapshot(state: SeriesRefreshState, now: datetime) -> MetadataSnapshot:
    """Protected artwork is retained, not validated as permission for file output."""
    previous = state.series.baseline
    snapshot = assemble_metadata(
        MetadataEntityKind.SERIES,
        state.series.identities,
        (),
        state.policies,
        now=now,
        current=state.series.values.model_copy(update={"image_url": None}),
        previous=previous.model_copy(
            update={
                "values": previous.values.model_copy(update={"image_url": None}),
            }
        )
        if previous
        else None,
        overrides=state.series.overrides,
    )
    artwork = (
        next((origin for origin in previous.origins if origin.field == "image_url"), None)
        if previous
        else None
    )
    if previous is not None and previous.values.image_url != state.series.values.image_url:
        artwork = FieldOrigin(
            field="image_url",
            domain=field_domain(MetadataEntityKind.SERIES, "image_url"),
            observed_at=now,
            user_override=True,
        )
    return snapshot.model_copy(
        update={
            "values": snapshot.values.model_copy(
                update={"image_url": state.series.values.image_url}
            ),
            "origins": tuple(origin for origin in snapshot.origins if origin.field != "image_url")
            + ((artwork,) if artwork else ()),
        }
    )


def _validated(field: str, value: object) -> object:
    label = FIELDS[field][0]
    if isinstance(value, str):
        if not value.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise ValueError(f"Enter a valid {label.lower()} or choose Clear value.")
        if field != "description":
            value = value.strip()
    if value is None and field in REQUIRED:
        raise ValueError(f"{label} cannot be cleared.")
    try:
        if field == "status" and value is not None:
            if not isinstance(value, str):
                raise ValueError("Expected status text")
            value = SeriesStatus(value).value
        if field == "series_type" and value is not None:
            if not isinstance(value, str):
                raise ValueError("Expected series type text")
            value = SeriesType(value).value
        return getattr(MetadataValues.model_validate({field: value}), field)
    except (ValueError, TypeError):
        raise ValueError(f"Enter a valid {label.lower()}.") from None


def _saved_sources(
    binding: SidecarBinding | None, targets: tuple[SidecarTarget, ...]
) -> dict[str, tuple[str, dict[str, object]]]:
    sources: dict[str, tuple[str, dict[str, object]]] = {}
    if binding is None:
        return sources
    for target in targets:
        if target.read.action == "blocked" or target.previous is None:
            continue
        data = read_series_sidecar(target.previous)
        legacy = parse_source_sidecar(target.previous.decode())
        layers: list[tuple[str, dict[str, object]]] = []
        prior = data.get("pullbox")
        if isinstance(prior, dict):
            snapshot = MetadataSnapshot.model_validate(prior["snapshot"])
            if snapshot.identities and set(snapshot.identities) <= set(binding.snapshot.identities):
                layers.append(("Pullbox snapshot", snapshot.values.model_dump()))
        if legacy.get("comicid") is not None and any(
            ref.namespace.value == "comicvine" and ref.external_id == str(legacy["comicid"])
            for ref in binding.snapshot.identities
        ):
            for name, layer in [
                ("saved fields", data),
                *[
                    (key, item)
                    for key, item in data.items()
                    if key.casefold() == "metadata" and isinstance(item, dict)
                ],
            ]:
                mapped: dict[str, object] = {}
                aliases = {
                    "title": {"name", "series", "title"},
                    "year_start": {"year", "year_start"},
                    "description": {"summary", "description"},
                    "series_type": {"type", "booktype", "series_type"},
                    "status": {"status", "series_status"},
                }
                for field in FIELDS:
                    found = [
                        value
                        for key, value in layer.items()
                        if key.casefold() in aliases.get(field, {field})
                    ]
                    if found and all(value == found[0] for value in found):
                        mapped[field] = (
                            "standard"
                            if field == "series_type" and found[0] == "Print"
                            else found[0]
                        )
                layers.append((name, mapped))
        for name, values in layers:
            valid = {}
            for field in FIELDS:
                if field not in values or values[field] is None:
                    continue
                try:
                    valid[field] = _validated(field, values[field])
                except ValueError:
                    continue
            token = hashlib.sha256(f"{target.location.directory}\0{name}".encode()).hexdigest()
            sources[token] = (
                f"series.json ({Path(target.location.directory).name}, {name})",
                valid,
            )
    return sources


def _render_targets(
    binding: SidecarBinding, targets: tuple[SidecarTarget, ...], edited_fields: frozenset[str]
) -> tuple[SidecarTarget, ...]:
    """Render from the reviewed bytes, never silently recapture a changed source."""
    result = []
    for target in targets:
        if target.read.action == "blocked":
            result.append(target)
            continue
        try:
            content = render_series_sidecar(
                binding.snapshot,
                binding.aliases,
                target.previous,
                updated_at=binding.updated_at,
                edited_fields=edited_fields,
            )
            unchanged = target.previous is not None and json.loads(target.previous) == json.loads(
                content
            )
            read = target.read.model_copy(
                update={
                    "action": "unchanged"
                    if unchanged
                    else "create"
                    if target.previous is None
                    else "update",
                    "changes": [] if unchanged else ["Selected series fields and saved provenance"],
                }
            )
        except ValueError:
            content = b""
            read = target.read.model_copy(
                update={
                    "action": "blocked",
                    "reason": (
                        "Selected metadata cannot be safely written to this sidecar. "
                        "Review the values or use Library only."
                    ),
                }
            )
        result.append(replace(target, content=content, read=read))
    return tuple(result)


async def prepare_series_editor(
    session: AsyncSession, series_id: int, review: SeriesMetadataEditorReview
) -> PreparedSeriesEditor:
    if session.new or session.dirty or session.deleted:
        raise ValueError("Finish pending changes before editing series metadata.")
    state = await read_series_refresh_state(session, series_id, include_issues=False)
    series = await session.get(Series, series_id)
    assert series is not None
    snapshot = _library_snapshot(state, series.updated_at)
    binding = None
    targets: tuple[SidecarTarget, ...] = ()
    warnings: list[str] = []
    unsupported_artwork = bool(
        state.series.values.image_url and not allowed_artwork_url(state.series.values.image_url)
    )
    if review.mode != "library_only" and unsupported_artwork:
        warnings.append(
            "Sidecars are unavailable until the protected artwork URL is reviewed. "
            "Library metadata can still be saved."
        )
    elif review.mode != "library_only":
        try:
            binding = await read_series_sidecar_binding(session, series_id)
            targets = await inspect_series_sidecar_binding(binding)
        except ArchiveMetadataBindingError:
            warnings.append(
                "Sidecars are unavailable until the series provider links are verified. "
                "Library metadata can still be saved."
            )
    sources = _saved_sources(binding, targets)
    original = targets
    values = snapshot.values.model_dump()
    origins = {origin.field: origin for origin in snapshot.origins}
    fields = []
    for field, (label, input_type) in FIELDS.items():
        current = getattr(state.series.values, field)
        previous = origins.get(field)
        edit = review.edits.get(type_cast("EditableSeriesField", field))
        if edit is not None:
            if edit.action == "reset":
                if previous is None or not previous.user_override:
                    raise ValueError(f"{label} has no manual override to remove.")
            elif edit.action == "set":
                values[field] = _validated(field, edit.value)
            elif edit.action == "clear":
                values[field] = _validated(field, None)
            else:
                if edit.action == "sidecar" and (
                    edit.source not in sources or field not in sources[edit.source][1]
                ):
                    raise ValueError(
                        f"The saved source for {label.lower()} is unavailable. Preview again."
                    )
                selected = (
                    current
                    if edit.action == "library"
                    else sources.get(edit.source or "", ("", {}))[1].get(field)
                )
                if selected is None or selected == "":
                    if current not in (None, "") or any(
                        source.get(field) not in (None, "") for _, source in sources.values()
                    ):
                        raise ValueError(
                            f"{label} has no value in that saved source. "
                            "Choose Clear value to remove it."
                        )
                    values[field] = selected
                else:
                    values[field] = _validated(field, selected)
            origins[field] = FieldOrigin(
                field=field,
                domain=field_domain(snapshot.entity_kind, field),
                observed_at=series.updated_at,
                user_override=edit.action != "reset",
                provider_managed=edit.action == "reset",
            )
        choices = (
            [item.value for item in SeriesStatus]
            if field == "status"
            else [item.value for item in SeriesType]
            if field == "series_type"
            else ["Limited Series", "Digital Chapter"]
            if field == "archive_format"
            else []
        )
        fields.append(
            SeriesMetadataEditorField(
                key=f"series.{field}",
                label=label,
                current=_display(current),
                proposed=_display(values[field]),
                input_type=input_type,
                overridden=bool(previous and previous.user_override),
                selected=edit is not None,
                changed=edit is not None,
                can_clear=field not in REQUIRED,
                options=[
                    SeriesMetadataEditorOption(value=value, label=value.replace("_", " ").title())
                    for value in choices
                ],
                sources=[
                    SeriesMetadataEditorSource(
                        token="library", label="Library", value=_display(current)
                    ),
                    *[
                        SeriesMetadataEditorSource(
                            token=token, label=name, value=_display(source[field])
                        )
                        for token, (name, source) in sources.items()
                        if field in source
                    ],
                ],
            )
        )
    proposed = MetadataValues.model_validate(values)
    if (
        proposed.year_start is not None
        and proposed.year_end is not None
        and proposed.year_end < proposed.year_start
    ):
        raise ValueError("End year cannot be earlier than start year.")
    snapshot = MetadataSnapshot.model_validate(
        {**snapshot.model_dump(), "values": proposed, "origins": tuple(origins.values())}
    )
    if binding is not None:
        targets = _render_targets(
            replace(binding, snapshot=snapshot), targets, frozenset(review.edits)
        )
    protected = [
        ("issue_count", "Issue count", state.series.values.issue_count),
        ("image_url", "Cover artwork", state.series.values.image_url),
        *[
            (
                f"id.{identity.namespace.value}",
                f"{identity.namespace.value} ID",
                identity.external_id,
            )
            for identity in state.series.identities
        ],
    ]
    fields.extend(
        SeriesMetadataEditorField(
            key=f"series.{field}",
            label=label,
            current=_display(value),
            proposed=_display(value),
            editable=False,
            can_clear=False,
            reason="Protected catalog or identity field; use its dedicated workflow.",
        )
        for field, label, value in protected
    )
    evidence = TypeAdapter(SeriesRefreshState).dump_json(state) + review.model_dump_json().encode()
    evidence += TypeAdapter(SidecarBinding | None).dump_json(binding)
    evidence += TypeAdapter(tuple[SidecarTarget, ...]).dump_json(original)
    if len(evidence) > _MAX_PLAN_BYTES:
        raise ValueError(
            "This sidecar review is too large. Use Library only or review fewer folders."
        )
    preview = SeriesMetadataEditorPreview(
        series_id=series_id,
        mode=review.mode,
        editor_fields=fields,
        editor_changed=bool(review.edits),
        targets=[target.read for target in targets],
        ready=bool(review.edits),
        review_key=hashlib.sha256(evidence).hexdigest(),
        warnings=warnings,
    )
    return PreparedSeriesEditor(state, snapshot, binding, targets, preview)


async def save_series_editor(
    session: AsyncSession, series_id: int, review: SeriesMetadataEditorReview, review_key: str
) -> SeriesEditorPlan:
    prepared = await prepare_series_editor(session, series_id, review)
    if not prepared.preview.ready or prepared.preview.review_key != review_key:
        raise ValueError(
            "The selected values, library metadata or sidecars changed. Preview again."
        )
    # Release the read snapshot before entering SQLite's writer admission path.
    await session.rollback()
    await lock_file_mutation_admission(session)
    await session.execute(
        select(MetadataSourceConfig.id)
        .order_by(MetadataSourceConfig.source)
        .with_for_update(read=True)
    )
    await session.execute(select(Series.id).where(Series.id == series_id).with_for_update())
    if await read_series_refresh_state(session, series_id, include_issues=False) != prepared.state:
        raise ValueError("Library metadata changed. Preview again.")
    if (
        prepared.binding is not None
        and await read_series_sidecar_binding(session, series_id) != prepared.binding
    ):
        raise ValueError("Series folder ownership changed. Preview again.")
    now = datetime.now(UTC)
    snapshot = prepared.snapshot.model_copy(
        update={
            "origins": tuple(
                origin.model_copy(update={"observed_at": now})
                if origin.field in review.edits
                else origin
                for origin in prepared.snapshot.origins
            )
        }
    )
    async with metadata_write_scope(session):
        series = await session.get(Series, series_id)
        assert series is not None
        if {"status", "year_end"} & review.edits.keys():
            # The legacy pin owns both fields. Assembly retained the unselected pin as provenance.
            series.status_override = None
        await apply_series_metadata_values(session, series, snapshot.values)
        series.updated_at = now
        await save_metadata_baselines(
            session,
            [MetadataBaselineWrite(series_id, snapshot, prepared.state.series.baseline_revision)],
        )
        await session.flush()
        state = await read_series_refresh_state(session, series_id, include_issues=False)
        binding = None
        targets = prepared.targets
        if prepared.binding is not None:
            binding = replace(
                await read_series_sidecar_binding(session, series_id), snapshot=snapshot
            )
            targets = _render_targets(binding, targets, frozenset(review.edits))
        plan = SeriesEditorPlan(
            state,
            snapshot,
            binding,
            targets,
            frozenset(review.edits),
            review.mode,
            tuple(prepared.preview.warnings),
        )
        if len(PLAN.dump_json(plan)) > _MAX_PLAN_BYTES:
            raise ValueError("The retained sidecar plan is too large. Use Library only.")
        return plan


def read_editor_plan(config: dict[str, object]) -> SeriesEditorPlan:
    if set(config) != {"series_id", "series_editor"} or type(config["series_id"]) is not int:
        raise ValueError("Invalid series metadata job.")
    plan = PLAN.validate_python(config["series_editor"])
    if (
        plan.state.series.local_id != config["series_id"]
        or plan.state.issues
        or plan.snapshot != plan.state.series.baseline
        or not plan.edited_fields <= FIELDS.keys()
        or plan.mode not in {"library_only", "library_and_sidecars"}
        or len(plan.targets) > 50
        or len({target.location.directory for target in plan.targets}) != len(plan.targets)
        or (plan.mode == "library_only" and (plan.binding is not None or plan.targets))
        or (plan.targets and plan.binding is None)
        or (
            plan.binding is not None
            and (
                plan.binding.snapshot != plan.snapshot
                or plan.binding.series_id != plan.state.series.local_id
            )
        )
        or len(PLAN.dump_json(plan)) > _MAX_PLAN_BYTES
    ):
        raise ValueError("Invalid retained series metadata plan.")
    return plan


async def run_series_editor_target(
    session: AsyncSession, plan: SeriesEditorPlan, index: int
) -> dict[str, object]:
    if plan.binding is None or index < 0 or index >= len(plan.targets):
        raise ValueError("Invalid retained sidecar target.")
    if (
        await read_series_refresh_state(session, plan.state.series.local_id, include_issues=False)
        != plan.state
    ):
        raise ValueError("Library metadata is saved but has changed since this job. Preview again.")
    await session.rollback()
    result = await finish_short_mutation(
        asyncio.create_task(
            write_editor_sidecar_target(
                session,
                plan.binding,
                plan.targets[index],
                plan.edited_fields,
            )
        )
    )
    return {
        "directory": result.directory,
        "outcome": "saved" if result.action == "unchanged" else "failed",
        "reason": result.reason,
    }


async def latest_editor_job(session: AsyncSession, series_id: int) -> UtilityJob | None:
    document = JSON().with_variant(Text(), "sqlite")
    return await session.scalar(
        select(UtilityJob)
        .where(
            UtilityJob.job_type == JobType.FILE_METADATA,
            type_coerce(cast(UtilityJob.config, document), JSON)["series_id"].as_integer()
            == series_id,
        )
        .order_by(UtilityJob.created_at.desc(), UtilityJob.id.desc())
        .limit(1)
    )


async def editor_job_details(session: AsyncSession, job: UtilityJob) -> dict[str, object]:
    plan = read_editor_plan(json.loads(job.config))
    items = list(
        await session.scalars(
            select(UtilityJobItem)
            .where(UtilityJobItem.job_id == job.id)
            .order_by(UtilityJobItem.item_index)
            .limit(51)
        )
    )
    indexed = {json.loads(item.before_state or "{}").get("target_index"): item for item in items}
    targets = []
    for index, target in enumerate(plan.targets):
        item = indexed.get(index)
        targets.append(
            {
                "directory": target.location.directory,
                "state": item.state
                if item
                else "SKIPPED"
                if target.read.action == "blocked"
                else "COMPLETED"
                if target.read.action == "unchanged"
                else "PENDING",
                "reason": item.error_message if item else target.read.reason,
            }
        )
    unfinished = [target for target in targets if target["state"] not in {"COMPLETED", "SKIPPED"}]
    blocked = any(target.read.action == "blocked" for target in plan.targets)
    warnings = list(plan.warnings)
    if blocked:
        warnings.append(
            "Some folders were ineligible and were not written. "
            "Review their reasons before a new preview."
        )
    return {
        "library_saved": True,
        "mode": plan.mode,
        "targets": targets,
        "partial": bool(unfinished) or blocked or bool(warnings),
        "retryable": bool(unfinished) and job.is_terminal,
        "warnings": warnings,
    }


async def retry_editor_plan(session: AsyncSession, series_id: int, job_id: str) -> SeriesEditorPlan:
    job = await session.get(UtilityJob, job_id)
    if job is None or job.job_type != JobType.FILE_METADATA or not job.is_terminal:
        raise ValueError("Choose a finished or cancelled series metadata job.")
    plan = read_editor_plan(json.loads(job.config))
    if plan.state.series.local_id != series_id:
        raise ValueError("The metadata job belongs to another series.")
    if await read_series_refresh_state(session, series_id, include_issues=False) != plan.state:
        raise ValueError(
            "Library metadata changed. Preview again; retry will not reapply old edits."
        )
    items = list(
        await session.scalars(
            select(UtilityJobItem).where(UtilityJobItem.job_id == job.id).limit(51)
        )
    )
    completed = {
        json.loads(item.before_state or "{}").get("target_index")
        for item in items
        if item.state in {ItemState.COMPLETED, ItemState.SKIPPED}
    }
    remaining = tuple(
        target
        for index, target in enumerate(plan.targets)
        if index not in completed and target.read.action in {"create", "update"}
    )
    if not remaining:
        raise ValueError("No unfinished approved sidecars remain.")
    return replace(plan, targets=remaining)
