"""Pullbox background tasks -- scheduled jobs for search, downloads, scanning, and health.

Task modules are imported here so their ``@scheduled_task`` decorators
run and populate the task registry at import time.
"""

from pullbox.tasks import backup_task as backup_task
from pullbox.tasks import blocklist_task as blocklist_task
from pullbox.tasks import cover_backfill_task as cover_backfill_task
from pullbox.tasks import dashboard_task as dashboard_task
from pullbox.tasks import database_maintenance_task as database_maintenance_task
from pullbox.tasks import download_monitor_apply as download_monitor_apply
from pullbox.tasks import download_monitor_poll as download_monitor_poll
from pullbox.tasks import download_monitor_read as download_monitor_read
from pullbox.tasks import download_monitor_updates as download_monitor_updates
from pullbox.tasks import download_post_processing_cleanup as download_post_processing_cleanup
from pullbox.tasks import (
    download_post_processing_destination as download_post_processing_destination,
)
from pullbox.tasks import download_post_processing_queue as download_post_processing_queue
from pullbox.tasks import download_post_processing_runtime as download_post_processing_runtime
from pullbox.tasks import (
    download_post_processing_source_validation as download_post_processing_source_validation,
)
from pullbox.tasks import download_post_processing_sources as download_post_processing_sources
from pullbox.tasks import download_post_processing_transfer as download_post_processing_transfer
from pullbox.tasks import download_progress as download_progress
from pullbox.tasks import download_recovery as download_recovery
from pullbox.tasks import download_scheduler_task as download_scheduler_task
from pullbox.tasks import download_stall_recovery as download_stall_recovery
from pullbox.tasks import download_task as download_task
from pullbox.tasks import health_task as health_task
from pullbox.tasks import intervention_task as intervention_task
from pullbox.tasks import metadata_scheduler_task as metadata_scheduler_task
from pullbox.tasks import metadata_task as metadata_task
from pullbox.tasks import scan_task as scan_task
from pullbox.tasks import search_scheduler_task as search_scheduler_task
from pullbox.tasks import search_task as search_task
from pullbox.tasks import story_arc_metadata_task as story_arc_metadata_task
from pullbox.tasks import story_arc_sync_task as story_arc_sync_task
from pullbox.tasks import update_check_task as update_check_task
from pullbox.tasks import usage_stats_task as usage_stats_task
from pullbox.tasks import whats_new_task as whats_new_task
