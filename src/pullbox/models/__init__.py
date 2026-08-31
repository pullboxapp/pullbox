"""
Pullbox ORM models — imports all models so Alembic autogenerate discovers them.

Import Base from here for migration target_metadata.
"""

from pullbox.models.airdcpp import AirDcppAcquisition, AirDcppClientSettings
from pullbox.models.audit_log import AuditEventType, AuditLog
from pullbox.models.base import Base, IdentityMixin, TimestampMixin
from pullbox.models.blocklist import BlocklistEntry, BlocklistReason
from pullbox.models.client import DownloadClientConfig
from pullbox.models.config import SystemConfig
from pullbox.models.creator import Creator, IssueCreator
from pullbox.models.dashboard import DashboardMetricRollup, DashboardStorageSnapshot
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
    DirectArtifactRouteKind,
    DirectArtifactState,
    DirectHostAccountState,
    DirectHostConfig,
    DirectHostOperationalResult,
    DirectHostReachabilityState,
    DirectProviderConfig,
    DirectProviderState,
    DirectProviderTrustLevel,
    DirectResolverConfig,
    DirectResolverKind,
    DirectResolverState,
)
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.health import (
    HealthCheckResult,
    HealthCurrentStatus,
    HealthIncident,
    HealthStatus,
)
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.indexer import IndexerConfig, IndexerSource, IndexerType
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryRoot,
    LibraryRootPolicy,
    LibraryRootPolicySource,
    MatchConfidence,
)
from pullbox.models.matching_suggestion import MatchingSuggestion, SuggestionStatus
from pullbox.models.operation_progress import (
    OperationProgress,
    OperationProgressState,
    OperationProgressTone,
    OperationProgressType,
    OperationProgressVisibility,
)
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.provider_cache import MetadataProviderCacheEntry
from pullbox.models.publisher import Publisher
from pullbox.models.reader import IssueReaderState
from pullbox.models.scheduler_task_stat import ScheduledTaskStat
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import (
    IssueCatalogState,
    Series,
    SeriesStatus,
    SeriesStatusOverride,
    SeriesType,
)
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcExternalIdentity,
    StoryArcLifecycle,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
    StoryArcSymlinkStyle,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import (
    StoryArcSyncReason,
    StoryArcSyncWork,
    StoryArcSyncWorkState,
)
from pullbox.models.user import APIKey, User
from pullbox.models.whats_new import WhatsNewCacheKind, WhatsNewReleaseCache
from pullbox.utilities.models import (
    ItemState,
    JobState,
    JobType,
    LogLevel,
    UtilityJob,
    UtilityJobItem,
    UtilityJobLog,
)

__all__ = [
    "APIKey",
    "AirDcppAcquisition",
    "AirDcppClientSettings",
    "AuditEventType",
    "AuditLog",
    "Base",
    "BlocklistEntry",
    "BlocklistReason",
    "Creator",
    "DashboardMetricRollup",
    "DashboardStorageSnapshot",
    "DirectAcquisitionAttempt",
    "DirectAcquisitionState",
    "DirectArtifactAttempt",
    "DirectArtifactFailureClass",
    "DirectArtifactHostKind",
    "DirectArtifactRouteKind",
    "DirectArtifactState",
    "DirectHostAccountState",
    "DirectHostConfig",
    "DirectHostOperationalResult",
    "DirectHostReachabilityState",
    "DirectProviderConfig",
    "DirectProviderState",
    "DirectProviderTrustLevel",
    "DirectResolverConfig",
    "DirectResolverKind",
    "DirectResolverState",
    "DownloadClientConfig",
    "DownloadClientType",
    "DownloadHistory",
    "DownloadState",
    "FileFormat",
    "HealthCheckResult",
    "HealthCurrentStatus",
    "HealthIncident",
    "HealthStatus",
    "IdentityMixin",
    "ImportFileHandlingMode",
    "ImportJob",
    "ImportJobLog",
    "ImportJobStatus",
    "ImportSeriesStatus",
    "ImportSourceType",
    "ImportedFile",
    "ImportedFileStatus",
    "ImportedSeries",
    "ImportedStoryArc",
    "ImportedStoryArcEntry",
    "ImportedStoryArcStatus",
    "IndexerConfig",
    "IndexerSource",
    "IndexerType",
    "Issue",
    "IssueCatalogState",
    "IssueCreator",
    "IssueReaderState",
    "IssueStatus",
    "IssueStoryArc",
    "IssueType",
    "ItemState",
    "JobState",
    "JobType",
    "LibraryFile",
    "LibraryRoot",
    "LibraryRootPolicy",
    "LibraryRootPolicySource",
    "LogLevel",
    "MatchConfidence",
    "MatchingSuggestion",
    "MetadataProviderCacheEntry",
    "OperationProgress",
    "OperationProgressState",
    "OperationProgressTone",
    "OperationProgressType",
    "OperationProgressVisibility",
    "PendingMatch",
    "PendingMatchStatus",
    "Publisher",
    "ScheduledTaskStat",
    "SearchLog",
    "SearchType",
    "Series",
    "SeriesStatus",
    "SeriesStatusOverride",
    "SeriesType",
    "StoryArc",
    "StoryArcExternalIdentity",
    "StoryArcLifecycle",
    "StoryArcPlacement",
    "StoryArcPlacementMode",
    "StoryArcPlacementOwnership",
    "StoryArcPlacementState",
    "StoryArcResolutionState",
    "StoryArcSourceKind",
    "StoryArcSymlinkStyle",
    "StoryArcSyncReason",
    "StoryArcSyncWork",
    "StoryArcSyncWorkState",
    "SuggestionStatus",
    "SystemConfig",
    "TimestampMixin",
    "User",
    "UtilityJob",
    "UtilityJobItem",
    "UtilityJobLog",
    "WhatsNewCacheKind",
    "WhatsNewReleaseCache",
]
