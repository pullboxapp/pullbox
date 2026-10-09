"""Read-only issue state for releases under an explicitly confirmed series."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import and_, case, desc, literal, or_, select, tuple_, union_all

from pullbox.core.issue_numbers import normalize_issue_number_text, parse_issue_number_text
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectAcquisitionState
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.services.whats_new_actions import (
    WhatsNewSelectionError,
    positive_id,
    release_series_id,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.series import Series


class ReleaseIssueStatus(StrEnum):
    OWNED = "owned"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    PAUSED = "paused"
    MISSING = "missing"
    SKIPPED = "skipped"
    NEEDS_REVIEW = "needs_review"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ReleaseIssueState:
    state: ReleaseIssueStatus
    issue_id: int | None = None

    @property
    def label(self) -> str:
        return {
            ReleaseIssueStatus.NEEDS_REVIEW: "Needs review",
            ReleaseIssueStatus.UNRESOLVED: "Issue not linked",
        }.get(self.state, self.state.value.capitalize())


_PROVIDERS = {
    "comicvine_issue_id": IdentityNamespace.COMICVINE,
    "metron_issue_id": IdentityNamespace.METRON,
    "gcd_issue_id": IdentityNamespace.GCD,
}
_DOWNLOAD_STATES = {
    DownloadState.QUEUED: ReleaseIssueStatus.QUEUED,
    DownloadState.SENT: ReleaseIssueStatus.QUEUED,
    DownloadState.RETRY_PENDING: ReleaseIssueStatus.QUEUED,
    DownloadState.DOWNLOADING: ReleaseIssueStatus.DOWNLOADING,
    DownloadState.PAUSED: ReleaseIssueStatus.PAUSED,
    DownloadState.FINALIZING: ReleaseIssueStatus.PROCESSING,
    DownloadState.POST_PROCESSING: ReleaseIssueStatus.PROCESSING,
    DownloadState.COMPLETED: ReleaseIssueStatus.PROCESSING,
}
_DIRECT_STATES = {
    DirectAcquisitionState.RESOLVING: ReleaseIssueStatus.QUEUED,
    DirectAcquisitionState.PLANNED: ReleaseIssueStatus.QUEUED,
    DirectAcquisitionState.QUEUED: ReleaseIssueStatus.QUEUED,
    DirectAcquisitionState.DOWNLOADING: ReleaseIssueStatus.DOWNLOADING,
    DirectAcquisitionState.VALIDATING: ReleaseIssueStatus.PROCESSING,
    DirectAcquisitionState.POST_PROCESSING: ReleaseIssueStatus.PROCESSING,
    DirectAcquisitionState.RETRY_PENDING: ReleaseIssueStatus.QUEUED,
    DirectAcquisitionState.PAUSED: ReleaseIssueStatus.PAUSED,
    DirectAcquisitionState.INTERVENTION: ReleaseIssueStatus.NEEDS_REVIEW,
}


def _sources(release: Mapping[str, object]) -> list[Mapping[str, object]]:
    group = release.get("_discovery_releases")
    if isinstance(group, list):
        return list(group) if group and all(isinstance(source, dict) for source in group) else []
    return [release]


def _number(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return normalize_issue_number_text(value)
    except ValueError:
        return None


def _date(value: object) -> date | None:
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


def _refs(release: Mapping[str, object]) -> dict[IdentityNamespace, str | None]:
    return {
        namespace: _provider_id(release[field])
        for field, namespace in _PROVIDERS.items()
        if release.get(field) is not None
    }


def _provider_id(value: object) -> str | None:
    return str(value) if type(value) is int and 0 < value < 2**63 else None


async def local_release_issues(
    session: AsyncSession,
    releases: Sequence[Mapping[str, object]],
    owners: Mapping[str, Series],
) -> list[ReleaseIssueState | None]:
    """Resolve visible rows in three SELECTs, without provider/file I/O or persistence.

    Number/date is discovery evidence only under a confirmed LOCG series. An
    explicit unknown, disputed or contradictory cross-ID never falls back to it.
    """
    if not releases or not owners:
        return [None for _ in releases]
    sources = [source for release in releases for source in _sources(release)]
    pairs: set[tuple[int, str]] = set()
    for source in sources:
        try:
            identity = release_series_id(dict(source))
        except WhatsNewSelectionError:
            continue
        owner = owners.get(identity or "")
        number = _number(source.get("issue_number"))
        if owner and number:
            pairs.add((owner.id, number))
    legacy_pairs = {
        (series_id, parse_issue_number_text(number)[0])
        for series_id, number in pairs
        if number == normalize_issue_number_text(parse_issue_number_text(number)[0])
    }
    requested = {
        (namespace, external_id)
        for source in sources
        for namespace, external_id in _refs(source).items()
        if external_id
    }
    legacy_cv_ids = {
        int(value) for namespace, value in requested if namespace is IdentityNamespace.COMICVINE
    }
    candidate = or_(
        tuple_(Issue.series_id, Issue.issue_number_text).in_(pairs),
        and_(
            Issue.issue_number_text.is_(None),
            tuple_(Issue.series_id, Issue.issue_number).in_(legacy_pairs),
        ),
        Issue.comicvine_id.in_(legacy_cv_ids),
    )
    claims = list(
        (
            await session.scalars(
                select(IssueExternalIdentity).where(
                    or_(
                        IssueExternalIdentity.issue_id.in_(select(Issue.id).where(candidate)),
                        tuple_(
                            IssueExternalIdentity.identity_namespace,
                            IssueExternalIdentity.external_id,
                        ).in_(requested),
                    )
                )
            )
        ).all()
    )
    claimed = {(claim.identity_namespace, claim.external_id): claim for claim in claims}
    disputed = {
        claim.issue_id
        for claim in claims
        if claim.verification_state is not IdentityVerificationState.VERIFIED
    }
    has_file = select(LibraryFile.id).where(LibraryFile.issue_id == Issue.id).exists()
    pending = (
        select(PendingMatch.id)
        .where(PendingMatch.issue_id == Issue.id, PendingMatch.status == PendingMatchStatus.PENDING)
        .exists()
    )
    rows = (
        await session.execute(
            select(Issue, has_file, pending)
            .execution_options(populate_existing=True)
            .where(or_(candidate, Issue.id.in_({claim.issue_id for claim in claims})))
        )
    ).all()
    issues = {issue.id: issue for issue, _, _ in rows}
    files = {issue.id for issue, registered, _ in rows if registered}
    pending_issues = {issue.id for issue, _, held in rows if held}
    by_number: dict[tuple[int, str], list[Issue]] = defaultdict(list)
    by_cv_id = {issue.comicvine_id: issue for issue in issues.values() if issue.comicvine_id}
    for issue in issues.values():
        by_number[(issue.series_id, issue.effective_issue_number_text)].append(issue)
    downloads = (
        await session.execute(
            union_all(
                select(
                    DownloadHistory.issue_id,
                    case(
                        *[
                            (DownloadHistory.state == key, value.value)
                            for key, value in _DOWNLOAD_STATES.items()
                        ]
                    ).label("state"),
                    literal(0).label("priority"),
                    DownloadHistory.id.label("id"),
                ).where(
                    DownloadHistory.issue_id.in_(issues),
                    DownloadHistory.imported_at.is_(None),
                    DownloadHistory.state.in_(_DOWNLOAD_STATES),
                ),
                select(
                    DirectAcquisitionAttempt.issue_id,
                    case(
                        *[
                            (DirectAcquisitionAttempt.state == key, value.value)
                            for key, value in _DIRECT_STATES.items()
                        ]
                    ).label("state"),
                    literal(1).label("priority"),
                    DirectAcquisitionAttempt.id.label("id"),
                ).where(
                    DirectAcquisitionAttempt.issue_id.in_(issues),
                    DirectAcquisitionAttempt.state.in_(_DIRECT_STATES),
                ),
            ).order_by("priority", desc("id"))
        )
    ).all()
    active: dict[int, ReleaseIssueStatus] = {}
    for issue_id, state, _priority, _id in downloads:
        active.setdefault(issue_id, ReleaseIssueStatus(state))
    result: list[ReleaseIssueState | None] = []
    for release in releases:
        nested = release.get("series")
        identity = positive_id(nested.get("locg_series_id")) if isinstance(nested, dict) else None
        identity = identity or positive_id(release.get("locg_series_id"))
        owner = owners.get(identity or "")
        if owner is None:
            result.append(None)
            continue
        resolved = [
            _resolve(source, owner.id, identity, claimed, disputed, issues, by_number, by_cv_id)
            for source in _sources(release)
        ]
        if not resolved or None in resolved or len(set(resolved)) != 1:
            result.append(ReleaseIssueState(ReleaseIssueStatus.UNRESOLVED))
            continue
        issue_id = resolved[0]
        assert issue_id is not None
        issue = issues[issue_id]
        state = active.get(issue_id)
        if state is None:
            if issue_id in files:
                state = ReleaseIssueStatus.OWNED
            elif issue_id in pending_issues or issue.status in {
                IssueStatus.OWNED,
                IssueStatus.DOWNLOADING,
                IssueStatus.UNKNOWN,
            }:
                state = ReleaseIssueStatus.NEEDS_REVIEW
            else:
                state = (
                    ReleaseIssueStatus.SKIPPED
                    if issue.status is IssueStatus.SKIPPED
                    else ReleaseIssueStatus.MISSING
                )
        result.append(ReleaseIssueState(state, issue_id))
    return result


def _resolve(
    release: Mapping[str, object],
    series_id: int,
    locg_series_id: str | None,
    claimed: Mapping[tuple[IdentityNamespace, str], IssueExternalIdentity],
    disputed: set[int],
    issues: Mapping[int, Issue],
    by_number: Mapping[tuple[int, str], list[Issue]],
    by_cv_id: Mapping[int, Issue],
) -> int | None:
    try:
        if release_series_id(dict(release)) != locg_series_id or not positive_id(
            release.get("locg_issue_id")
        ):
            return None
    except WhatsNewSelectionError:
        return None
    number = _number(release.get("issue_number"))
    refs = _refs(release)
    if refs:
        matches = []
        for namespace, external_id in refs.items():
            claim = claimed.get((namespace, external_id)) if external_id else None
            issue = issues.get(claim.issue_id) if claim else None
            if claim and claim.verification_state is not IdentityVerificationState.VERIFIED:
                return None
            if claim is None and namespace is IdentityNamespace.COMICVINE and external_id:
                issue = by_cv_id.get(int(external_id))
            if issue is None or issue.series_id != series_id or issue.id in disputed:
                return None
            if namespace is IdentityNamespace.COMICVINE and issue.comicvine_id not in {
                None,
                int(external_id or "0"),
            }:
                return None
            if number and issue.effective_issue_number_text != number:
                return None
            matches.append(issue.id)
        return matches[0] if matches and len(set(matches)) == 1 else None
    observed = _date(release.get("store_date"))
    candidates = [
        issue
        for issue in by_number.get((series_id, number or ""), [])
        if observed
        and (issue.store_date or issue.release_date) == observed
        and issue.issue_type is IssueType.ISSUE
        and issue.id not in disputed
    ]
    return candidates[0].id if len(candidates) == 1 else None
