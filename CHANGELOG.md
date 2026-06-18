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

## [0.9.8] - 2026-06-18

Corrective release for the `0.9.7` publish attempt, which pushed container
images but did not create a GitHub Release because Docker Hub signature
verification could not discover the freshly uploaded signature.

### CI / Build

- Release image signing now signs GHCR and Docker Hub image references
  separately.
- Release image verification now retries digest-signature checks so Docker Hub
  has time to expose newly uploaded Cosign signatures before the workflow fails.

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
