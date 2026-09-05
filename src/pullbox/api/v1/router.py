"""V1 API router — aggregates all sub-routers under /api/v1/."""

from fastapi import APIRouter

from pullbox.api.v1.activity import router as activity_router
from pullbox.api.v1.audit import router as audit_router
from pullbox.api.v1.auth import router as auth_router
from pullbox.api.v1.blocklist import router as blocklist_router
from pullbox.api.v1.clients import router as clients_router
from pullbox.api.v1.config import router as config_router
from pullbox.api.v1.covers import router as covers_router
from pullbox.api.v1.direct_hosts import router as direct_hosts_router
from pullbox.api.v1.direct_providers import router as direct_providers_router
from pullbox.api.v1.direct_resolver import router as direct_resolver_router
from pullbox.api.v1.downloads import router as downloads_router
from pullbox.api.v1.filesystem import router as filesystem_router
from pullbox.api.v1.health import router as health_router
from pullbox.api.v1.import_completed_cleanup import router as import_completed_cleanup_router
from pullbox.api.v1.import_job_archive import router as import_job_archive_router
from pullbox.api.v1.import_jobs import router as import_jobs_router
from pullbox.api.v1.import_safety_bulk import router as import_safety_bulk_router
from pullbox.api.v1.indexers import router as indexers_router
from pullbox.api.v1.intervention import router as intervention_router
from pullbox.api.v1.issues import router as issues_router
from pullbox.api.v1.library import router as library_router
from pullbox.api.v1.reader import router as reader_router
from pullbox.api.v1.search import router as search_router
from pullbox.api.v1.series import router as series_router
from pullbox.api.v1.story_arc_placements import router as story_arc_placements_router
from pullbox.api.v1.story_arcs import router as story_arcs_router
from pullbox.api.v1.suggestions import router as suggestions_router
from pullbox.api.v1.system import router as system_router
from pullbox.api.v1.whats_new import router as whats_new_router
from pullbox.utilities.router import router as utilities_router

v1_router = APIRouter(prefix="/api/v1")

v1_router.include_router(activity_router)
v1_router.include_router(audit_router)
v1_router.include_router(blocklist_router)
v1_router.include_router(auth_router)
v1_router.include_router(series_router)
v1_router.include_router(story_arcs_router)
v1_router.include_router(story_arc_placements_router)
v1_router.include_router(issues_router)
v1_router.include_router(library_router)
v1_router.include_router(reader_router)
v1_router.include_router(downloads_router)
v1_router.include_router(direct_providers_router)
v1_router.include_router(direct_hosts_router)
v1_router.include_router(direct_resolver_router)
v1_router.include_router(search_router)
v1_router.include_router(indexers_router)
v1_router.include_router(clients_router)
v1_router.include_router(config_router)
v1_router.include_router(covers_router)
v1_router.include_router(filesystem_router)
v1_router.include_router(health_router)
v1_router.include_router(import_jobs_router)
v1_router.include_router(import_completed_cleanup_router)
v1_router.include_router(import_job_archive_router)
v1_router.include_router(import_safety_bulk_router)
v1_router.include_router(intervention_router)
v1_router.include_router(suggestions_router)
v1_router.include_router(system_router)
v1_router.include_router(whats_new_router)
v1_router.include_router(utilities_router)
