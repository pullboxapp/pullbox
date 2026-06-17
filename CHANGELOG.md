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

- GitHub Release notes now start from the curated `CHANGELOG.md` release section
  before appending generated commit details.
- GitHub Release image verification commands now use the exact published image
  digest from the Docker workflow.

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
