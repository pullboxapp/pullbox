"""Durable Library conversion admission, registration and evidence-based recovery."""

import asyncio
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import ValidationError
from pullbox.core.file_safety import (
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
)
from pullbox.core.library_file_ownership import require_mutable_library_target
from pullbox.models import LibraryFile, LibraryRoot
from pullbox.models.library import LibraryFileStorageMode
from pullbox.models.library_conversion import LibraryConversion
from pullbox.services.archive_metadata_binding import (
    ArchiveMetadataBindingError,
    lock_archive_metadata_binding,
    read_archive_metadata_binding,
    require_unowned_metadata_file,
)
from pullbox.services.archive_metadata_publication import _fingerprint
from pullbox.services.library_conversion_files import (
    ConversionBinding,
    ConversionFile,
    ConversionPlan,
    check_directories,
    conversion_metadata_digest,
    decode_plan,
    inspect_conversion,
    matches,
    publish,
    remove_original,
)
from pullbox.services.library_mutation_coordination import (
    finish_short_mutation,
    lock_file_mutation_admission,
)

logger = structlog.get_logger(__name__)


async def _require_conversion_metadata(session: AsyncSession, plan: ConversionPlan) -> None:
    if plan.metadata_state_digest is None:
        return
    if plan.binding.file_id is None or plan.binding.issue_id is None:
        raise ValidationError("Conversion metadata requires a verified issue match.")
    try:
        await require_unowned_metadata_file(session, plan.binding.file_id)
        binding = await read_archive_metadata_binding(
            session,
            plan.binding.file_id,
            expected_issue_id=plan.binding.issue_id,
            allow_conversion_source=True,
        )
        await lock_archive_metadata_binding(session, binding)
        await require_unowned_metadata_file(session, plan.binding.file_id)
        binding = await read_archive_metadata_binding(
            session,
            plan.binding.file_id,
            expected_issue_id=plan.binding.issue_id,
            allow_conversion_source=True,
        )
    except ArchiveMetadataBindingError as exc:
        if exc.code == "import_rollback_protected":
            raise ValidationError(
                "This file still belongs to an import's rollback journal. "
                "Paired conversion is not available for it yet; the file has not been changed."
            ) from None
        raise ValidationError(
            "File metadata needs review before conversion can continue."
        ) from None
    if (
        conversion_metadata_digest(
            binding.metadata,
            await get_archive_size_limit_bytes(session),
            await is_dangerous_file_blocking_enabled(session),
        )
        != plan.metadata_state_digest
    ):
        raise ValidationError("File metadata changed during conversion. Review it before retrying.")


async def read_conversion_binding(session: AsyncSession, source: Path) -> ConversionBinding:
    await require_mutable_library_target(
        session, source, include_descendants=False, operation="converted"
    )
    file = await session.scalar(
        select(LibraryFile)
        .where(LibraryFile.file_path == str(source))
        .execution_options(populate_existing=True)
    )
    roots = list(
        (
            await session.scalars(
                select(LibraryRoot)
                .where(LibraryRoot.enabled.is_(True), LibraryRoot.allow_managed_writes.is_(True))
                .limit(201)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    candidates = [root for root in roots if source.is_relative_to(Path(root.path).resolve())]
    if file is not None:
        candidates = [root for root in candidates if root.id == file.library_root_id]
    elif candidates:
        depth = max(len(Path(root.path).resolve().parts) for root in candidates)
        candidates = [root for root in candidates if len(Path(root.path).resolve().parts) == depth]
    if (
        len(roots) > 200
        or len(candidates) != 1
        or (
            file is not None
            and (
                file.library_root_id != candidates[0].id
                or file.storage_mode is not LibraryFileStorageMode.MANAGED
            )
        )
    ):
        raise ValidationError(
            "Conversion requires one enabled library root that allows managed files."
        )
    root = candidates[0]
    return ConversionBinding(
        root_id=root.id,
        root_path=root.path,
        file_id=file.id if file else None,
        issue_id=file.issue_id if file else None,
        file_format=file.file_format.value if file else None,
        file_size=file.file_size if file else None,
        file_modified_at=file.file_modified_at if file else None,
    )


async def require_no_library_conversion(
    session: AsyncSession,
    *paths: Path,
    include_descendants: bool,
    excluding: str | None = None,
) -> None:
    targets = {variant for path in paths for variant in (path.absolute(), path.resolve())}
    last_id = 0
    while True:
        query = select(
            LibraryConversion.id,
            case(
                (
                    func.length(LibraryConversion.plan_json) <= 65536,
                    LibraryConversion.plan_json,
                ),
                else_=None,
            ),
        ).where(LibraryConversion.active.is_(True), LibraryConversion.id > last_id)
        if excluding is not None:
            query = query.where(LibraryConversion.operation_id != excluding)
        rows = (await session.execute(query.order_by(LibraryConversion.id).limit(8))).all()
        if not rows:
            return
        for row_id, encoded in rows:
            last_id = row_id
            try:
                plan = decode_plan(encoded)
            except (TypeError, ValueError):
                raise ValidationError(
                    "A pending conversion needs recovery before files can change."
                ) from None
            if any(
                reserved == target or (include_descendants and reserved.is_relative_to(target))
                for reserved in plan.paths
                for target in targets
            ):
                raise ValidationError(
                    "A conversion is pending for this file or folder. "
                    "Restart Pullbox to recover it before retrying."
                )


async def record_conversion(
    session: AsyncSession, plan: ConversionPlan, operation_id: UUID
) -> None:
    from pullbox.services.library_mutation_coordination import require_no_archive_publication

    decode_plan(plan.model_dump_json())
    await lock_file_mutation_admission(session)
    await require_no_archive_publication(session, *plan.paths, include_descendants=False)
    if await read_conversion_binding(session, plan.original.path) != plan.binding:
        raise ValidationError("The library registration changed during conversion.")
    await _require_conversion_metadata(session, plan)
    if await session.scalar(
        select(LibraryFile.id).where(LibraryFile.file_path == str(plan.output.path))
    ):
        raise ValidationError("The converted file's destination is already registered.")
    check_directories(plan)
    if _fingerprint(plan.original.path) != plan.original.fingerprint:
        raise ValidationError("The original changed during conversion.")
    session.add(
        LibraryConversion(
            operation_id=str(operation_id),
            library_file_id=plan.binding.file_id,
            plan_json=plan.model_dump_json(),
        )
    )
    await session.flush()
    session.info[f"conversion_intent:{operation_id}"] = session.sync_session.get_transaction()


async def publish_conversion(session: AsyncSession, operation_id: UUID) -> None:
    if (
        session.in_transaction()
        and session.info.get(f"conversion_intent:{operation_id}")
        is session.sync_session.get_transaction()
    ):
        raise ValidationError("Conversion intent must commit before publication.")
    await lock_file_mutation_admission(session)
    row = await session.scalar(
        select(LibraryConversion)
        .where(LibraryConversion.operation_id == str(operation_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or not row.active or row.state != "intended":
        raise ValidationError("Conversion has already been recovered; retry from the Library.")
    plan = decode_plan(row.plan_json)
    if await read_conversion_binding(session, plan.original.path) != plan.binding:
        raise ValidationError("The library registration changed during conversion.")
    await _require_conversion_metadata(session, plan)
    await finish_short_mutation(asyncio.create_task(asyncio.to_thread(publish, plan)))


async def recover_conversion(session: AsyncSession, operation_id: UUID) -> str:
    """Own clean session transactions; never resume a converter or publish a stage."""

    if session.new or session.dirty or session.deleted or session.in_nested_transaction():
        raise ValidationError("Conversion recovery requires a clean session.")
    row = await session.scalar(
        select(LibraryConversion)
        .where(LibraryConversion.operation_id == str(operation_id))
        .execution_options(populate_existing=True)
    )
    if row is None:
        return "missing"
    encoded, state, active = row.plan_json, row.state, row.active
    await session.commit()
    if not active or state == "review":
        return state
    plan = decode_plan(encoded)
    inspected = await inspect_conversion(plan)
    return await _apply_inspected_conversion(session, operation_id, encoded, plan, inspected)


async def _apply_inspected_conversion(
    session: AsyncSession,
    operation_id: UUID,
    encoded: str,
    plan: ConversionPlan,
    inspected: dict[str, ConversionFile | None],
) -> str:
    from pullbox.services.library_convert_service import _sync_converted_file_record

    await lock_file_mutation_admission(session)
    row = await session.scalar(
        select(LibraryConversion)
        .where(LibraryConversion.operation_id == str(operation_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or not row.active or row.state == "review":
        await session.rollback()
        return row.state if row else "missing"
    if row.plan_json != encoded:
        raise ValidationError("Conversion evidence changed during recovery.")
    check_directories(plan)
    for name, actual in inspected.items():
        if _fingerprint(getattr(plan, name).path) != (actual.fingerprint if actual else None):
            raise ValidationError("Conversion files changed during recovery.")
    proven = {
        name: bool(
            actual
            and matches(actual.fingerprint, getattr(plan, name).fingerprint)
            and actual.digest == getattr(plan, name).digest
        )
        for name, actual in inspected.items()
    }
    if inspected["output"] is None and proven["original"] and row.state == "intended":
        row.state, row.active = "abandoned", False
        await session.commit()
        return "abandoned"
    if (
        not proven["output"]
        or not proven["backup"]
        or (inspected["original"] is not None and not proven["original"])
    ):
        row.state = "review"
        await session.commit()
        return "review"

    if row.state == "intended":
        try:
            current = await read_conversion_binding(session, plan.original.path)
            if current != plan.binding or not proven["original"]:
                raise ValidationError("Conversion registration changed.")
            await _require_conversion_metadata(session, plan)
            if await session.scalar(
                select(LibraryFile.id).where(LibraryFile.file_path == str(plan.output.path))
            ):
                raise ValidationError("Conversion destination is already registered.")
            await _sync_converted_file_record(
                session,
                before_path=str(plan.original.path),
                after_path=str(plan.output.path),
                metadata_embedded=plan.metadata_state_digest is not None,
            )
        except ValidationError:
            row.state = "review"
            await session.commit()
            return "review"
        row.state = "registered"
        # This commit atomically records registration. Lost acknowledgements leave
        # the original and the journal intact; subsequent recovery reads DB truth.
        await session.commit()
        return await _apply_inspected_conversion(session, operation_id, encoded, plan, inspected)

    current = await read_conversion_binding(session, plan.output.path)
    expected = plan.binding
    if (
        current.file_id != expected.file_id
        or current.issue_id != expected.issue_id
        or current.root_id != expected.root_id
        or current.root_path != expected.root_path
        or (
            current.file_id is not None
            and (current.file_format != "cbz" or current.file_size != plan.output.fingerprint[2])
        )
    ):
        row.state = "review"
        await session.commit()
        return "review"
    try:
        await _require_conversion_metadata(session, plan)
    except ValidationError:
        row.state = "review"
        await session.commit()
        return "review"
    await finish_short_mutation(asyncio.create_task(remove_original(plan, inspected["original"])))
    row.state, row.active = "complete", False
    await session.commit()
    return "complete"


async def recover_library_conversions(session: AsyncSession) -> int:
    """Bounded startup pass; unresolved evidence stays reserved and is logged."""
    if session.new or session.dirty or session.deleted or session.in_nested_transaction():
        raise ValidationError("Conversion recovery requires a clean session.")
    last_id = completed = 0
    while True:
        rows = (
            await session.execute(
                select(LibraryConversion.id, LibraryConversion.operation_id)
                .where(
                    LibraryConversion.active.is_(True),
                    LibraryConversion.state != "review",
                    LibraryConversion.id > last_id,
                )
                .order_by(LibraryConversion.id)
                .limit(8)
            )
        ).all()
        await session.commit()
        if not rows:
            return completed
        for row_id, operation in rows:
            last_id = row_id
            try:
                state = await recover_conversion(session, UUID(operation))
                completed += state in {"complete", "abandoned"}
                logger.info("library_conversion_recovered", operation_id=operation, state=state)
            except Exception:
                await session.rollback()
                logger.warning(
                    "library_conversion_recovery_deferred", operation_id=operation, exc_info=True
                )
