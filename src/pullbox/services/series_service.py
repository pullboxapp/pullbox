"""Series service — CRUD operations and monitoring logic for series."""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from pullbox.core.events import EventBus, IssueWanted, SeriesAdded
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.library_policy import load_library_naming_policy
from pullbox.core.naming import format_series_folder
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.import_job import ImportJob, ImportSourceType
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.models.series import (
    IssueCatalogState,
    Series,
    SeriesStatus,
    SeriesStatusOverride,
    SeriesType,
)
from pullbox.providers.base import IssueSummary, SeriesMetadata
from pullbox.services.cover_cache_service import (
    cache_imported_series_cover,
    find_imported_series_cover,
    purge_series_cover_cache,
)
from pullbox.services.series_delete_targets import (
    SeriesDeleteContext as SeriesDeleteContext,
)
from pullbox.services.series_delete_targets import (
    SeriesDeleteTarget as SeriesDeleteTarget,
)
from pullbox.services.series_delete_targets import (
    build_series_delete_context,
    build_series_delete_target,
    dedupe_existing_paths,
    is_relative_to,
    path_key,
    reclaim_transient_series_folder,
    trash_relative_path,
)
from pullbox.utilities.settings import move_path_to_utility_trash

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedSeries
    from pullbox.services.metadata_service import MetadataService

logger = structlog.get_logger(__name__)


def _targeted_import_folder_type_hint(
    series: Series,
    issue_summaries: list[IssueSummary],
) -> SeriesType | None:
    """Return source-only folder naming evidence for a standalone one-shot.

    ComicVine can model a one-shot as issue #1 of an otherwise standard volume.
    Keep that catalog classification intact, but retain an explicit source hint
    in the initial folder name when the selected volume contains only that item.
    """
    if series.series_type != SeriesType.STANDARD or series.issue_count != 1:
        return None
    if len(issue_summaries) != 1:
        return None
    return (
        SeriesType.ONE_SHOT if issue_summaries[0].issue_type.strip().lower() == "one_shot" else None
    )


async def _cancel_download_on_client(download: DownloadHistory, session: AsyncSession) -> None:
    """Best-effort cancellation on the download client.

    Failures are logged but never raised — the user's intent is to delete
    the series regardless of client state.
    """
    if not download.external_id:
        return

    from pullbox.composition.providers import register_download_clients
    from pullbox.providers.base import ProviderRegistry

    registry = ProviderRegistry()
    await register_download_clients(session, registry)

    is_torrent = download.download_client == DownloadClientType.QBITTORRENT
    client = registry.get_torrent_client() if is_torrent else registry.get_nzb_client()

    if not client:
        logger.warning(
            "cancel_no_client_configured",
            download_id=download.id,
            client_type=download.download_client,
        )
        return

    try:
        removed = await client.remove_download(download.external_id, delete_files=True)
        if removed:
            logger.info("download_cancelled_on_client", download_id=download.id)
        else:
            logger.info(
                "download_not_found_on_client",
                download_id=download.id,
                external_id=download.external_id,
            )
    except Exception:
        logger.exception("cancel_client_error", download_id=download.id)


class SeriesService:
    """Manages series lifecycle: add, update, search, delete.

    Args:
        metadata_service: For fetching ComicVine data when adding series.
        event_bus: For emitting domain events.
    """

    def __init__(
        self,
        metadata_service: MetadataService,
        event_bus: EventBus,
    ) -> None:
        self._metadata = metadata_service
        self._event_bus = event_bus

    async def add_from_comicvine(
        self,
        session: AsyncSession,
        comicvine_id: int,
        library_root_id: int | None = None,
        *,
        search_on_add: bool = False,
    ) -> Series:
        """Full workflow: fetch metadata, create series + publisher, fetch issues.

        When *search_on_add* is True, monitored is set to True and all issues
        are marked WANTED with an auto-search triggered.  When False, monitored
        stays False and all issues remain SKIPPED.

        When *library_root_id* is provided, a series folder is created on disk
        using the configured naming template.  Folder collisions are resolved
        by appending ``[cv-{comicvine_id}]``.
        """
        log = logger.bind(comicvine_id=comicvine_id, search_on_add=search_on_add)
        log.info("series_add_from_comicvine")

        series_meta, issue_summaries = await self.prefetch_comicvine_bundle(comicvine_id)
        return await self.add_from_comicvine_prefetched(
            session,
            comicvine_id=comicvine_id,
            library_root_id=library_root_id,
            search_on_add=search_on_add,
            series_meta=series_meta,
            issue_summaries=issue_summaries,
        )

    async def add_from_import_review_targeted(
        self,
        session: AsyncSession,
        *,
        import_series: ImportedSeries,
        library_root_id: int | None = None,
        search_on_add: bool = False,
        issue_summaries: list[IssueSummary],
    ) -> Series:
        """Create a series from import-review metadata and targeted issue rows only."""
        cv_id = import_series.user_selected_cv_id or import_series.cv_id
        if cv_id is None:
            msg = "ComicVine ID is required for targeted import review series creation"
            raise ValidationError(msg)
        title = import_series.cv_title or import_series.raw_series_name
        series_meta = SeriesMetadata(
            provider_id=str(cv_id),
            title=title,
            sort_title=title,
            year_start=getattr(import_series, "cv_year", None)
            or getattr(import_series, "raw_year", None),
            year_end=None,
            status=None,
            publisher=getattr(import_series, "cv_publisher", None),
            description=None,
            cover_url=None,
            issue_count=getattr(import_series, "cv_issue_count", None),
            comicvine_url=getattr(import_series, "cv_url", None),
        )
        # Step 2 may already have fetched the complete series record. Reuse it
        # before folder creation so type-aware naming has the same metadata as
        # the later hydration pass, without adding a cold ComicVine request.
        cached_lookup = getattr(type(self._metadata), "get_cached_series_metadata", None)
        if cached_lookup is not None:
            cached_meta = await cached_lookup(self._metadata, cv_id)
            if cached_meta is not None:
                series_meta = cached_meta
        series = await self._metadata.upsert_series_metadata(session, cv_id, series_meta)
        series.monitored = search_on_add
        series.issue_catalog_state = IssueCatalogState.HYDRATING
        series.issue_catalog_error = None
        series.issue_catalog_last_synced_at = None
        series.issue_catalog_last_checked_at = None
        series.metadata_source = "comicvine_partial"

        if library_root_id is not None and not series.path:
            await self._create_series_folder(
                session,
                series,
                library_root_id,
                cv_id,
                folder_series_type=_targeted_import_folder_type_hint(series, issue_summaries),
            )

        local_cover = (
            find_imported_series_cover(Path(import_series.source_folder))
            if import_series.source_folder
            else None
        )
        if local_cover is not None:
            source_type = await session.scalar(
                select(ImportJob.source_type).where(ImportJob.id == import_series.import_job_id)
            )
            if source_type == ImportSourceType.MYLAR3:
                await cache_imported_series_cover(session, series, local_cover)

        if issue_summaries:
            await self._metadata.upsert_issue_summaries(session, series, issue_summaries)
        await session.flush()
        return series

    async def hydrate_series_catalog(
        self,
        session: AsyncSession,
        series_id: int,
        *,
        search_on_add: bool | None = None,
    ) -> Series:
        """Fetch the full ComicVine issue catalog for a targeted import series."""
        series = await session.get(Series, series_id)
        if series is None:
            raise NotFoundError("Series", series_id)
        if series.comicvine_id is None:
            raise ValidationError("Series has no ComicVine ID")

        series.issue_catalog_state = IssueCatalogState.HYDRATING
        series.issue_catalog_error = None
        await session.flush()
        try:
            series_meta, issue_summaries = await self.prefetch_comicvine_bundle(
                int(series.comicvine_id)
            )
            hydrated = await self.add_from_comicvine_prefetched(
                session,
                comicvine_id=int(series.comicvine_id),
                library_root_id=series.library_root_id,
                search_on_add=series.monitored if search_on_add is None else search_on_add,
                series_meta=series_meta,
                issue_summaries=issue_summaries,
            )
            return hydrated
        except Exception as exc:
            failed = await session.get(Series, series_id)
            if failed is not None:
                failed.issue_catalog_state = IssueCatalogState.FAILED
                failed.issue_catalog_error = str(exc)
                failed.issue_catalog_last_synced_at = None
                failed.issue_catalog_last_checked_at = None
                await session.flush()
            raise

    async def prefetch_comicvine_bundle(
        self,
        comicvine_id: int,
    ) -> tuple[SeriesMetadata, list[IssueSummary]]:
        """Fetch the remote series bundle ahead of the Step 4 writer lane."""
        series_meta, issue_summaries = await asyncio.gather(
            self._metadata.get_series_metadata(comicvine_id),
            self._metadata.get_issue_summaries_for_series(comicvine_id),
        )
        return series_meta, issue_summaries

    async def add_from_comicvine_prefetched(
        self,
        session: AsyncSession,
        comicvine_id: int,
        library_root_id: int | None = None,
        *,
        search_on_add: bool = False,
        series_meta: SeriesMetadata,
        issue_summaries: list[IssueSummary],
    ) -> Series:
        """Persist a series using provider data fetched outside the write-heavy section."""
        log = logger.bind(comicvine_id=comicvine_id, search_on_add=search_on_add)
        log.info("series_add_from_comicvine_prefetched")

        # Create/update the durable records after provider work is already in hand.
        series = await self._metadata.upsert_series_metadata(
            session,
            comicvine_id,
            series_meta,
        )

        # Set monitoring — driven by search_on_add
        series.monitored = search_on_add

        # Create a series folder when a library root was selected and the
        # series does not already have a canonical library location.
        if library_root_id is not None and not series.path:
            await self._create_series_folder(
                session,
                series,
                library_root_id,
                comicvine_id,
            )

        # Fetch all issues
        new_issues = await self._metadata.upsert_issue_summaries(
            session,
            series,
            issue_summaries,
            infer_series_type_from_summaries=True,
        )
        await session.flush()

        # Infer series status from issue release dates
        await self._metadata.infer_series_status(session, series)

        synced_at = datetime.now(UTC)
        series.issue_catalog_state = IssueCatalogState.COMPLETE
        series.issue_catalog_last_synced_at = synced_at
        series.issue_catalog_last_checked_at = synced_at
        series.issue_catalog_error = None

        # If monitored, mark all issues as WANTED
        if search_on_add:
            await self._apply_monitoring_on(session, series)

        await self._event_bus.emit(SeriesAdded(series_id=series.id, comicvine_id=comicvine_id))

        log.info(
            "series_added",
            series_id=series.id,
            title=series.title,
            new_issues=len(new_issues),
            path=series.path,
        )
        return series

    # ── Folder creation ───────────────────────────────────────────────

    @staticmethod
    async def _load_naming_config(session: AsyncSession) -> dict[str, str]:
        """Load naming-related config keys from the database."""
        policy = await load_library_naming_policy(session)
        return {
            "series_folder_template": policy.series_folder_template,
            "replace_illegal_characters": "true" if policy.replace_illegal_characters else "false",
            "colon_replacement": policy.colon_replacement,
        }

    async def _create_series_folder(
        self,
        session: AsyncSession,
        series: Series,
        library_root_id: int,
        comicvine_id: int,
        *,
        folder_series_type: SeriesType | None = None,
    ) -> None:
        """Create a series folder on disk inside the given library root.

        Sets ``series.path`` and ``series.library_root_id``.  If a folder
        with the same name already exists, appends ``[cv-{comicvine_id}]``
        to disambiguate.
        """
        root = await session.get(LibraryRoot, library_root_id)
        if root is None:
            raise ValidationError(f"Library root {library_root_id} not found")
        if not root.enabled:
            raise ValidationError(f"Library root '{root.name}' is disabled")

        # Load naming config
        cfg = await self._load_naming_config(session)
        template = cfg.get("series_folder_template", "{Series} ({Year})")
        replace_illegal = cfg.get("replace_illegal_characters", "true") == "true"
        colon_replacement = cfg.get("colon_replacement", "dash")

        # Resolve publisher name for the template
        publisher_name: str | None = None
        if series.publisher_id:
            await session.refresh(series, attribute_names=["publisher"])
            publisher_name = series.publisher.name if series.publisher else None

        # Format folder name
        folder_name = format_series_folder(
            title=series.title,
            year=series.year_start,
            publisher=publisher_name,
            comicvine_id=comicvine_id,
            series_type=(folder_series_type or series.series_type).value
            if (folder_series_type or series.series_type)
            else None,
            template=template,
            replace_illegal=replace_illegal,
            colon_replacement=colon_replacement,
        )

        root_path = Path(root.path)
        folder_path = root_path / folder_name

        # Reclaim empty/progress-only collision folders left behind by prior interrupted work.
        if folder_path.exists():
            reclaimed = await asyncio.to_thread(reclaim_transient_series_folder, folder_path)
            if reclaimed:
                logger.warning(
                    "series_folder_reclaimed_transient_collision",
                    path=str(folder_path),
                    comicvine_id=comicvine_id,
                )

        # Handle collision: append [cv-{id}] if folder already exists
        if folder_path.exists():
            folder_name_cv = f"{folder_name} [cv-{comicvine_id}]"
            folder_path = root_path / folder_name_cv
            logger.info(
                "series_folder_collision",
                original=folder_name,
                resolved=folder_name_cv,
                comicvine_id=comicvine_id,
            )

        # Create the directory (run in thread to avoid blocking event loop)
        await asyncio.to_thread(folder_path.mkdir, parents=False, exist_ok=True)

        series.path = str(folder_path)
        series.library_root_id = library_root_id

        logger.info(
            "series_folder_created",
            path=str(folder_path),
            series_title=series.title,
            library_root=root.name,
        )

    @staticmethod
    async def _build_series_folder_name(
        session: AsyncSession,
        series: Series,
    ) -> str | None:
        """Compute the canonical folder name for a series under its library root."""
        if series.library_root_id is None:
            return None

        cfg = await SeriesService._load_naming_config(session)
        template = cfg.get("series_folder_template", "{Series} ({Year})")
        replace_illegal = cfg.get("replace_illegal_characters", "true") == "true"
        colon_replacement = cfg.get("colon_replacement", "dash")

        publisher_name: str | None = None
        if series.publisher_id:
            await session.refresh(series, attribute_names=["publisher"])
            publisher_name = series.publisher.name if series.publisher else None

        return format_series_folder(
            title=series.title,
            year=series.year_start,
            publisher=publisher_name,
            comicvine_id=series.comicvine_id,
            series_type=series.series_type.value if series.series_type else None,
            template=template,
            replace_illegal=replace_illegal,
            colon_replacement=colon_replacement,
        )

    # ── Folder renaming ─────────────────────────────────────────────

    async def rename_series_folder(
        self,
        session: AsyncSession,
        series_id: int,
    ) -> str | None:
        """Rename a series folder to match the current naming template.

        Reads the naming config, computes the expected folder name, and
        renames the directory on disk if it differs.  Updates ``series.path``.

        Returns:
            The new path if renamed, or ``None`` if no rename was needed.
        """
        series = await session.get(Series, series_id)
        if not series or not series.path or not series.library_root_id:
            return None

        root = await session.get(LibraryRoot, series.library_root_id)
        if not root:
            return None

        # Load naming config
        cfg = await self._load_naming_config(session)
        template = cfg.get("series_folder_template", "{Series} ({Year})")
        replace_illegal = cfg.get("replace_illegal_characters", "true") == "true"
        colon_replacement = cfg.get("colon_replacement", "dash")

        # Resolve publisher
        publisher_name: str | None = None
        if series.publisher_id:
            await session.refresh(series, attribute_names=["publisher"])
            publisher_name = series.publisher.name if series.publisher else None

        # Compute expected folder name
        expected_name = format_series_folder(
            title=series.title,
            year=series.year_start,
            publisher=publisher_name,
            comicvine_id=series.comicvine_id,
            series_type=series.series_type.value if series.series_type else None,
            template=template,
            replace_illegal=replace_illegal,
            colon_replacement=colon_replacement,
        )

        root_path = Path(root.path)
        current_path = Path(series.path)
        expected_path = root_path / expected_name

        # No rename needed if already correct
        if current_path == expected_path:
            return None

        # Don't overwrite an existing different folder
        if expected_path.exists() and expected_path != current_path:
            reclaimed = await asyncio.to_thread(reclaim_transient_series_folder, expected_path)
            if reclaimed:
                logger.warning(
                    "series_folder_reclaimed_transient_collision",
                    series_id=series_id,
                    path=str(expected_path),
                )

        if expected_path.exists() and expected_path != current_path:
            if series.comicvine_id:
                expected_name = f"{expected_name} [cv-{series.comicvine_id}]"
                expected_path = root_path / expected_name
            if expected_path.exists() and expected_path != current_path:
                logger.warning(
                    "series_folder_rename_collision",
                    series_id=series_id,
                    current=str(current_path),
                    target=str(expected_path),
                )
                return None

        # Rename on disk
        if current_path.is_dir():
            try:
                await asyncio.to_thread(current_path.rename, expected_path)
                series.path = str(expected_path)
                logger.info(
                    "series_folder_renamed",
                    series_id=series_id,
                    old_path=str(current_path),
                    new_path=str(expected_path),
                )
                return str(expected_path)
            except OSError:
                logger.exception(
                    "series_folder_rename_failed",
                    series_id=series_id,
                    old_path=str(current_path),
                    new_path=str(expected_path),
                )
                return None
        else:
            # Folder doesn't exist on disk — just update path
            series.path = str(expected_path)
            return str(expected_path)

    async def rename_all_series_folders(
        self,
        session: AsyncSession,
    ) -> dict[str, int]:
        """Rename all series folders to match the current naming template.

        Returns:
            Dict with ``renamed`` and ``skipped`` counts.
        """
        result = await session.execute(
            select(Series).where(
                Series.path.isnot(None),
                Series.library_root_id.isnot(None),
            )
        )
        series_list = list(result.scalars().all())

        renamed = 0
        skipped = 0
        for s in series_list:
            new_path = await self.rename_series_folder(session, s.id)
            if new_path:
                renamed += 1
            else:
                skipped += 1

        if renamed:
            await session.flush()
            logger.info("series_folders_rename_all", renamed=renamed, skipped=skipped)

        return {"renamed": renamed, "skipped": skipped}

    @staticmethod
    async def build_delete_target(
        session: AsyncSession,
        series: Series,
    ) -> SeriesDeleteTarget:
        """Resolve the actual delete target for one series."""
        return await build_series_delete_target(
            session,
            series,
            build_series_folder_name=SeriesService._build_series_folder_name,
        )

    @staticmethod
    async def build_delete_context(
        session: AsyncSession,
        series_ids: list[int],
    ) -> SeriesDeleteContext:
        """Build delete-modal UI state for one or more series."""
        return await build_series_delete_context(
            session,
            series_ids,
            target_builder=SeriesService.build_delete_target,
        )

    async def toggle_monitoring(
        self,
        session: AsyncSession,
        series_id: int,
        enabled: bool,
    ) -> Series:
        """Toggle monitoring for a series.

        When enabling: all SKIPPED issues (except manual_skip) become WANTED.
        When disabling: all WANTED issues become SKIPPED, DOWNLOADING issues
        are cancelled gracefully.  OWNED issues are never touched.
        """
        series = await session.get(Series, series_id)
        if not series:
            raise NotFoundError("Series", series_id)

        if series.monitored == enabled:
            return series  # No change needed

        series.monitored = enabled

        if enabled:
            await self._apply_monitoring_on(session, series)
        else:
            await self._apply_monitoring_off(session, series)

        logger.info(
            "series_monitoring_toggled",
            series_id=series_id,
            monitored=enabled,
        )
        return series

    async def set_status_override(
        self,
        session: AsyncSession,
        series_id: int,
        status_override: SeriesStatusOverride | None,
    ) -> Series:
        """Set or clear a user-owned series lifecycle status."""
        series = await session.get(Series, series_id)
        if not series:
            raise NotFoundError("Series", series_id)

        series.status_override = status_override
        if status_override is None:
            if series.comicvine_id is not None:
                series = await self._metadata.refresh_series(session, series_id, force=True)
            else:
                await self._metadata.infer_series_status(session, series)
        else:
            series.status = SeriesStatus(status_override.value)
            if status_override == SeriesStatusOverride.CONTINUING:
                series.year_end = None
            else:
                series.year_end = await self._metadata.derive_series_end_year(session, series)

        logger.info(
            "series_status_override_updated",
            series_id=series_id,
            status=series.status.value,
            status_override=status_override.value if status_override else None,
        )
        return series

    @staticmethod
    async def get_by_id(session: AsyncSession, series_id: int) -> Series:
        """Get a series by ID."""
        series = await session.get(Series, series_id)
        if not series:
            raise NotFoundError("Series", series_id)
        # noinspection PyTypeChecker
        return series

    @staticmethod
    async def get_all(
        session: AsyncSession,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Series], int]:
        """Get all series with pagination. Returns (items, total)."""
        total = (await session.execute(select(func.count(Series.id)))).scalar_one()

        result = await session.execute(
            select(Series).order_by(Series.sort_title).limit(limit).offset(offset)
        )
        return list(result.scalars().all()), total

    @staticmethod
    async def search(
        session: AsyncSession,
        query: str,
        limit: int = 20,
    ) -> list[Series]:
        """Search series by title."""
        result = await session.execute(
            select(Series)
            .where(Series.title.ilike(f"%{query}%"))
            .order_by(Series.sort_title)
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def update_alternate_names(
        session: AsyncSession,
        series_id: int,
        alternate_names: list[str],
    ) -> Series:
        """Replace the alternate names list for a series."""
        series = await session.get(Series, series_id)
        if not series:
            raise NotFoundError("Series", series_id)

        # Deduplicate and strip whitespace, preserving order
        seen: set[str] = set()
        cleaned: list[str] = []
        for name in alternate_names:
            stripped = name.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                cleaned.append(stripped)

        series.alternate_names = cleaned
        logger.info(
            "series_alternate_names_updated",
            series_id=series_id,
            count=len(cleaned),
        )
        # noinspection PyTypeChecker
        return series

    @staticmethod
    async def delete(
        session: AsyncSession,
        series_id: int,
        *,
        delete_files: bool = False,
        delete_folder: bool = False,
        trash_dir: Path | None = None,
        folder_path_override: Path | None = None,
    ) -> None:
        """Delete a series with optional file/folder cleanup and download cancellation.

        Handles:
        1. Cancelling active downloads on the client (SABnzbd/qBittorrent)
        2. Removing physical library files from disk (when delete_files=True)
        3. Removing the series folder from disk (when delete_folder=True)
        4. Deleting the series record (cascades to issues and download history)
        """
        series = await session.get(Series, series_id)
        if not series:
            raise NotFoundError("Series", series_id)

        series_title = series.title
        root = (
            await session.get(LibraryRoot, series.library_root_id)
            if trash_dir is not None and series.library_root_id is not None
            else None
        )

        # ── 1. Cancel active downloads on client ────────────────────
        cancellable = frozenset(
            {
                DownloadState.QUEUED,
                DownloadState.SENT,
                DownloadState.DOWNLOADING,
                DownloadState.FINALIZING,
                DownloadState.PAUSED,
                DownloadState.RETRY_PENDING,
            }
        )

        issue_ids_subq = select(Issue.id).where(Issue.series_id == series_id)
        dl_result = await session.execute(
            select(DownloadHistory).where(
                DownloadHistory.issue_id.in_(issue_ids_subq),
                DownloadHistory.state.in_(cancellable),
            )
        )
        active_downloads = list(dl_result.scalars().all())

        for dl in active_downloads:
            # Best-effort client cancellation
            if dl.external_id:
                await _cancel_download_on_client(dl, session)

            # Clear progress cache
            from pullbox.tasks.download_task import _clear_progress

            _clear_progress(dl.id)

            dl.state = DownloadState.FAILED
            dl.error_message = "Cancelled: series deleted"

        # ── 2. Delete library files from disk ───────────────────────
        effective_delete_files = delete_files or delete_folder
        file_result = await session.execute(
            select(LibraryFile).where(LibraryFile.issue_id.in_(issue_ids_subq))
        )
        linked_files = list(file_result.scalars().all())
        managed_files = [
            library_file
            for library_file in linked_files
            if library_file.storage_mode == LibraryFileStorageMode.MANAGED
        ]
        referenced_files = [
            library_file
            for library_file in linked_files
            if library_file.storage_mode == LibraryFileStorageMode.REFERENCED
        ]

        delete_target = await SeriesService.build_delete_target(session, series)
        folder_paths = (
            dedupe_existing_paths([folder_path_override.expanduser()])
            if folder_path_override is not None
            else delete_target.folder_paths
        )
        moved_folder_keys: set[str] = set()
        if delete_folder:
            for folder_path in folder_paths:
                folder_contains_reference = any(
                    is_relative_to(Path(library_file.file_path), folder_path)
                    for library_file in referenced_files
                )
                if folder_contains_reference:
                    logger.info(
                        "referenced_folder_preserved",
                        series_id=series_id,
                        path=str(folder_path),
                    )
                    continue
                if trash_dir is not None:
                    try:
                        move_path_to_utility_trash(
                            folder_path,
                            trash_dir,
                            relative_path=trash_relative_path(folder_path, root),
                        )
                    except FileExistsError as exc:
                        raise ValidationError(str(exc)) from exc
                    moved_folder_keys.add(path_key(folder_path))
                    continue
                if folder_path.is_symlink():
                    folder_path.unlink()
                    continue
                if folder_path.is_dir():
                    try:
                        shutil.rmtree(folder_path)
                    except PermissionError:
                        logger.exception("folder_delete_permission_error", path=str(folder_path))

        if effective_delete_files:
            for lf in linked_files:
                file_path = Path(lf.file_path)
                if lf.storage_mode == LibraryFileStorageMode.REFERENCED:
                    await session.delete(lf)
                    logger.info(
                        "referenced_file_detached",
                        series_id=series_id,
                        library_file_id=lf.id,
                        path=str(file_path),
                    )
                    continue
                if moved_folder_keys and any(
                    path_key(folder_path) in moved_folder_keys
                    and is_relative_to(file_path, folder_path)
                    for folder_path in folder_paths
                ):
                    await session.delete(lf)
                    continue
                if file_path.is_file():
                    try:
                        if trash_dir is not None:
                            move_path_to_utility_trash(
                                file_path,
                                trash_dir,
                                relative_path=trash_relative_path(file_path, root),
                            )
                        else:
                            file_path.unlink()
                    except OSError:
                        logger.exception("file_delete_failed", path=str(file_path))
                await session.delete(lf)
        else:
            for lf in referenced_files:
                await session.delete(lf)
                logger.info(
                    "referenced_file_detached",
                    series_id=series_id,
                    library_file_id=lf.id,
                    path=lf.file_path,
                )

        # ── 3. Delete series record (cascades to issues) ────────────
        await purge_series_cover_cache(session, series_id)
        await session.delete(series)
        logger.info(
            "series_deleted",
            series_id=series_id,
            title=series_title,
            files_deleted=delete_files,
            folder_deleted=delete_folder,
            trashed=trash_dir is not None,
            managed_files_deleted=len(managed_files) if effective_delete_files else 0,
            referenced_files_detached=len(referenced_files),
        )

    async def _apply_monitoring_on(
        self,
        session: AsyncSession,
        series: Series,
    ) -> None:
        """Enable monitoring: SKIPPED → WANTED (respects manual_skip).

        Issues with manual_skip=True are left as SKIPPED.
        OWNED issues are never touched.
        """
        result = await session.execute(
            select(Issue).where(
                Issue.series_id == series.id,
                Issue.status == IssueStatus.SKIPPED,
                Issue.manual_skip.is_(False),
            )
        )
        issues = list(result.scalars().all())

        for issue in issues:
            issue.status = IssueStatus.WANTED
            await self._event_bus.emit(IssueWanted(issue_id=issue.id, series_id=series.id))

    async def _apply_monitoring_off(
        self,
        session: AsyncSession,
        series: Series,
    ) -> None:
        """Disable monitoring: WANTED/DOWNLOADING → SKIPPED.

        OWNED issues are never touched.  manual_skip flags are preserved.
        DOWNLOADING issues are set to SKIPPED — the download client may
        finish but Pullbox won't process the result.
        """
        result = await session.execute(
            select(Issue).where(
                Issue.series_id == series.id,
                Issue.status.in_([IssueStatus.WANTED, IssueStatus.DOWNLOADING]),
            )
        )
        for issue in result.scalars().all():
            issue.status = IssueStatus.SKIPPED
