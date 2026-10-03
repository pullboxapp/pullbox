"""Saved identity evidence and explicit operator review."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from sqlalchemy import case, delete, false, func, insert, or_, select, true, update
from sqlalchemy.exc import IntegrityError

from pullbox.core.metadata_identity import (
    ExactIdentityEvidence,
    ExternalIdentityRef,
    IdentityEvidenceKind,
    IdentityNamespace,
    MetadataEntityKind,
)
from pullbox.core.metadata_identity_events import (
    IdentityEventActor,
    IdentityEventEvidence,
    IdentityEventReplayConflictError,
    IdentityEventRequest,
    prepare_identity_event,
    validate_identity_event_replay,
)
from pullbox.core.metadata_identity_state import (
    IdentityReviewRequiredError,
    transition_identity_state,
)
from pullbox.core.metadata_identity_state import (
    IdentityVerificationAction as Action,
)
from pullbox.core.metadata_identity_state import (
    IdentityVerificationState as State,
)
from pullbox.models import Base, Issue, Series, User
from pullbox.services.metadata_identity_attachment import (
    IdentityAttachmentConflictError,
    IdentityAttachmentReceipt,
    _lock_parent_graph,
    _locked_targets,
    _validate_issue_parents,
    _validate_legacy_owners,
)

if TYPE_CHECKING:
    from sqlalchemy import RowMapping, Table
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


_REVIEW_NAMESPACE = UUID("0d7b2c0d-1515-43b2-a9dd-c9a6d516d60c")


class IdentityReviewNotFoundError(ValueError):
    """The requested saved claim is not part of this local target."""


def _tables(kind: MetadataEntityKind) -> tuple[Table, Table, str]:
    return (
        Base.metadata.tables[f"{kind.value}_external_identities"],
        Base.metadata.tables[f"{kind.value}_identity_events"],
        f"{kind.value}_id",
    )


def _scope(active: Table, kind: MetadataEntityKind) -> ColumnElement[bool]:
    return active.c.namespace == "story_arc" if kind is MetadataEntityKind.STORY_ARC else true()


def _namespace(active: Table, kind: MetadataEntityKind) -> ColumnElement[str]:
    return active.c.source if kind is MetadataEntityKind.STORY_ARC else active.c.identity_namespace


async def _begin_write(session: AsyncSession) -> None:
    if session.get_bind().dialect.name == "sqlite":
        await session.execute(
            update(Series).where(false()).values(comicvine_id=Series.comicvine_id)
        )


async def _replay(
    session: AsyncSession, request: IdentityEventRequest
) -> IdentityAttachmentReceipt | None:
    _, history, target_key = _tables(request.evidence.claim.identity.entity_kind)
    payload = prepare_identity_event(request)
    row = (
        (
            await session.execute(
                select(history).where(
                    history.c[target_key] == request.local_id,
                    history.c.event_key == payload.event_key,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    validate_identity_event_replay(
        payload, event_key=row.event_key, request_fingerprint=row.request_fingerprint
    )
    if row.request_json != payload.request_json:
        raise IdentityEventReplayConflictError("Stored identity request is inconsistent")
    return IdentityAttachmentReceipt(row.id, True)


async def _append(
    session: AsyncSession, request: IdentityEventRequest, state: State
) -> IdentityAttachmentReceipt:
    identity = request.evidence.claim.identity
    _, history, target_key = _tables(identity.entity_kind)
    payload = prepare_identity_event(request)
    event_id = await session.scalar(
        insert(history)
        .values(
            **{
                target_key: request.local_id,
                "identity_namespace": identity.namespace,
                "external_id": identity.external_id,
                "verification_state": state,
                "evidence_kind": request.evidence.claim.evidence_kind,
                "event_key": payload.event_key,
                "request_fingerprint": payload.request_fingerprint,
                "request_json": payload.request_json,
            }
        )
        .returning(history.c.id)
    )
    assert event_id is not None
    return IdentityAttachmentReceipt(event_id, False)


async def _owner(
    session: AsyncSession, kind: MetadataEntityKind, local_id: int, namespace: IdentityNamespace
) -> RowMapping | None:
    active, _, key = _tables(kind)
    return (
        (
            await session.execute(
                select(active).where(
                    active.c[key] == local_id,
                    _scope(active, kind),
                    _namespace(active, kind) == namespace,
                )
            )
        )
        .mappings()
        .one_or_none()
    )


async def record_identity_observation(
    session: AsyncSession, request: IdentityEventRequest
) -> IdentityAttachmentReceipt:
    """Record adapter evidence without acquiring ownership or clearing decisions.

    Adapters establish provenance before calling. This is not an HTTP boundary;
    user confirmation and rejection are accepted only by the review service.
    """
    if request.actor is not IdentityEventActor.AUTOMATION or request.action not in {
        Action.OBSERVE,
        Action.REPORT_CONFLICT,
        Action.MARK_STALE,
    }:
        raise ValueError("Only automatic non-verifying observations are accepted")
    identity = request.evidence.claim.identity
    kind, namespace = identity.entity_kind, identity.namespace
    active, history, key = _tables(kind)
    await _begin_write(session)
    async with session.begin_nested():
        await _lock_parent_graph(session, [request])
        await _locked_targets(session, kind, [request.local_id])
        replay = await _replay(session, request)
        if replay:
            return replay
        owner = await _owner(session, kind, request.local_id, namespace)
        latest = await session.scalar(
            select(history.c.verification_state)
            .where(
                history.c[key] == request.local_id,
                history.c.identity_namespace == namespace,
                history.c.external_id == identity.external_id,
            )
            .order_by(history.c.id.desc())
            .limit(1)
        )
        same_owner = owner is not None and owner.external_id == identity.external_id
        previous = latest or (
            owner.verification_state if owner is not None and same_owner else State.OBSERVED
        )
        # Retained rejection always wins; an active conflict cannot be cleared by
        # observing an older, previously verified claim again.
        if previous is not State.REJECTED and same_owner and owner is not None:
            previous = owner.verification_state
        state = transition_identity_state(previous, request.action)
        if owner is not None and (
            same_owner or (request.action is Action.REPORT_CONFLICT and state is not State.REJECTED)
        ):
            values: dict[str, object] = {"revision": owner.revision + 1}
            if same_owner:
                values["verification_state"] = state
                if request.action is Action.OBSERVE:
                    values["last_seen_at"] = datetime.now(UTC)
            else:
                values["verification_state"] = State.CONFLICTED
            await session.execute(update(active).where(active.c.id == owner.id).values(**values))
        return await _append(session, request, state)


async def _candidate(
    session: AsyncSession, kind: MetadataEntityKind, local_id: int, event_id: int
) -> RowMapping:
    _, history, key = _tables(kind)
    row = (
        (
            await session.execute(
                select(history).where(
                    history.c[key] == local_id,
                    history.c.id == event_id,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise IdentityReviewNotFoundError("Saved identity evidence was not found for this record")
    return row


def _parent(row: RowMapping) -> ExternalIdentityRef | None:
    """Read only bounded, canonical parent proof from saved internal evidence."""
    try:
        if hashlib.sha256(row.request_json.encode()).hexdigest() != row.request_fingerprint:
            raise ValueError
        payload = json.loads(row.request_json)
        if not isinstance(payload, dict):
            raise ValueError
        parent = payload.get("parent_identity")
        if parent is None:
            return None
        if not isinstance(parent, dict):
            raise ValueError
        result = ExternalIdentityRef(
            IdentityNamespace(parent["namespace"]),
            MetadataEntityKind(parent["entity_kind"]),
            parent["external_id"],
        )
        if (
            result.entity_kind is not MetadataEntityKind.SERIES
            or result.namespace != row.identity_namespace
        ):
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityReviewRequiredError(
            "Saved parent evidence is invalid; recheck its source"
        ) from exc


def _request(
    kind: MetadataEntityKind,
    local_id: int,
    row: RowMapping,
    *,
    action: Action,
    fingerprint: str,
    review_revision: int,
    actor_user_id: int,
) -> IdentityEventRequest:
    from pullbox.core.metadata_identity_events import (
        IdentityEvidenceLocator,
        IdentityEvidenceRecordKind,
    )

    identity = ExternalIdentityRef(row.identity_namespace, kind, row.external_id)
    try:
        parent = _parent(row)
    except IdentityReviewRequiredError:
        if action is not Action.REJECT:
            raise
        # An invalid proof must remain rejectable without trusting its contents.
        parent = None
    return IdentityEventRequest(
        uuid5(
            _REVIEW_NAMESPACE, f"{kind}:{local_id}:{row.id}:{actor_user_id}:{action}:{fingerprint}"
        ),
        local_id,
        action,
        IdentityEventEvidence(
            ExactIdentityEvidence(identity, IdentityEvidenceKind.USER_SELECTION),
            fingerprint,
            locator=IdentityEvidenceLocator(
                IdentityEvidenceRecordKind(f"{kind.value}_identity_event"), row.id
            ),
            parent_identity=parent,
        ),
        actor=IdentityEventActor.USER,
        actor_user_id=actor_user_id,
        review_revision=review_revision,
    )


async def _preview(
    session: AsyncSession, kind: MetadataEntityKind, local_id: int, row: RowMapping
) -> dict[str, object]:
    active, history, key = _tables(kind)
    target = (await _locked_targets(session, kind, [local_id]))[local_id]
    owners = (
        (
            await session.execute(
                select(active)
                .where(
                    _scope(active, kind),
                    _namespace(active, kind) == row.identity_namespace,
                    or_(active.c[key] == local_id, active.c.external_id == row.external_id),
                )
                .order_by(active.c.id)
            )
        )
        .mappings()
        .all()
    )
    latest = await session.scalar(
        select(func.max(history.c.id)).where(
            history.c[key] == local_id,
            history.c.identity_namespace == row.identity_namespace,
        )
    )
    target_model = Base.metadata.tables[
        {
            MetadataEntityKind.SERIES: "series",
            MetadataEntityKind.ISSUE: "issues",
            MetadataEntityKind.STORY_ARC: "story_arcs",
        }[kind]
    ]
    legacy_owner = None
    if row.identity_namespace is IdentityNamespace.COMICVINE and int(row.external_id) < 2**63:
        legacy_owner = await session.scalar(
            select(target_model.c.id).where(target_model.c.comicvine_id == int(row.external_id))
        )
    proof: dict[str, object] = {
        "schema_version": 1,
        "candidate": dict(row),
        "target": dict(target),
        "owners": [dict(owner) for owner in owners],
        "latest_event": latest,
        "legacy_owner": legacy_owner,
    }
    if kind is MetadataEntityKind.ISSUE:
        series = (
            (
                await session.execute(
                    select(Series.id, Series.comicvine_id).where(Series.id == target.series_id)
                )
            )
            .mappings()
            .one()
        )
        parent_owner = await _owner(
            session, MetadataEntityKind.SERIES, target.series_id, row.identity_namespace
        )
        proof["parent"] = dict(series)
        proof["parent_owner"] = dict(parent_owner) if parent_owner else None
    fingerprint = hashlib.sha256(
        json.dumps(proof, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    current = next((item for item in owners if item[key] == local_id), None)
    external_owner = next(
        (item[key] for item in owners if item.external_id == row.external_id), None
    )
    state = await session.scalar(
        select(history.c.verification_state)
        .where(
            history.c[key] == local_id,
            history.c.identity_namespace == row.identity_namespace,
            history.c.external_id == row.external_id,
        )
        .order_by(history.c.id.desc())
        .limit(1)
    )
    if (
        state is not State.REJECTED
        and current is not None
        and current.external_id == row.external_id
    ):
        state = current.verification_state
    return {
        "event_id": row.id,
        "entity_kind": kind,
        "local_id": local_id,
        "identity_namespace": row.identity_namespace,
        "external_id": row.external_id,
        "verification_state": state,
        "current_external_id": current.external_id if current else None,
        "owner_local_id": external_owner or legacy_owner,
        "review_revision": (latest or 0) + 1,
        "fingerprint": fingerprint,
    }


async def preview_identity_review(
    session: AsyncSession, kind: MetadataEntityKind, local_id: int, event_id: int
) -> dict[str, object]:
    """Return a sanitized optimistic snapshot; it is not an authorization token."""
    row = await _candidate(session, kind, local_id, event_id)
    return await _preview(session, kind, local_id, row)


async def list_identity_claims(
    session: AsyncSession, kind: MetadataEntityKind, local_id: int, *, limit: int, offset: int
) -> tuple[list[dict[str, object]], int]:
    """Page the latest state per claim, without returning raw evidence or paths."""
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("Invalid identity claim page")
    await _locked_targets(session, kind, [local_id])
    active, history, key = _tables(kind)
    latest = (
        select(func.max(history.c.id).label("id"))
        .where(history.c[key] == local_id)
        .group_by(history.c.identity_namespace, history.c.external_id)
        .subquery()
    )
    total = await session.scalar(select(func.count()).select_from(latest))
    result = await session.execute(
        select(
            history.c.id.label("event_id"),
            history.c.identity_namespace,
            history.c.external_id,
            case(
                (history.c.verification_state == State.REJECTED, history.c.verification_state),
                else_=func.coalesce(active.c.verification_state, history.c.verification_state),
            ).label("verification_state"),
        )
        .outerjoin(
            active,
            (active.c[key] == history.c[key])
            & _scope(active, kind)
            & (_namespace(active, kind) == history.c.identity_namespace)
            & (active.c.external_id == history.c.external_id),
        )
        .where(history.c.id.in_(select(latest.c.id)))
        .order_by(history.c.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [dict(row) for row in result.mappings()], total or 0


async def apply_identity_review(
    session: AsyncSession,
    kind: MetadataEntityKind,
    local_id: int,
    event_id: int,
    *,
    action: Action,
    fingerprint: str,
    review_revision: int,
    actor_user_id: int,
) -> IdentityAttachmentReceipt:
    """Apply an authenticated decision in the caller's transaction, without I/O.

    The transport must require interactive operator auth and CSRF. Actor identity
    comes from that dependency, never the request body. Replays return history,
    not a claim that the old ownership is still current.
    """
    if action not in {Action.CONFIRM, Action.REJECT}:
        raise ValueError("Review requires an explicit confirm or reject decision")
    await _begin_write(session)
    try:
        async with session.begin_nested():
            if not await session.scalar(
                select(User.id).where(User.id == actor_user_id, User.is_active.is_(True))
            ):
                raise IdentityReviewRequiredError("An active operator is required")
            row = await _candidate(session, kind, local_id, event_id)
            request = _request(
                kind,
                local_id,
                row,
                action=action,
                fingerprint=fingerprint,
                review_revision=review_revision,
                actor_user_id=actor_user_id,
            )
            await _lock_parent_graph(session, [request])
            replay = await _replay(session, request)
            if replay:
                return replay
            current = await _preview(session, kind, local_id, row)
            if (
                current["fingerprint"] != fingerprint
                or current["review_revision"] != review_revision
            ):
                raise IdentityReviewRequiredError(
                    "Identity evidence changed; reload the review before deciding"
                )
            if action is Action.CONFIRM:
                await _confirm(session, request)
                state = State.VERIFIED
            else:
                await _reject(session, request)
                state = State.REJECTED
            return await _append(session, request, state)
    except IntegrityError as exc:
        raise IdentityAttachmentConflictError(
            "Identity ownership changed; reload before retrying"
        ) from exc


async def confirm_locg_series_selection(
    session: AsyncSession, request: IdentityEventRequest
) -> IdentityAttachmentReceipt:
    """Record a cache-revalidated interactive discovery choice in its Add transaction.

    The caller owns interactive authentication, CSRF and release freshness. LOCG
    is passive discovery evidence here, not an automatic metadata provider.
    """
    identity = request.evidence.claim.identity
    if (
        identity.namespace is not IdentityNamespace.LOCG
        or identity.entity_kind is not MetadataEntityKind.SERIES
        or request.action is not Action.CONFIRM
        or request.actor is not IdentityEventActor.USER
        or request.evidence.claim.evidence_kind is not IdentityEvidenceKind.USER_SELECTION
    ):
        raise ValueError("Discovery links require an explicit LOCG series selection")
    await _begin_write(session)
    try:
        async with session.begin_nested():
            if not await session.scalar(
                select(User.id).where(User.id == request.actor_user_id, User.is_active.is_(True))
            ):
                raise IdentityReviewRequiredError("An active operator is required")
            await _lock_parent_graph(session, [request])
            replay = await _replay(session, request)
            if replay:
                return replay
            await _confirm(session, request)
            return await _append(session, request, State.VERIFIED)
    except IntegrityError as exc:
        raise IdentityAttachmentConflictError(
            "Identity ownership changed; reload before retrying"
        ) from exc


async def _confirm(session: AsyncSession, request: IdentityEventRequest) -> None:
    identity = request.evidence.claim.identity
    kind, namespace = identity.entity_kind, identity.namespace
    if namespace is IdentityNamespace.COMICVINE and int(identity.external_id) >= 2**63:
        raise IdentityAttachmentConflictError(
            "ComicVine identity exceeds the compatibility ID range"
        )
    targets = await _locked_targets(session, kind, [request.local_id])
    await _validate_legacy_owners(session, kind, [request], targets)
    if kind is MetadataEntityKind.ISSUE:
        if request.evidence.parent_identity is None:
            raise IdentityAttachmentConflictError(
                "Issue confirmation requires exact series evidence; recheck its source"
            )
        await _validate_issue_parents(session, [request], targets)
    active, _, key = _tables(kind)
    owner = await _owner(session, kind, request.local_id, namespace)
    other = await session.scalar(
        select(active.c[key]).where(
            _scope(active, kind),
            _namespace(active, kind) == namespace,
            active.c.external_id == identity.external_id,
            active.c[key] != request.local_id,
        )
    )
    if other is not None or (owner is not None and owner.external_id != identity.external_id):
        raise IdentityAttachmentConflictError(
            "Identity already has an owner; review that assignment before replacing it"
        )
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "verification_state": State.VERIFIED,
        "evidence_kind": IdentityEvidenceKind.USER_SELECTION,
        "evidence_locator": prepare_identity_event(request).request_json,
        "verified_at": now,
        "last_seen_at": now,
        "revision": owner.revision + 1 if owner else 1,
    }
    if owner:
        await session.execute(update(active).where(active.c.id == owner.id).values(**values))
    else:
        values.update({key: request.local_id, "external_id": identity.external_id})
        values.update(
            {"source": namespace.value, "namespace": "story_arc"}
            if kind is MetadataEntityKind.STORY_ARC
            else {"identity_namespace": namespace}
        )
        await session.execute(insert(active).values(**values))
    if namespace is IdentityNamespace.COMICVINE:
        target = Base.metadata.tables[
            {
                MetadataEntityKind.SERIES: "series",
                MetadataEntityKind.ISSUE: "issues",
                MetadataEntityKind.STORY_ARC: "story_arcs",
            }[kind]
        ]
        await session.execute(
            update(target)
            .where(target.c.id == request.local_id)
            .values(comicvine_id=int(identity.external_id))
        )


async def _reject(session: AsyncSession, request: IdentityEventRequest) -> None:
    identity = request.evidence.claim.identity
    kind, namespace = identity.entity_kind, identity.namespace
    active, _, _ = _tables(kind)
    owner = await _owner(session, kind, request.local_id, namespace)
    target = Base.metadata.tables[
        {
            MetadataEntityKind.SERIES: "series",
            MetadataEntityKind.ISSUE: "issues",
            MetadataEntityKind.STORY_ARC: "story_arcs",
        }[kind]
    ]
    legacy = await session.scalar(
        select(target.c.comicvine_id).where(target.c.id == request.local_id)
    )
    same_owner = owner is not None and owner.external_id == identity.external_id
    same_legacy = namespace is IdentityNamespace.COMICVINE and str(legacy) == identity.external_id
    if kind is MetadataEntityKind.SERIES and (same_owner or same_legacy):
        child = Base.metadata.tables["issue_external_identities"]
        dependent = await session.scalar(
            select(child.c.id)
            .join(Issue, Issue.id == child.c.issue_id)
            .where(
                Issue.series_id == request.local_id,
                child.c.identity_namespace == namespace,
            )
            .limit(1)
        )
        legacy_child = namespace is IdentityNamespace.COMICVINE and await session.scalar(
            select(Issue.id)
            .where(
                Issue.series_id == request.local_id,
                Issue.comicvine_id.is_not(None),
            )
            .limit(1)
        )
        if dependent or legacy_child:
            raise IdentityAttachmentConflictError(
                "Review dependent issue identities before detaching their series"
            )
    if same_owner and owner is not None:
        await session.execute(delete(active).where(active.c.id == owner.id))
    if same_legacy:
        await session.execute(
            update(target).where(target.c.id == request.local_id).values(comicvine_id=None)
        )
