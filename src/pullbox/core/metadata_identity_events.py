"""Validated identity evidence and semantic retry contracts, not authorization."""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from pullbox.core.metadata_identity import (
    ExactIdentityEvidence,
    ExternalIdentityRef,
    IdentityEvidenceKind,
    IdentityNamespace,
    MetadataEntityKind,
)
from pullbox.core.metadata_identity_state import IdentityVerificationAction


class IdentityEvidenceRecordKind(enum.StrEnum):
    SERIES = "series"
    ISSUE = "issue"
    STORY_ARC = "story_arc"
    STORY_ARC_IDENTITY = "story_arc_identity"
    SERIES_IDENTITY_EVENT = "series_identity_event"
    ISSUE_IDENTITY_EVENT = "issue_identity_event"
    STORY_ARC_IDENTITY_EVENT = "story_arc_identity_event"
    IMPORTED_SERIES = "imported_series"
    IMPORTED_FILE = "imported_file"
    IMPORTED_STORY_ARC = "imported_story_arc"
    IMPORT_JOB_ACTION = "import_job_action"
    LIBRARY_FILE = "library_file"
    RELEASE_WATCH = "release_watch"


class IdentityEventActor(enum.StrEnum):
    AUTOMATION = "automation"
    USER = "user"
    MIGRATION = "migration"


@dataclass(frozen=True)
class IdentityEvidenceLocator:
    """Safe local record locator, not a path or evidence payload."""

    record_kind: IdentityEvidenceRecordKind
    record_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.record_kind, IdentityEvidenceRecordKind) or not _positive_id(
            self.record_id
        ):
            raise ValueError("Invalid identity evidence record locator")


@dataclass(frozen=True)
class IdentityEventEvidence:
    """One revision of local or provider evidence; it does not establish trust."""

    claim: ExactIdentityEvidence
    revision: str
    locator: IdentityEvidenceLocator | None = None
    source_identity: ExternalIdentityRef | None = None
    parent_identity: ExternalIdentityRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.claim, ExactIdentityEvidence) or not isinstance(
            self.claim.identity, ExternalIdentityRef
        ):
            raise ValueError("Invalid identity evidence claim")
        if not _is_digest(self.revision):
            raise ValueError("Invalid identity evidence revision digest")
        if self.parent_identity is not None and (
            not isinstance(self.parent_identity, ExternalIdentityRef)
            or self.claim.identity.entity_kind is not MetadataEntityKind.ISSUE
            or self.parent_identity.entity_kind is not MetadataEntityKind.SERIES
            or self.parent_identity.namespace != self.claim.identity.namespace
        ):
            raise ValueError("Invalid issue parent identity evidence")
        provider_kinds = {
            IdentityEvidenceKind.PROVIDER_RESULT,
            IdentityEvidenceKind.PROVIDER_CROSSWALK,
        }
        source = self.claim.source_instance
        if source is None:
            if (
                isinstance(self.locator, IdentityEvidenceLocator)
                and self.locator.record_kind is IdentityEvidenceRecordKind.RELEASE_WATCH
            ):
                if (
                    self.claim.evidence_kind is IdentityEvidenceKind.PROVIDER_CROSSWALK
                    and self.claim.identity.namespace is IdentityNamespace.LOCG
                    and self.claim.identity.entity_kind is MetadataEntityKind.SERIES
                    and isinstance(self.source_identity, ExternalIdentityRef)
                    and self.source_identity.entity_kind is MetadataEntityKind.SERIES
                    and self.source_identity.namespace is not IdentityNamespace.LOCG
                ):
                    return
                raise ValueError("Invalid published Watch crosswalk")
            if (
                not isinstance(self.locator, IdentityEvidenceLocator)
                or self.source_identity is not None
                or self.claim.evidence_kind in provider_kinds
            ):
                raise ValueError("Invalid local identity evidence provenance")
            return
        if (
            self.locator is not None
            or not isinstance(self.source_identity, ExternalIdentityRef)
            or self.source_identity.namespace != source.identity_namespace
            or self.source_identity.entity_kind != self.claim.identity.entity_kind
            or self.claim.evidence_kind
            not in provider_kinds | {IdentityEvidenceKind.USER_SELECTION}
        ):
            raise ValueError("Invalid provider identity evidence provenance")
        if self.claim.evidence_kind is IdentityEvidenceKind.PROVIDER_CROSSWALK:
            if self.claim.identity.namespace == self.source_identity.namespace:
                raise ValueError("A crosswalk must identify another provider namespace")
        elif self.source_identity != self.claim.identity:
            raise ValueError("Provider result identity disagrees with its source record")


@dataclass(frozen=True)
class IdentityEventRequest:
    """Server-assembled logical action; typed actors/UUIDs do not authorize it.

    Keep operation_id durable across retries. A new observation run or explicit
    decision receives a new server-generated operation ID. Source adapters bind
    revision to the evidence snapshot. Auth, review freshness, exact agreement,
    and ownership checks still belong to the application transaction.
    """

    operation_id: UUID
    local_id: int
    action: IdentityVerificationAction
    evidence: IdentityEventEvidence
    actor: IdentityEventActor = IdentityEventActor.AUTOMATION
    actor_user_id: int | None = None
    review_revision: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, UUID) or not self.operation_id.int:
            raise ValueError("Invalid identity operation ID")
        if not _positive_id(self.local_id) or not isinstance(
            self.action, IdentityVerificationAction
        ):
            raise ValueError("Invalid identity event target or action")
        if not isinstance(self.evidence, IdentityEventEvidence):
            raise ValueError("Invalid identity event evidence")
        if not isinstance(self.actor, IdentityEventActor):
            raise ValueError("Invalid identity event actor")
        if self.actor is IdentityEventActor.USER:
            if not _positive_id(self.actor_user_id):
                raise ValueError("User identity event actor requires a user ID")
        elif self.actor_user_id is not None:
            raise ValueError("Non-user identity event actor cannot supply a user ID")
        if self.review_revision is not None and not _positive_id(self.review_revision):
            raise ValueError("Invalid identity review revision")
        if (
            self.action in {IdentityVerificationAction.CONFIRM, IdentityVerificationAction.REJECT}
            or self.evidence.claim.evidence_kind is IdentityEvidenceKind.USER_SELECTION
        ) and (self.actor is not IdentityEventActor.USER or self.review_revision is None):
            raise ValueError("Explicit identity review requires a user and review revision")


@dataclass(frozen=True)
class PreparedIdentityEvent:
    event_key: str
    request_fingerprint: str
    request_json: str


class IdentityEventReplayConflictError(ValueError):
    """A stored operation cannot be reinterpreted as a different request."""


def prepare_identity_event(request: IdentityEventRequest) -> PreparedIdentityEvent:
    """Build a stable retry slot and separate semantic request fingerprint."""
    if not isinstance(request, IdentityEventRequest):
        raise ValueError("Invalid identity event request")
    evidence = request.evidence
    identity = evidence.claim.identity
    origin: dict[str, object]
    if evidence.locator is not None:
        origin = {
            "record_kind": evidence.locator.record_kind.value,
            "record_id": evidence.locator.record_id,
        }
        if evidence.source_identity is not None:
            origin["source_identity"] = _identity_payload(evidence.source_identity)
    else:
        assert evidence.claim.source_instance is not None and evidence.source_identity is not None
        origin = {
            "source_instance": evidence.claim.source_instance.value,
            "source_identity": _identity_payload(evidence.source_identity),
        }
    slot = {
        "version": 1,
        "operation_id": str(request.operation_id),
        "entity_kind": identity.entity_kind.value,
        "local_id": request.local_id,
        "identity_namespace": identity.namespace.value,
        "action": request.action.value,
        "origin": origin,
        "evidence_revision": evidence.revision,
    }
    payload = {
        "version": 1,
        "operation_id": str(request.operation_id),
        "local_id": request.local_id,
        "identity": _identity_payload(identity),
        "action": request.action.value,
        "evidence_kind": evidence.claim.evidence_kind.value,
        "evidence_revision": evidence.revision,
        "origin": origin,
        "actor": request.actor.value,
        "actor_user_id": request.actor_user_id,
        "review_revision": request.review_revision,
    }
    # Keep existing v1 requests byte-identical when no parent was recorded.
    if evidence.parent_identity is not None:
        payload["parent_identity"] = _identity_payload(evidence.parent_identity)
    serialized = _canonical_json(payload)
    return PreparedIdentityEvent(_digest(_canonical_json(slot)), _digest(serialized), serialized)


def validate_identity_event_replay(
    request: PreparedIdentityEvent, *, event_key: str, request_fingerprint: str
) -> None:
    """Reject a reused retry slot with changed semantics; never update history."""
    if (
        not _is_digest(event_key)
        or not _is_digest(request_fingerprint)
        or not _is_digest(request.event_key)
        or not _is_digest(request.request_fingerprint)
        or request.event_key != event_key
        or request.request_fingerprint != request_fingerprint
    ):
        raise IdentityEventReplayConflictError(
            "Identity operation was already recorded with a different request"
        )


def _positive_id(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _identity_payload(identity: ExternalIdentityRef) -> dict[str, str]:
    return {
        "namespace": identity.namespace.value,
        "entity_kind": identity.entity_kind.value,
        "external_id": identity.external_id,
    }


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: str) -> str:
    # Content/retry identifiers only; not credentials or authorization tokens.
    return hashlib.sha256(value.encode("ascii")).hexdigest()
