# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

### Fixed

- Raised the Pillow dependency floor to 12.3.0 to include upstream fixes for
  crafted font/image memory exhaustion and Windows viewer command injection.

### Performance

### Testing

### Documentation

### CI / Build

- Parallelized AMD64 and ARM64 release builds across two Docker runners while
  preserving Grype, smoke-test, dual-registry, attestation, and signing gates.

### Internal

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
