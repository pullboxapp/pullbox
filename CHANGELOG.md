# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

### Fixed

### Performance

### Testing

### Documentation

### CI / Build

### Internal

## [0.9.11-rc1] - 2026-06-21

Release candidate focused on production bug-bash validation, large-library
import reliability, restore readiness, and pre-v1 confidence.

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

- Added release-candidate container tagging safeguards so RC tags do not update
  `latest`.
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
