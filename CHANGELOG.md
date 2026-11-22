# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- [Rehearse restoring a SQLite migration backup and its configuration](docs/sqlite-postgresql-migration.md#back-up-and-rehearse-restoring-the-configuration).
  Verify that the restored deployment files match and its key can read saved credentials,
  without starting Pullbox or revealing keys.

- [Capture a verified offline SQLite snapshot](docs/sqlite-postgresql-migration.md#capture-the-offline-sqlite-snapshot)
  for PostgreSQL migration without starting Pullbox. Committed WAL changes are
  included; a source restart or unfinished library operation blocks publication.

- [Check that a Docker SQLite source is offline](docs/sqlite-postgresql-migration.md#check-whether-the-docker-source-is-offline)
  before migration. The check identifies containers with overlapping writable
  mounts and detects a source restart without changing the running stack.

- Offline migration checks now verify physical Story Arc copies and links and
  compare local cover artwork, without recreating files or fetching images.

- [Sign in to review a staged PostgreSQL library](docs/sqlite-postgresql-migration.md#review-the-copied-library)
  in a read-only terminal before switching anything. Inspect issue ownership,
  reading orders, metadata sources and import follow-up without starting recovery or searches.

- [Check a staged migration's key and comic mounts offline](docs/sqlite-postgresql-migration.md#check-the-key-and-comic-files).
  Compare a sample or the entire registered library without reopening PostgreSQL,
  exposing credentials, or changing files.

- [Keep an offline PostgreSQL staging copy](docs/sqlite-postgresql-migration.md#keep-an-offline-staging-copy)
  of a verified SQLite backup. The copy stays closed to application connections,
  including after an interruption; your running library is not switched.

- [Rehearse a SQLite library transfer](docs/sqlite-postgresql-migration.md#rehearse-a-copy)
  against a disposable PostgreSQL 16 or 18 database. The rehearsal verifies
  records and relationships without changing your files or switching databases.
- Before a PostgreSQL migration, you can now [check the destination](docs/sqlite-postgresql-migration.md#check-the-destination)
  without changing it. Pullbox explains when a database is occupied, shared, or
  unsuitable. Neither the source nor the target command switches your live library.

- Check a SQLite backup before planning a PostgreSQL migration with
  [migration-preflight](docs/sqlite-postgresql-migration.md). It reports table
  counts, broken relationships, and unfinished work without changing either database.

- **Review related series** in the Story Arc deletion preview shows which issues
  fall outside the arc and which other reading orders link to each series.
  No series is selected by default. Select individual series to delete their
  records along with the arc while keeping every comic file on disk. The
  confirmation names other reading orders that will lose a chapter link.
  If the selection changes before the job finishes, no records are deleted.
- When removing a related series, **Choose series files** lists its registered
  paths, including issues outside the reading order. Only the managed files
  you select are removed. The confirmation shows which paths go to Trash or
  are permanently deleted. Cancellation keeps the arc and series records but
  does not restore files already removed.
- In the same review you can independently select the arc's reading-order
  copies or links. The confirmation lists these separately from library
  originals. Removing just an arc copy or link leaves its original on disk.
- Story Arc details now offer **Delete Story Arc** alongside Archive. Review the
  reading order that will be removed and the comics that will stay. Deleting the
  arc alone leaves your series, issues, other arcs, and all files untouched.
- Choose **Review library comics** in the deletion preview to see each registered
  file's issue number, path, ownership, and links to import history. For shared
  issues, expand the list to see which other reading orders use them, including
  archived arcs. No library files are selected by default.
- You can now explicitly select original library comics when deleting a Story Arc.
  The preview names the affected files and other arcs, and shows which issues
  would become skipped because their last copy is being removed. Shared comics
  require an extra confirmation. If cleanup is interrupted, you can finish the
  recorded file operation without deleting the arc or starting another removal.
- To clean up arc copies or links at the same time, choose **Review arc files**,
  select individual paths, and review whether they will go to Trash or be
  permanently removed. Follow progress in the Utilities queue. Cancelling keeps
  the reading list and stops further removals; it does not undo completed ones.
- The deletion preview lists the exact recorded paths for arc copies, links and
  references. Managed placement removal now protects paths that have become
  library files, destinations no longer approved for writes, and import-owned
  placements needed for rollback.
- Removing an individual managed Story Arc copy or link now honors the configured
  trash folder. If Pullbox cannot verify the trash copy, it stops rather than
  permanently deleting the arc file. The original library comic stays in place.
- If a story arc or its membership changes while you read, you can stay on
  the same page and explicitly continue in series order. An old arc link
  can still open the issue in series order if its file is available.
- If a story arc chapter is missing on disk, damaged, or blocked by the reader's
  safety limits, you can keep your page and choose the next or previous available
  chapter. Pullbox rechecks the file and reading order first so you don't pass
  over a repaired chapter. The unavailable issue is not marked read.
- When a story arc has an unmatched chapter or no supported file for it,
  the reader names the gap instead of silently skipping it. You can move
  to the next or previous available issue, or return to the arc. Going
  past a chapter does not mark it read or change its acquisition status.
- Series Details now offers **Read series** at the first readable issue and
  **Continue series** at the issue you last read in series order. Reading
  through a story arc does not move your series bookmark. If the bookmarked
  issue is no longer available, you can choose another instead.
- **Reading order** lets you switch between series order and any confirmed
  story arc without losing the page or zoom. Previous and next follow your
  choice; closing the reader returns to the issue in that context.
- **Read story arc** opens the first chapter. Once a page has been displayed
  and its progress saved, **Continue story arc** opens your bookmarked chapter
  using that comic's normal page progress. Each user and arc has a separate
  bookmark. A removed or unavailable chapter is never silently replaced.
- Opening an issue from a Story Arc now follows the arc's saved reading order,
  even across series. The reader shows your position and identifies unavailable
  neighboring entries. Opening an issue directly still uses series order.
- Add Series and What's New > Find & Add can now create the first `series.json`
  in the new managed folder. Check the option in the Add dialog. Existing
  files are never replaced; if the write is blocked, the series stays in your
  library and the dialog offers folder review.
- Series Details can now update `series.json` in all eligible managed folders
  when you click **Refresh metadata**. Turn it on in **Settings > Metadata >
  Refresh policy**. Skipped folders can be reviewed without refreshing again;
  comic files and folders kept in place are unchanged.
- What's New refresh now clears a Watch from the Watching list when fresh
  publication data confirms a series already tracked in the selected library.
  It does not add a second series, change monitoring, or download issues.
- A Watch can now add a newly published series to its selected library when the
  release feed identifies it exactly and an enabled provider confirms the full
  issue catalog. The new series uses normal monitored-series behavior. Temporary
  provider failures retry on refresh; ambiguous matches still offer Find & Add.
- Added review of proven Metron/GCD identity disagreements on recorded Mylar
  files, with linked-record details, source recheck and Skip. Unknown native IDs
  do not invalidate a valid Mylar match or create provider links.
- Added folder-import matching for CBZ files with exact Metron identities already
  verified in the library. These files can import without a ComicVine identity;
  conflicting or unlinked identities stay in review and originals remain intact.
- Added Metron/GCD library-series and issue choices in import review, with
  search, preview and Add for missing series. Adding a series leaves file choices
  explicit, and import rollback preserves independently added series.
- Added folder-import recognition of compiled `series.json` provider identities.
  Exact file identities can match verified local issues across linked providers;
  a series-only sidecar still requires an explicit issue choice for each file.
- Added searched, paginated Metron/GCD issue choices to completed-import Follow-up
  for identified library series. Choices remain local until saved; stale links,
  competing copies, and changed sources cannot silently replace owned files.
- Added library selection and Metron/GCD search, preview and Add to **Find series**
  in folder-import Follow-up. Users can finish deferred files without rescanning;
  series selection still requires explicit issue choices before recovery.
- Added separate Metron/GCD review groups for exact-identity archives found in
  Mylar folders but absent from Mylar's issue records. Recorded Mylar issues and
  untagged neighbors retain their existing matching paths.
- Added compiled `series.json` matching for unrecorded Mylar files. Verified
  per-file issue IDs can resolve across linked catalogs; a series-only sidecar
  offers explicit issue choices without automatically matching its neighbors.
- Added opt-in Metron API and read-only GCD Local series search, provider linking,
  and reviewed metadata refresh alongside the existing ComicVine sources.
- Added explicit existing-CBZ metadata review and coordinated ComicInfo.xml and
  MetronInfo.xml output, including source choices for conflicting descriptive
  values. Identity conflicts, reference-only files, and unsafe archives remain blocked.
- Added a Series Details preview and approved `series.json` write using the saved
  reconciled metadata, verified provider links, and field provenance. Safe custom
  JSON fields are preserved; shared, reference-only, and read-only folders stay
  unchanged. Split managed folders are reviewed individually.
- Added reviewed single-issue metadata editing and a separate series editor for
  saved library metadata and `series.json`. Writes retain verified identities,
  field provenance, and coordinated ComicInfo.xml/MetronInfo.xml output.
- Added explicit series-to-comic metadata propagation with selected fields and
  files, per-file previews, background progress, cancellation, and resumable writes.
  Saving series metadata alone does not modify comic files.
- Added recovery and dismissal actions for interrupted AirDC++ work, a reviewed
  Grab anyway action for eligible rejected results, and a replacement workflow
  for incomplete story-only downloads.

### Fixed

- Issue details can now open after migration when an older database stored the
  issue type as `issue` instead of `ISSUE`. Ownership, issue numbers and Story
  Arc links are preserved. System > About identifies PostgreSQL and its version,
  and startup logs identify the database engine without printing its connection string.

- On PostgreSQL, **System > Backup** now explains [native backup and restore](docs/sqlite-postgresql-migration.md#back-up-and-restore-a-postgresql-library).
  Pullbox no longer schedules SQLite ZIP backups, prunes a previous SQLite
  library's restore points, or processes its pending restore marker.

- PostgreSQL now preserves import file sizes above 2 GiB, duplicate and
  safety-approved import outcomes, and series classified as volumes.
- Fixed a dashboard error after signing in on PostgreSQL. On SQLite, pending
  matching suggestions are now counted even if they were created with the
  database default. Previously accepted or dismissed suggestions stay closed.
- Database setup now accepts percent-encoded characters in the PostgreSQL
  password without printing the connection URL in a configuration error.
- Fresh PostgreSQL installations can now run the historical schema migrations
  to completion. Existing SQLite libraries do not need to rerun migrations.
  This does not yet enable moving a library between database engines.
- Deleting a series now keeps its chapters in surviving Story Arcs as missing,
  with their reading order and issue labels intact. Skipped chapters stay
  skipped. If an arc still has a managed copy or pending file work, the delete
  dialog names the arc instead of reporting a generic failure.
- Import Results now distinguishes pending file metadata updates from failures.
  Use **Check status** to see the latest outcome without retrying or reimporting files.
- Preserved the import page shell and working follow-up and rollback controls
  when using browser Back after reviewing an issue. History restoration reloads
  current import state without duplicating the application's script runtime.
- Required the patched Brotli decoder used by archive and HTTP decompression.
- Retained Metron/GCD issue identities when scanning and rechecking CBR, CB7,
  and CBT imports. Recorded Mylar matches still accept unknown native IDs;
  proven conflicts stay in review. Archive limits and original files are preserved.
- Fixed Step 3 source rechecks for confirmed Mylar copy imports whose paths need
  no translation. Recheck uses the exact saved source path without granting a
  directory root, bypassing archive safety, or changing the original file.
- Used matched series names for import selection controls, so files grouped
  from a mixed source folder remain distinguishable to screen-reader users.
- Included the visible background activity count in its accessible button label.
- Kept unresolved native files for existing library series visible in completed
  import Follow-up, showing their matched series rather than the source folder's
  title. Recovery retains explicit issue choices and original-file protection.
- Extended Follow-up's Recheck files action to recover exact Metron/GCD folder
  identities after their library links are verified, including compiled sidecars.
  Recovery preserves original files, rejects changed identities, and keeps
  ambiguous or competing copies for review without falling back to title guesses.
- Fixed an empty import issue picker when a selected library series and its
  verified issue catalog use different linked Metron/GCD identities.
- Kept import-review tab labels and counts readable during lane changes by
  switching their theme colors together without an intermediate color fade.
- Reconciled stale provider catalog counts and safely extended linked issue
  catalogs without replacing existing matches or owned files. Repeated Metron
  searches now respect provider cooldowns and distinguish timeouts from empty results.
- Recovered retained downloads and overlapping series packs without downloading
  or rewriting already imported comics. Oversized files can receive a bounded
  approval using the retained download instead of starting again.
- Corrected shared XML metadata reconciliation and restart-sensitive recovery
  checks while retaining archive, identity, and changed-file protections.
- Allowed metadata edits and conversion after completed imports. Later import
  rollback preserves edited files and dependent library records and reports that
  preservation instead of deleting the user's subsequent work.
- Corrected background activity counts and refresh, restored active download
  sizes, and prevented multiple issue tabs from exhausting activity connections
  or leaving the interface stuck after a stylesheet load failure.
- Added visible Story Arc preview loading and actionable folder validation and
  retry for separate reading-order copies, preserving canonical comics.

### Security

- Updated Pillow to the patched 13.0 line for image decoding and conversion.
- Updated the development stylesheet compiler's source-map parser and file
  watcher to remove vulnerable parser dependencies.

Automatic import metadata writing, GCD API v2, and LOCG enrichment are not enabled
by default in this development preview. The experimental Anna verification flow
is not included. This is not the completed v2.0 general release.

## [1.3.0] - 2026-09-15

Minor release adding Story Arc management, an optional local Comic Vine catalog,
multi-root and in-place library imports, and substantial large-library import
performance and recovery improvements.

### Added

- Added an optional local Comic Vine catalog for series searches and import
  matching without live metadata requests. Download it from Metadata settings
  with progress, resumable transfers, signed verification, and daily updates.
  Failed updates preserve the installed catalog; full metadata enrichment still
  uses the user's Comic Vine API key.
- Added import source-layout previews for series folders, publisher/series
  folders, and custom folder and issue naming patterns.
- Added independent options to keep existing files in place and use an approved
  layout for future managed files without reorganizing an existing library.
- Added first-class Story Arcs with ordered memberships, local issue selection,
  monitoring, Mylar Story Arc import, and folder-based arc evidence.
- Added optional separate Story Arc copies or links, reading-order filename
  prefixes, and safe previewed reordering while preserving canonical files and
  user-owned arc references.
- Added category-scoped bulk import safety review with bounded previews and
  audit evidence.
- Added a dedicated import Follow-up workspace for remaining matching, recovery,
  and cleanup decisions without restarting a completed import.
- Added previewed clean-library builds with background progress, configurable
  naming and conversion, and explicit safeguards for source files and destinations.
- Added safe removal of disabled library roots with dependency checks before
  deleting their configuration.
- Added Comic Vine Story Arc discovery, reviewed member ordering, canonical
  series/issue reuse, provider-change review, and arc-scoped missing-issue search.
- Added Mylar in-place adoption and original-filename arc copies with optional
  leading reading-order numbers. Initial copies can run independently of future
  synchronization and expose resumable progress and explicit retries.
- Added a guarded, manually published signed development-image channel for
  isolated testing without a general-availability release.

### Changed

- Grouped Mylar library-access problems by their underlying root and offered
  explicit resolution, skip, or detailed repair actions instead of presenting
  every affected series as a separate configuration problem.
- Unified Comic Vine series and Story Arc discovery, added a selectable header
  Add action, and simplified Story Arc reading-order review and reordering.
- Centralized library naming and Story Arc file policies, including independent
  permissions for referenced existing files and managed library destinations.

### Fixed

- Improved completed-import recovery for trusted legacy identities, misplaced
  files, and deferred outcomes. Bounded retries preserve successful imports,
  revalidate changed sources and approved roots, and resume interrupted work
  without losing matching evidence or repeating completed actions.
- Reconciled stale Mylar issue identifiers, renamed files, and unqualified volume
  identities when independent evidence proves the target. Mixed folders no longer
  require treating every file as belonging to the folder's series.
- Restored actionable recovery controls on completed jobs and kept safety-approved
  items, archived history, background activity, and import counters consistent.
- Preserved file ownership and dependent records during in-place adoption and
  clean-library execution, and corrected folder imports' reference-root setup.
- Sent torrent metadata through Pullbox to download clients rather than requiring
  remote clients to fetch private Prowlarr or indexer download URLs themselves.
- Corrected reader progress display for large compendiums.
- Improved Mylar and folder scan throughput with resource-capped archive
  inspection, batched review inserts, and fewer unnecessary duplicate checks.
  Corrected current-item scan progress and unknown time estimates, and bounded
  pending file-processing work without weakening cancellation or archive safety.
- Reconciled stale Mylar filenames with uniquely verified same-folder comic
  files, including `#1` versus `001` naming changes, without weakening identity
  or archive safety checks. Added a dry-run-first offline repair for saved
  reviews that preserves matches and source files, and clarified missing-path
  errors without claiming the source changed after scanning.
- Automatic searches now distinguish valid matches that could not be queued
  from empty results. Search history counts only validation rejections, not
  unused alternatives or failed downloads.
- Opted-in AirDC++ automatic searches now hand accepted matches to the durable
  queue, with restart-safe intervention for lower-confidence matches and
  duplicate-download and blocklist checks.
- SABnzbd NZB retrieval now allows slow indexer proxies a separate bounded
  timeout without delaying normal client-control requests.
- Recognized unmarked four-digit issue numbers for known series such as 2000 AD
  without treating numeric series names as issue numbers. Search confidence now
  uses known issue publication/store dates instead of comparing every issue to
  the series start year; undated continuing issues use a medium-confidence,
  bounded year window while issue, series, and type checks remain required.
- Corrected Mylar `series.json` metadata wrappers and `cvinfo` volume URL
  parsing in both Mylar and folder imports, without interpreting another
  application's unqualified `series_id` as a ComicVine identity.
- Isolated metadata-only and possible cover-only comic archives in import
  review while preserving valid series identities. Single-page approval is
  explicit and does not bypass archive safety limits.
- Added an offline, dry-run-first `recheck-import` maintenance command for
  affected saved reviews, preserving manual decisions and source files without
  repeating the directory scan.
- Raised the database-size health warning to above 1 GiB and the critical
  threshold to above 2 GiB to accommodate large collections and retained logs.
  Disk-space, integrity, latency, and database-bloat checks remain unchanged.
- Preserved supported Unicode spaces, joiners, and direction marks in Mylar
  source paths across preview, mapping, and import without renaming files or
  relaxing root-containment protections. Invalid path text no longer reports
  a misleading outside-root error.
- Kept Mylar scan counters, progress bars, and saved checkpoints in sync during
  file checks and source-page preparation instead of waiting for the full scan.
- Reconciled unambiguous Mylar filename format, case, and spacing changes within
  the same folder while retaining original-path evidence and leaving ambiguous
  or unavailable files for review. Source files and the Mylar database stay untouched.
- Made Mylar path preflight show searchable, exportable exceptions with exact
  paths and repair guidance, distinguishing missing folders from bad mappings.
  Added explicit, revalidated continuation with available sources while retaining
  unavailable records for review and keeping permission and safety failures blocked.
- Included recent Mylar preflight evidence in diagnostic packages even when an
  import cannot start.
- Matched issue-only filenames using series-folder context and handled issue
  titles embedded in filenames without treating the title as a new series.
- Preserved exact issue-number identity for large and special issue numbers
  instead of allowing floating-point or scientific-notation drift.
- Preserved exact issue numbers in managed filenames, embedded ComicInfo, and
  the reader, including suffixes and fractional padded filenames.
- Preserved other series' in-place files when deleting or trashing a shared
  folder, without treating literal folder-name characters as SQL wildcards.
- Applied sensitive-directory restrictions to import previews and preserved
  literal special characters when opening Mylar databases read-only.
- Preserved Mylar, ComicInfo, and supported series sidecar evidence through
  import review and matching without silently overriding stronger identities.
- Rechecked arc search eligibility after provider waits so newly skipped or
  removed members cannot be queued from stale search results.
- Fixed import-conflict review queries overflowing the parser stack on older
  SQLite builds while retaining server-side sorting and bounded pagination.
- Preserved replaced or edited Story Arc files during failed-publication
  cleanup instead of relying only on filesystem device and inode identifiers.

### Performance

- Prioritized visible catalog hydration after import while keeping ComicInfo
  writes in separate background work. Improved restart recovery and progress
  reporting, and prevented duplicate manual refreshes during initial metadata sync.
- Reduced large-library scan and review overhead with bounded worker budgets,
  compact projections, and batched database work. Import, rollback, and hydration
  progress now use clearer phase-aware completion and time estimates.
- Streamed complete import-review safety summaries using one narrow-field query
  instead of repeatedly scanning and sorting blocked files for every batch.
  Category counts, examples, and bulk-approval eligibility remain unchanged.
- Moved import archive safety checks off the application event loop and added
  throttled progress and cancellation checkpoints between Mylar file checks.
- Removed recursive comic-library sizing from diagnostic generation and moved
  blocking filesystem, database snapshot, and ZIP work off the application event
  loop. Mylar preflight filesystem analysis also runs on a worker thread.
- Bounded Mylar staging, file matching, and conflict rebuilding so large jobs do
  not retain complete ORM result sets or unbounded task queues.
- Moved filesystem inventory and source ordering to a private temporary SQLite
  spool, with bounded active workers, archive tasks, progress delivery, and
  cooperative cancellation cleanup.

## [1.2.1] - 2026-08-28

Patch release hardening AirDC++ acquisition recovery so retries, cancellation,
and merged queue bundles remain race-safe.

### Fixed

- Kept transient AirDC++ queue lookup failures from consuming the bounded queue
  mutation retry budget or prematurely failing an active remote download.
- Prevented AirDC++ acquisition and reconciliation from racing cancellation,
  including pre-bundle requests and remote mutations already in flight.
- Claimed missing AirDC++ bundle retries atomically, bound completion to the
  exact claim, preserved remote bundles merged with existing queue work, and
  prevented duplicate intents from taking ownership of the same bundle.
- Rechecked AirDC++ bundle ownership immediately before stale recovery cleanup
  so cancellation cannot delete a bundle adopted by another Pullbox download.

## [1.2.0] - 2026-08-28

Minor release adding native Direct Connect acquisition, LibGen direct-download
search, expanded private reading workflows, substantially faster trusted
imports, and unified background operation progress.

### Added

- Added native AirDC++ client configuration, supervised API sessions, Direct
  Connect search and ranking, live queue progress, completed-download import,
  and durable queue reconciliation.
- Added LibGen as a direct-download search and fallback source with bounded
  provider deadlines, safe artifact routing, and resumable transfers.
- Added a private Reading workspace with Continue, Want to Read, and Read views,
  plus dashboard, issue, series-detail, and series-registry reading state.
- Added safe previous/next issue navigation inside the embedded reader with
  awaited progress saves, final-page actions, and failure-safe context retention.
- Added durable operation progress and a global activity surface for imports,
  downloads, post-processing, utilities, and other background work.
- Added nightly SQLite optimization and reindex maintenance.

### Changed

- Evolved reading progress into independent resume, completion, and Want to Read
  dimensions keyed to the signed-in user and canonical issue.
- Preserved reading state across rename, relocation, conversion, replacement,
  file removal, and re-import, with page-count-aware resume reconciliation.
- Made trusted Mylar3 imports and sidecar-backed folder imports provider-free on
  their critical matching path while preserving genuine exceptions for review.
- Reused available Mylar series cover art and kept metadata enrichment
  asynchronous so imported libraries become usable sooner.
- Serialized direct-provider transfers to reduce artifact-host throttling and
  improve transfer reliability while leaving Usenet, torrent, and Direct
  Connect acquisition parallel.

### Fixed

- Normalized Windows drive-letter and UNC download paths safely, including
  case-insensitive Windows matching, traversal rejection, and protection from
  unintended filesystem-root scans.
- Kept CPU-heavy import and ComicInfo work off the request event loop so
  background processing no longer blocks normal application use.
- Recovered the application shell when a cached stylesheet fails to load.
- Treated generic HTTPS health per download instead of leaving the download
  client registry permanently degraded.
- Completed import cancellation, orphan recovery, and background progress
  lifecycle handling without stale countdowns or stranded activity entries.
- Improved direct-download and post-processing progress identity so active work
  remains visible through acquisition and processing transitions.
- Aligned AirDC++ client and queue priority controls with the shared settings
  contract.
- Kept AirDC++ cancellations active until the exact remote bundle is confirmed
  removed, and recreated externally removed bundles from durable provenance on
  retry.
- Recovered ambiguous AirDC++ queue mutations with bounded retries and prevented
  repeated socket updates from hiding distinct search results at the result cap.

### Performance

- Added the measured `library_files.issue_id` join index so bounded reading
  shelves remain fast on large libraries.
- Removed unnecessary ComicVine requests from trusted Mylar3 and metadata-backed
  folder imports, with benchmark tooling for large migration workloads.
- Moved archive conversion, metadata writing, and other blocking file work off
  the async request loop.

### Testing

- Added file-continuity, 10,000-issue query-scale, 50-switch resource,
  cross-browser, keyboard, reduced-motion, and responsive reader regressions.
- Added migration, Windows path, AirDC++, LibGen, import fast-path, cancellation,
  progress lifecycle, scheduler, and large-library benchmark coverage.

### CI / Build

- Updated pinned CodeQL and Docker Buildx actions and refreshed reviewed Docker
  Hardened Image vulnerability exceptions for current package revisions.

## [1.1.2] - 2026-08-26

Patch release that makes release notifications and browser validation more reliable.

### Fixed

- Preserve successful Discord release delivery records so later channel posts and
  workflow reruns do not create duplicate messages.
- Record Discord delivery state against the exact release commit.
- Stabilize the utilities export footer check in Firefox by targeting the
  dedicated fields value.

## [1.1.1] - 2026-08-23

Patch release improving direct-download reliability for collected issue packs.

### Fixed

- Accept safe, separately packaged contiguous direct-download issue packs when
  their declared coverage includes the requested issue; extract and import the
  explicitly selected issue plus any other wanted members without replacing
  existing files.
- Preserve direct-acquisition fallback source metadata and accept configured
  alternate series names when validating extracted pack members.

### CI / Build

- Refreshed narrow Docker Hardened Image vulnerability exceptions for the
  current Debian 13 package revisions that have no upstream fix.

## [1.1.0] - 2026-08-18

Minor release introducing native direct acquisition, an embedded comic reader,
and significant operational resilience improvements for large libraries and
long-running imports.

### Added

- Added native Jackett indexer management with Prowlarr-consistent test and
  synchronization controls, one independently managed Torznab source per
  configured tracker, and duplicate-manager warnings.
- Added direct acquisition as a first-class search and download path, with
  GetComics and Anna's Archive provider support, source-aware matching, and
  user-controlled provider priority.
- Added configured artifact-host support for PixelDrain, MEGA, MediaFire,
  TeraBox, DataNodes, Rootz, and generic HTTPS routes, including encrypted
  credentials, reachability checks, fallback routing, per-host blocklisting,
  and manual source switching.
- Added browser challenge resolvers with ranked FlareSolverr, ByParr, and TRAWL
  support for direct providers and manually configured Torznab indexers.
- Added a lightweight embedded comic reader with private reading progress,
  bounded page caching, responsive controls, and operational cache limits.
- Added direct-download recovery entries to the Intervention Queue so users can
  retry failed mirrors with a newly planned route instead of reusing a stale
  artifact.
- Added a database optimization utility and expanded health detail for direct
  acquisition routes, Jackett indexers, and database maintenance.
- Added manual refresh for stale What's New release data.

### Changed

- Preserve missing Prowlarr and Jackett tracker records as unavailable history
  instead of deleting them, and give Jackett-managed searches a proxy-aware
  timeout and request cadence while Jackett retains challenge ownership.
- Allow Anna's Archive users to edit the configured official URL while limiting
  the accepted values to the supported `.gl`, `.pk`, and `.gd` domains.
- Reworked direct-download settings into consistent provider, artifact-host,
  and challenge-resolver configuration surfaces with connection status and
  ordered preference controls.
- Extended matching for collected editions, graphic novels, omnibuses, titled
  trade volumes, one-shots, and subtitle-bearing releases while preserving the
  review-first path for uncertain matches.
- Improved download and intervention history with direct-provider and artifact
  host context, parsed release details, and clearer recovery actions.

### Fixed

- Corrected direct acquisition planning when a release exposes multiple
  artifact mirrors, including unavailable or unsafe routes, stale landing
  pages, host fallback, and source-specific retries.
- Routed one-click issue downloads through the same cross-source acquisition
  path as scheduled searches, so direct results honor source priority and work
  when no indexer result is available.
- Kept direct-review and direct-recovery entries actionable when background
  dispatch cannot start, instead of resolving them before a transfer is queued.
- Prevented direct downloads from appearing stalled during slow transfers and
  report a clear slow-source status after sustained low throughput.
- Preserved direct-download issue state as downloading throughout acquisition
  and corrected post-processing source resolution across mounted paths.
- Kept recovery-only Intervention Queue rows out of generic bulk approve and
  reject actions, and report untested enabled direct routes as needing
  attention rather than falsely healthy.
- Improved import safety-review rematching, folder naming tokens, targeted
  import type classification, and rematch review refresh behavior.
- Preserved in-place series monitoring changes, list state after deletion, and
  interactive cursor affordances across the application.
- Prevented reader worker, progress-write, cache-budget, fullscreen, and image
  validation edge cases from disrupting reading sessions.

### Performance

- Reduced sidebar navigation query work and kept manual monitoring changes
  reactive without full-page reloads.
- Improved database contention handling for concurrent imports, scheduled work,
  health checks, and direct acquisition recovery.

### Testing

- Expanded direct acquisition, reader, health, intervention, import, and
  browser workflow regression coverage.

### Documentation

### CI / Build

- Updated GitHub Actions and axe-core dependencies and recorded the reviewed
  base-image OpenSSL CVE triage.

### Internal

## [1.0.6] - 2026-07-23

Patch release focused on reliable wanted searches, user-controlled series
lifecycle status, and faster large-library management.

### Added

- Added manual continuing and ended lifecycle overrides on series details;
  metadata refreshes preserve the user-selected status.
- Added configurable Pull List page sizes, lifecycle status in the series list,
  acquisition most-to-least and least-to-most sorting, and file-explorer-style
  checkbox selection with Shift and Command/Ctrl modifiers.

### Fixed

- Made automatic and manual wanted searches recover promptly from indexer
  backoff, stream outcomes progressively, and preserve search-on-add results.
- Serialized shared database-session work during concurrent searches without
  reducing provider request concurrency.
- Cleaned completed Usenet source directories and preserved expanded download
  history details during live refreshes.
- Preserved Pull List breadcrumb context when opening and returning from series
  details.
- Prevented provider metadata refreshes from restoring an end year on series
  manually marked as continuing.

### CI / Build

- Updated Tailwind dependencies and pinned GitHub Actions releases.

## [1.0.5] - 2026-07-15

Patch release focused on production reliability under sustained background work
and large-library metadata synchronization.

### Fixed

- Hardened indexer, download-client, scheduled-search, metadata, and ComicInfo
  background workflows against remote failures, provider throttling, and SQLite
  lock contention.
- Preserved failed and rejected indexer outcomes so retry, intervention, and
  blocklist handling remain visible instead of silently losing results.
- Deferred ComicInfo enrichment safely when providers throttle requests, while
  allowing imports to complete and enrichment to resume later.
- Made series sorting deterministic across all pages and compatible with native
  PostgreSQL enum columns.
- Reconciled issue wanted/skipped states during bulk monitor changes across all
  selected results.
- Preserved the active page and selected series during metadata hydration, and
  stopped expanded search-history details from flashing during live polling.
- Prevented automatic health checks from probing disabled indexers.
- Corrected scheduled-task table styling and pointer feedback for enabled import
  rule saves.

## [1.0.4] - 2026-07-13

Maintenance release focused on reliable multi-platform publication across
Docker runtimes with classic image stores.

### CI / Build

- Evicted the digest-selected image between AMD64 and ARM64 runtime checks so
  sequential verification cannot fail with Docker's `cannot overwrite digest`
  error after manifests are published.

## [1.0.3] - 2026-07-13

Maintenance release focused on faster multi-architecture image publication and
dependency security while preserving the existing release gates.

### Fixed

- Raised the Pillow dependency floor to 12.3.0 to include upstream fixes for
  crafted font/image memory exhaustion and Windows viewer command injection.

### CI / Build

- Parallelized AMD64 and ARM64 release builds across two Docker runners while
  preserving Grype, smoke-test, dual-registry, attestation, and signing gates.

## [1.0.2] - 2026-07-12

Maintenance release focused on container runtime reliability, migration
correctness, and faster self-hosted CI without weakening release gates.

### Fixed

- Added the missing PostgreSQL enum migration for paused import jobs so
  PostgreSQL deployments upgrade cleanly.
- Made shell-free production images resilient to Python path changes between
  Docker Hardened Image builder and runtime releases.

### CI / Build

- Hardened release image validation with an Expat runtime floor, exact reviewed
  Grype exceptions, and pre-signing checks for both AMD64 and ARM64 images.
- Routed lightweight checks to a dedicated runner, increased Python test
  parallelism, sharded browser tests, and disabled routine E2E video encoding
  while retaining opt-in diagnostics.
- Updated the pinned CodeQL actions to 4.37.0.

## [1.0.1] - 2026-07-07

Patch maintenance release focused on dependency freshness and release-pipeline
hygiene after the 1.0 launch.

### CI / Build

- Updated pinned GitHub Actions used by CI, Docker validation, CodeQL branch
  probing, release image publication, and security scanning workflows.
- Updated Tailwind CSS and the Tailwind CLI to 4.3.2 and regenerated the
  compiled stylesheet.
- Added a narrow local and CI `pip-audit` ignore for the current Safety
  toolchain's transitive `nltk` advisory while no fixed upstream version is
  available.

## [1.0.0] - 2026-06-29

The first stable release. Pullbox 1.0 is a complete, self-hosted comic library
manager: pull list automation, weekly release discovery, a matching engine built
for how comics actually get named, and the operational depth to run unattended
on your server.

### Library & Collection

- Series management with eleven distinct series types, including annuals, trade
  paperbacks, omnibuses, hardcovers, one-shots, and more, so collections
  organize the way comics actually work.
- Pull list monitoring: mark a series monitored and new issues are searched,
  grabbed, and filed automatically.
- Review-first collection import: scan a folder, match against ComicVine,
  resolve conflicts and duplicates explicitly, then import with full rollback.
- Mylar3 importer: reads a Mylar3 database strictly read-only, preserves its
  ComicVine matches as high-confidence imports, and preserves external source
  folders during import.
- File-level tracking with naming templates per series type, configurable
  transfer methods, and ComicInfo.xml metadata writing.

### Acquisition

- Five download clients with full lifecycle management: SABnzbd, NZBGet,
  qBittorrent, Transmission, and Deluge.
- Indexer support: Prowlarr integration plus direct Newznab and Torznab
  connections, with health checks and per-indexer priority.
- Matching engine: a three-stage parse, match, validate pipeline developed
  against a corpus of more than 20,000 real-world release names, with an
  intervention queue for ambiguous matches instead of silent wrong grabs.
- Universal blocklist across all client types, with configurable expiry,
  wildcard release-group patterns, and per-series clearing.
- Two-pass search with configurable thresholds, size guardrails, scoring
  weights, and ignored-phrase filters.

### Weekly Release Discovery

- What's New: this week's releases, filterable by publisher.
- Coming Soon: upcoming weeks, with graceful offline fallback to cached data.

### Metadata

- ComicVine integration with your own free API key, encrypted at rest, with
  aggressive caching and internal rate limiting to stay well inside ComicVine's
  limits.

### Utilities

- Seven built-in tools, job-queue backed with live progress: File Converter,
  Mass Convert, Mass Rename, Integrity Check, Library Permissions, DB Check &
  Cleanup, and Export Library.

### Operations

- Health dashboard covering database, filesystem, ComicVine, clients, indexers,
  scheduler, and system resources, with actionable guidance on failures.
- One-click diagnostics packages with secrets redacted.
- Scheduled database backups with retention and restore.
- Audit log of security-relevant events.
- Full REST API behind the same auth as the UI, with an interactive reference
  served from the instance at `/docs`.

### Security & Runtime

- Ships on Python 3.14 using Docker Hardened Images, running as a fixed non-root
  user with a minimal attack surface.
- Encrypted credentials at rest, CSRF protection, tiered rate limiting, bcrypt
  password hashing, signed sessions, and API keys stored as hashes.
- Native HTTPS with your own certificates, or reverse-proxy friendly with
  trusted-proxy support.
- Multi-architecture images for `amd64` and `arm64` on GHCR and Docker Hub.
- Anonymous usage telemetry that is opt-in, off by default, and documented
  field-by-field on the public transparency page.

### Interface

- Light and dark themes with system-preference detection.
- Responsive layout from phone to desktop.
- Targets WCAG 2.2 AA with automated contrast, keyboard, focus, and axe
  regression checks.

## [1.0.0-rc1] - 2026-06-23

Release candidate for the v1.0 production burn-in, focused on Mylar3 migration
correctness, rollback safety, metadata recovery, download retry handling, and
large-library UI stability.

### Changed

- Adopted matching source folders during Mylar3 and folder imports when the
  source already lives inside the Pullbox library root, preserving adjacent
  Mylar artifacts instead of creating duplicate target folders.
- Surfaced catalog hydration state in series views so missing post-import
  metadata is visible while background recovery catches up.

### Fixed

- Fixed import rollback cleanup for adopted folders, renamed folders, failed
  files, empty target directories, and rollback details that were previously
  not persisted early enough for later recovery.
- Fixed a stale intervention issue link interaction that could swap an issue
  detail page into the intervention table instead of navigating normally.
- Fixed pending download retries that could remain in `retry pending` without
  being failed and blocklisted when no active downloads were polling.
- Fixed import result retry visibility for file failures that need recovery
  after review or rollback.

### Performance

- Reused catalog hydration indicators and import metadata recovery paths to
  keep large-library views responsive after production-scale imports.

### Testing

- Added coverage for folder adoption, rollback edge cases, pending download
  retry processing, intervention navigation, import result recovery, catalog
  hydration banners, and reviewed template-safe macro attributes.

## [0.9.12] - 2026-06-22

Stable release promoting the validated 0.9.11 release-candidate train and
moving Pullbox CI/CD back to a cost-controlled GitHub Actions pipeline with
self-hosted runner support.

### Added

- Added Docker production environment examples, documented runtime variables,
  and hardened-image-friendly storage guidance.
- Added restore recovery aftercare so fresh installs can recover metadata and
  background work after a database restore.
- Added persistent deferred ComicInfo enrichment and catalog hydration recovery
  after restarts.

### Changed

- Refined Library, Series, Pull List, Security, Settings, System, and import UI
  contracts based on production validation.
- Limited the Library browser to tracked catalog entries so existing external
  comic folders are not mistaken for Pullbox-managed library content.
- Clarified local-auth-bypass behavior for Docker bridge clients and local
  installs.

### Fixed

- Fixed production configuration issues for local-auth-bypass saves, runtime
  library root seeding, donation QR codes, HTTPS/settings toggles, API-key
  creation feedback, task status pills, browser title casing, and dashboard
  storage reporting.
- Fixed downloads and post-processing visibility, including Usenet finalization
  progress and downloads history empty-state clarity.
- Fixed import, rollback, and restore edge cases from Mylar3 and folder imports,
  including nested Mylar path maps, trusted Mylar issue targets, duplicate
  skip-existing imports, live import log updates, blocked-file retries, and
  restore recovery.
- Fixed pull-list monitoring toggles, series-detail monitored semantics, and
  Library action feedback after refresh-driven operations.

### Performance

- Sped up Step 2 import scanning, Step 4 file processing, import metadata
  hydration, large-library browsing, scheduled wanted-search fairness, and daily
  metadata task cadence.
- Reused import metadata cache data during hydration to avoid unnecessary
  ComicVine work.

### Testing

- Raised overall test coverage above the 90% gate.
- Added coverage for import execution, deferred ComicInfo enrichment, restore
  recovery, Mylar3 path handling, API keys, local auth bypass, library browsing,
  settings regressions, and UI shell contracts.

### Documentation

- Updated backup/restore, Docker production setup, environment variable, and
  restore-aftercare documentation.

### CI / Build

- Migrated CI, security checks, workflow hygiene, Docker validation, Docker
  release publishing, Cosign signing, and GitHub Release creation back to
  GitHub Actions.
- Added self-hosted runner support for trusted Docker work while keeping Python
  and E2E validation on GitHub-hosted runners.
- Preserved PR cost controls with lightweight preflight, label-gated full
  checks, and release-tag-only publishing.
- Kept final release tags responsible for moving the `latest` container tag
  while release-candidate tags publish only RC and SHA tags.

## [0.9.11-rc4] - 2026-06-21

Release candidate focused on release-note polish, PR gate cost control, and
Library action feedback stability before the next production validation pass.

### Fixed

- Kept Library action feedback visible after refresh-driven actions such as
  file rename operations so the UI confirms completed work consistently.
- Restored GitHub Release changelog formatting and skipped draft releases when
  generating full-changelog links.

### CI / Build

- Gated expensive full CircleCI PR checks behind the `ci:full` label while
  keeping cheap preflight checks on ordinary PR updates.
- Preserved the reduced untrusted path for Dependabot PRs even when branches
  originate from the main repository.
- Updated the GitHub Actions dependency group used by the remaining lightweight
  workflow bridge.

## [0.9.11-rc3] - 2026-06-21

Release candidate focused on production bug-bash validation, large-library
import reliability, restore readiness, CircleCI release automation, and pre-v1
confidence.

### Added

- Added Docker production environment examples, including documented runtime
  variables and hardened-image-friendly storage guidance.
- Added restore recovery aftercare so fresh installs can recover metadata and
  background work after a database restore.
- Added persistent deferred ComicInfo enrichment and catalog hydration recovery
  after restarts.

### Changed

- Refined Library, Series, Pull List, Security, Settings, System, and import UI
  contracts based on canary production testing.
- Limited the Library browser to tracked catalog entries so existing external
  comic folders are not mistaken for Pullbox-managed library content.
- Clarified local-auth-bypass behavior for Docker bridge clients and local
  installs.

### Fixed

- Fixed production configuration issues for local-auth-bypass saves, runtime
  library root seeding, donation QR codes, HTTPS/settings toggles, API-key
  creation feedback, task status pills, browser title casing, and dashboard
  storage reporting.
- Fixed downloads and post-processing visibility, including Usenet finalization
  progress and downloads history empty-state clarity.
- Fixed import and rollback edge cases from Mylar3 and folder imports, including
  nested Mylar path maps, trusted Mylar issue targets, duplicate skip-existing
  imports, live import log updates, blocked-file retries, and restore recovery.
- Fixed pull-list monitoring toggles and series-detail monitored semantics.
- Fixed release digest extraction in the CircleCI Docker release job so signing
  and GitHub Release creation receive the pushed image digest.

### Performance

- Sped up Step 2 import scanning, Step 4 file processing, import metadata
  hydration, and large-library browsing.
- Reused import metadata cache data during hydration to avoid unnecessary
  ComicVine work.
- Improved scheduled wanted-search fairness and daily metadata task cadence.

### Testing

- Raised overall test coverage above 91%.
- Added coverage for import execution, deferred ComicInfo enrichment, restore
  recovery, Mylar3 path handling, API keys, local auth bypass, library browsing,
  settings regressions, and UI shell contracts.

### Documentation

- Updated backup/restore, Docker production setup, environment variable, and
  restore-aftercare documentation.

### CI / Build

- Moved Pullbox CI, security checks, workflow hygiene, Docker validation,
  release publishing, Cosign signing, and GitHub Release creation to CircleCI.
- Added CircleCI test splitting, dependency caching, Docker Layer Caching,
  larger resource classes, and aggregate required checks for faster PR gates.
- Limited automatic CircleCI runs to open PRs and release tags so merge pushes
  do not rerun the expensive pipeline.
- Kept legacy GitHub Actions workflows as manual fallback only.
- Added release-candidate container tagging safeguards so RC tags do not update
  `latest`.
- Fixed Cosign installation in the CircleCI Docker release signing job.
- Fixed CircleCI release signing to request a Sigstore-audience OIDC token and
  verify the CircleCI pipeline-definition certificate identity.
- Fixed Docker smoke validation to avoid fixed host-port collisions.
- Added and documented the release-sync fast path for version-only
  post-release sync PRs.

## [0.9.10] - 2026-06-18

Corrective release for the `0.9.9` release workflow, which pushed registry
images but failed while validating the OCI index package description.

### CI / Build

- Docker release metadata validation now checks the full index-level package
  description that is actually published to GHCR and Docker Hub.
- Workflow contract tests now guard against release image label and index
  annotation validation drift.

## [0.9.9] - 2026-06-18

Corrective release for the `0.9.8` release workflow, which pushed registry
images but failed while validating OCI metadata on the Docker runner.

### CI / Build

- Docker release metadata validation now uses the runner-available `python3`
  executable instead of assuming a `python` shim exists on the hardened Docker
  runner.
- Workflow contract tests now guard the release metadata validation command so
  future release-only checks stay compatible with the Docker runner image.

## [0.9.8] - 2026-06-18

Release-pipeline hardening focused on making release automation predictable,
signed, and easier to debug ahead of the v1 release candidate path.

### Testing

- Expanded workflow contract coverage for runner routing, publishing triggers,
  release metadata, signing requirements, Docker validation, and CI artifacts.
- Broadened accessibility regression coverage across the app shell, modal,
  dropdown, import, search, settings, and file-browser flows.

### CI / Build

- Split Docker validation and Docker release publishing into separate
  workflows so PR validation can never publish registry images.
- Docker release publishing now runs only from version tags or explicit release
  dispatches, then signs and verifies GHCR and Docker Hub images with Cosign.
- Release images now keep SBOM/provenance attestations and include OCI
  annotations for clearer package metadata.
- GitHub Release creation now depends on the successful Docker release workflow
  and validates the signed release digest artifact before publishing notes.
- Normal `main` merges no longer rerun full CI or publish Docker images after
  required PR and merge-queue checks have already passed.
- CI now uploads coverage XML artifacts for every tested Python version, and
  coverage stays safely above the 90% gate instead of relying on rounded output.

## [0.9.7] - 2026-06-17

Testing and coverage sprint release focused on hardening Pullbox ahead of the
v1 release candidate path.

### Testing

- Overall Python coverage now clears the 90% v1 gate.
- Added broad API, provider, service, utility, and UI route branch coverage.
- Added route and runtime regression coverage for downloads, search, settings,
  health, library, series, utilities, import conflict review, Docker entrypoint,
  startup helpers, filesystem browsing, and ComicVine metadata contracts.
- Stabilized E2E setup visibility and HTMX context-swap expectations.

### CI / Build

- CI and local full-CI coverage checks now enforce the 90% coverage gate.
- CI now rebuilds Tailwind CSS through the shared newline-preserving build
  script.
- Aligned pre-commit Ruff tooling and normalized generated Tailwind output.

### Internal

- Cleaned historical pre-commit drift and secret-like test fixture values.

## [0.9.6] - 2026-06-17

Corrective patch release for signed Docker image publication after `0.9.5`
published registry images but failed during the Cosign signing step.

### CI / Build

- Docker image signing and verification now run on a GitHub-hosted runner using
  GitHub Actions OIDC, while image build and registry publication remain on the
  trusted self-hosted runner.
- Release image digest artifacts are now produced after signing succeeds so the
  GitHub Release workflow can publish digest-specific Cosign verification
  instructions.

## [0.9.5] - 2026-06-17

Corrective patch release for the unpublished `0.9.4` image publication.

### Fixed

- The release container scan now triages the current Debian 13 SQLite findings
  from the Docker Hardened Images base while no stable trixie package fix is
  available.

### CI / Build

- Release image publishing can proceed after the current Grype baseline passes
  the documented DHI base-image triage.

## [0.9.4] - 2026-06-17

Patch release for pre-sprint cleanup, dependency maintenance, and release
pipeline hardening.

### Added

- System support links now include the Pullbox documentation site.

### Fixed

- The Discord support link now points to the current invite.
- Search settings now consistently use the 750 MB issue-size warning default.
- The Starlette dependency floor now includes the current security patch level.

### Testing

- Route and utility E2E contracts now have stronger regression coverage.

### Documentation

- Development examples now use the corrected paths.

### CI / Build

- GitHub Release notes now start from the curated `CHANGELOG.md` release section
  before appending generated commit details.
- GitHub Release image verification commands now use the exact published image
  digest from the Docker workflow.
- Updated npm development dependencies: `axe-core` 4.12.1, `tailwindcss` 4.3.1,
  and `@tailwindcss/cli` 4.3.1.

### Internal

## [0.9.3] - 2026-06-10

Corrective release for Docker Hub publication.

### CI / Build

- Docker Hub release publishing now targets the `docker.io/pullbox/pullbox` namespace.
- Generated GitHub release notes now show the corrected Docker Hub pull command.

## [0.9.2] - 2026-06-10

Patch release to validate dual-registry Docker publishing after the public repository relaunch.

### CI / Build

- Docker release publishing now publishes versioned release images to both GHCR and Docker Hub.
- Untagged `main` Docker workflow runs now build, scan, and smoke-test without publishing registry images.
- Generated GitHub release notes now include both GHCR and Docker Hub pull commands.

### Documentation

- Registry and release-process documentation now reflects the dual-registry publishing contract.

## [0.9.1] - 2026-06-10

First clean public release from the relaunched `pullboxapp/pullbox` repository.

### Added

- Public-ready repository configuration, rulesets, security checks, and release automation.
- CodeQL, secret scanning, push protection, Dependabot security posture, and aggregate required checks for the public repo.

### Fixed

- Runtime environment validation now tolerates expected deployment-provided values.
- Bandit findings were resolved or narrowed with explicit, documented exceptions.
- Library preview E2E assertions now wait for rendered modal rows before checking counts.

### CI / Build

- Trusted PR Docker validation now runs successfully against the clean public repository.
- Trusted CI routing is restored to the local self-hosted runners for same-repository PRs.
