"""Identity retries retain provenance and cannot rewrite a previous decision."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest

from pullbox.core.metadata_identity import (
    ExactIdentityEvidence,
    ExternalIdentityRef,
)
from pullbox.core.metadata_identity import (
    IdentityEvidenceKind as Evidence,
)
from pullbox.core.metadata_identity import (
    IdentityNamespace as Namespace,
)
from pullbox.core.metadata_identity import (
    MetadataEntityKind as Kind,
)
from pullbox.core.metadata_identity import (
    MetadataSource as Source,
)
from pullbox.core.metadata_identity_events import (
    IdentityEventActor as Actor,
)
from pullbox.core.metadata_identity_events import (
    IdentityEventEvidence,
    IdentityEventReplayConflictError,
    IdentityEventRequest,
    IdentityEvidenceLocator,
    prepare_identity_event,
    validate_identity_event_replay,
)
from pullbox.core.metadata_identity_events import (
    IdentityEvidenceRecordKind as Record,
)
from pullbox.core.metadata_identity_state import IdentityVerificationAction as Action


def _identity(value: str = "42", namespace: Namespace = Namespace.COMICVINE) -> ExternalIdentityRef:
    return ExternalIdentityRef(namespace, Kind.ISSUE, value)


def _request() -> IdentityEventRequest:
    return IdentityEventRequest(
        UUID("e614d9ad-354b-4dbc-b7dd-3f07ca140c56"),
        7,
        Action.OBSERVE,
        IdentityEventEvidence(
            ExactIdentityEvidence(_identity(), Evidence.COMICINFO_XML),
            "a" * 64,
            IdentityEvidenceLocator(Record.IMPORTED_FILE, 123),
        ),
    )


@pytest.mark.parametrize(
    ("kind", "namespace", "evidence", "source_kind", "source_namespace"),
    [
        (Kind.ISSUE, Namespace.LOCG, Evidence.PROVIDER_CROSSWALK, Kind.SERIES, Namespace.METRON),
        (Kind.SERIES, Namespace.GCD, Evidence.PROVIDER_CROSSWALK, Kind.SERIES, Namespace.METRON),
        (Kind.SERIES, Namespace.LOCG, Evidence.USER_SELECTION, Kind.SERIES, Namespace.METRON),
        (Kind.SERIES, Namespace.LOCG, Evidence.PROVIDER_CROSSWALK, Kind.ISSUE, Namespace.METRON),
        (Kind.SERIES, Namespace.LOCG, Evidence.PROVIDER_CROSSWALK, Kind.SERIES, Namespace.LOCG),
    ],
)
def test_watch_provenance_only_records_native_series_to_passive_locg(
    kind, namespace, evidence, source_kind, source_namespace
):
    with pytest.raises(ValueError, match="Watch crosswalk"):
        IdentityEventEvidence(
            ExactIdentityEvidence(ExternalIdentityRef(namespace, kind, "19"), evidence),
            "b" * 64,
            locator=IdentityEvidenceLocator(Record.RELEASE_WATCH, 12),
            source_identity=ExternalIdentityRef(source_namespace, source_kind, "87"),
        )


def test_retry_payload_has_explicit_version_and_only_safe_provenance_fields() -> None:
    prepared = prepare_identity_event(_request())
    payload = json.loads(prepared.request_json)
    assert payload == {
        "version": 1,
        "operation_id": "e614d9ad-354b-4dbc-b7dd-3f07ca140c56",
        "local_id": 7,
        "identity": {"namespace": "comicvine", "entity_kind": "issue", "external_id": "42"},
        "action": "observe",
        "evidence_kind": "comicinfo_xml",
        "evidence_revision": "a" * 64,
        "origin": {"record_kind": "imported_file", "record_id": 123},
        "actor": "automation",
        "actor_user_id": None,
        "review_revision": None,
    }
    assert len(prepared.event_key) == len(prepared.request_fingerprint) == 64
    assert prepared.event_key != prepared.request_fingerprint


def test_normalized_id_and_rebuilt_request_have_identical_retry_material() -> None:
    request = _request()
    normalized = replace(
        request,
        evidence=replace(
            request.evidence, claim=replace(request.evidence.claim, identity=_identity(" 00042 "))
        ),
    )
    first = prepare_identity_event(request)
    assert first == prepare_identity_event(normalized) == prepare_identity_event(_request())
    assert first.event_key
    validate_identity_event_replay(
        first, event_key=first.event_key, request_fingerprint=first.request_fingerprint
    )


@pytest.mark.parametrize("change", ["identity", "evidence_kind", "actor", "review_revision"])
def test_reused_retry_slot_with_different_semantics_fails_without_echoing_payload(
    change: str,
) -> None:
    request = replace(_request(), actor=Actor.USER, actor_user_id=1, review_revision=2)
    changed = {
        "identity": replace(
            request,
            evidence=replace(
                request.evidence, claim=replace(request.evidence.claim, identity=_identity("43"))
            ),
        ),
        "evidence_kind": replace(
            request,
            evidence=replace(
                request.evidence,
                claim=replace(request.evidence.claim, evidence_kind=Evidence.METRONINFO_XML),
            ),
        ),
        "actor": replace(request, actor_user_id=2),
        "review_revision": replace(request, review_revision=3),
    }[change]
    stored, replay = prepare_identity_event(request), prepare_identity_event(changed)
    assert stored.event_key == replay.event_key
    assert stored.request_fingerprint != replay.request_fingerprint
    with pytest.raises(IdentityEventReplayConflictError, match="different request") as error:
        validate_identity_event_replay(
            replay, event_key=stored.event_key, request_fingerprint=stored.request_fingerprint
        )
    assert replay.request_json not in str(error.value)


@pytest.mark.parametrize(
    "change", ["operation", "target", "action", "revision", "locator", "namespace"]
)
def test_new_logical_evidence_or_decision_gets_a_new_retry_slot(change: str) -> None:
    request = _request()
    changed = {
        "operation": replace(request, operation_id=UUID(int=2)),
        "target": replace(request, local_id=8),
        "action": replace(request, action=Action.REPORT_CONFLICT),
        "revision": replace(request, evidence=replace(request.evidence, revision="b" * 64)),
        "locator": replace(
            request,
            evidence=replace(
                request.evidence, locator=IdentityEvidenceLocator(Record.IMPORTED_FILE, 124)
            ),
        ),
        "namespace": replace(
            request,
            evidence=replace(
                request.evidence,
                claim=replace(
                    request.evidence.claim, identity=_identity(namespace=Namespace.METRON)
                ),
            ),
        ),
    }[change]
    assert prepare_identity_event(request).event_key != prepare_identity_event(changed).event_key


def test_provider_transport_and_crosswalk_asserting_identity_are_retained() -> None:
    claim = ExactIdentityEvidence(_identity(), Evidence.PROVIDER_CROSSWALK, Source.METRON_API)
    source_identity = _identity("75", Namespace.METRON)
    request = replace(
        _request(), evidence=IdentityEventEvidence(claim, "a" * 64, source_identity=source_identity)
    )
    origin = json.loads(prepare_identity_event(request).request_json)["origin"]
    assert origin == {
        "source_instance": "metron_api",
        "source_identity": {"namespace": "metron", "entity_kind": "issue", "external_id": "75"},
    }


def test_local_and_api_sources_share_identity_but_not_observation_provenance() -> None:
    request = _request()
    claims = [
        ExactIdentityEvidence(_identity(), Evidence.PROVIDER_RESULT, source)
        for source in (Source.COMICVINE_LOCAL, Source.COMICVINE_API)
    ]
    prepared = [
        prepare_identity_event(
            replace(
                request,
                evidence=IdentityEventEvidence(claim, "a" * 64, source_identity=_identity()),
            )
        )
        for claim in claims
    ]
    assert prepared[0].event_key != prepared[1].event_key


@pytest.mark.parametrize("action", [Action.CONFIRM, Action.REJECT])
def test_explicit_decisions_require_a_user_and_review_revision(action: Action) -> None:
    with pytest.raises(ValueError, match="review"):
        replace(_request(), action=action)
    with pytest.raises(ValueError, match="review"):
        replace(_request(), action=action, actor=Actor.USER, actor_user_id=1)
    request = replace(
        _request(), action=action, actor=Actor.USER, actor_user_id=1, review_revision=2
    )
    assert json.loads(prepare_identity_event(request).request_json)["review_revision"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "not-a-uuid"),
        ("operation_id", UUID(int=0)),
        ("local_id", True),
        ("local_id", 0),
        ("local_id", -1),
        ("local_id", 2**63),
        ("action", "confirm"),
        ("actor", "user"),
        ("actor_user_id", 1),
        ("review_revision", True),
        ("review_revision", 0),
        ("evidence", {}),
    ],
)
def test_invalid_request_fields_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(_request(), **{field: value})


@pytest.mark.parametrize(
    "record_kind,record_id",
    [
        ("issue", 1),
        (Record.IMPORTED_FILE, True),
        (Record.IMPORTED_FILE, 0),
        (Record.IMPORTED_FILE, "path"),
        (Record.IMPORTED_FILE, 2**63),
    ],
)
def test_locators_accept_only_typed_record_ids_not_paths(
    record_kind: object, record_id: object
) -> None:
    with pytest.raises(ValueError):
        IdentityEvidenceLocator(record_kind, record_id)


@pytest.mark.parametrize(
    "revision",
    [
        "",
        "a" * 63,
        "a" * 65,
        "G" * 64,
        "A" * 64,
        "https://example.invalid/?token=secret",
        "private\npath",
    ],
)
def test_evidence_revision_is_a_bounded_digest_not_raw_content(revision: str) -> None:
    with pytest.raises(ValueError) as error:
        replace(_request().evidence, revision=revision)
    assert revision not in str(error.value) if revision else True


@pytest.mark.parametrize(
    "scenario",
    [
        "no_origin",
        "both_origins",
        "untyped_locator",
        "provider_without_source",
        "provider_wrong_namespace",
        "provider_wrong_kind",
        "result_disagrees",
        "local_with_provider",
        "crosswalk_to_same_namespace",
        "bad_claim",
    ],
)
def test_inconsistent_provenance_fails_before_serialization(scenario: str) -> None:
    local = _request().evidence
    provider = ExactIdentityEvidence(_identity(), Evidence.PROVIDER_RESULT, Source.COMICVINE_API)
    with pytest.raises(ValueError):
        if scenario == "no_origin":
            replace(local, locator=None)
        elif scenario == "both_origins":
            replace(local, claim=provider, source_identity=_identity())
        elif scenario == "untyped_locator":
            replace(local, locator={"path": "/private/source"})
        elif scenario == "provider_without_source":
            replace(local, claim=ExactIdentityEvidence(_identity(), Evidence.PROVIDER_RESULT))
        elif scenario == "provider_wrong_namespace":
            IdentityEventEvidence(
                provider, "a" * 64, source_identity=_identity(namespace=Namespace.METRON)
            )
        elif scenario == "provider_wrong_kind":
            IdentityEventEvidence(
                provider,
                "a" * 64,
                source_identity=ExternalIdentityRef(Namespace.COMICVINE, Kind.SERIES, "42"),
            )
        elif scenario == "result_disagrees":
            IdentityEventEvidence(provider, "a" * 64, source_identity=_identity("43"))
        elif scenario == "local_with_provider":
            IdentityEventEvidence(
                replace(provider, evidence_kind=Evidence.COMICINFO_XML),
                "a" * 64,
                source_identity=_identity(),
            )
        elif scenario == "crosswalk_to_same_namespace":
            IdentityEventEvidence(
                replace(provider, evidence_kind=Evidence.PROVIDER_CROSSWALK),
                "a" * 64,
                source_identity=_identity(),
            )
        else:
            replace(local, claim={})


def test_user_actor_requires_user_id_and_user_selection_requires_review_context() -> None:
    with pytest.raises(ValueError, match="actor"):
        replace(_request(), actor=Actor.USER)
    with pytest.raises(ValueError, match="review"):
        replace(
            _request(),
            evidence=replace(
                _request().evidence,
                claim=replace(_request().evidence.claim, evidence_kind=Evidence.USER_SELECTION),
            ),
        )


def test_request_and_prepared_material_are_immutable() -> None:
    request = _request()
    with pytest.raises(FrozenInstanceError):
        request.local_id = 8
    prepared = prepare_identity_event(request)
    with pytest.raises(FrozenInstanceError):
        prepared.request_fingerprint = "b" * 64


@pytest.mark.parametrize("key,digest", [("bad", "a" * 64), ("a" * 64, "bad"), ("a" * 64, "b" * 64)])
def test_unknown_or_different_persisted_retry_material_fails_closed(key: str, digest: str) -> None:
    with pytest.raises(IdentityEventReplayConflictError):
        validate_identity_event_replay(
            prepare_identity_event(_request()), event_key=key, request_fingerprint=digest
        )
