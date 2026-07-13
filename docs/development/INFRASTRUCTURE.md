# Pullbox Infrastructure Standards

**Author:** Adam Hernandez
**Version:** 1.1
**Last Modified:** 2026-05-16

## Purpose

This document is the working infrastructure reference for Pullbox contributors.
It covers the test strategy, CI workflows, Docker image contract, deployment
expectations, dependency policy, and operational checks that keep the project
safe to build and ship.

Infrastructure should stay boring and repeatable. Local commands should match
CI where practical, release automation should be easy to reason about, and the
production container should stay minimal without hiding important runtime
requirements.

## Current Baseline Notes

- `make validate`, `make ci-local`, and `make ci-full` are the main local gates.
- GitHub Actions runs lint, format, typecheck, tests, migration checks,
  accessibility checks, E2E, security scans, Docker validation, and release
  automation.
- Pull request pushes run a cheap GitHub Actions preflight by default. Full PR
  CI/security/workflow-hygiene checks run after maintainers apply `ci:full`.
- CI tests Python 3.12, 3.13, and 3.14.
- Self-hosted Python matrix jobs use five pytest workers per Python version.
- Self-hosted functional E2E jobs use three isolated pytest workers per browser.
- Normal PR E2E runs disable video encoding; manual CI dispatches can enable
  retained failure video and tracing with the `e2e_diagnostics` input.
- Python 3.14 is the production container runtime.
- The production Docker image uses Docker Hardened Images.
- The runtime image is non-root and intentionally minimal.
- GHCR and Docker Hub are the current container registries.
- Release tags trigger Docker publication and GitHub Release generation.
- Security workflows include gitleaks, `pip-audit`, Safety, Bandit, workflow
  hygiene checks, and Grype container scanning.
- CodeQL is configured for public repositories and can be opt-in while private;
  it is informational in the required aggregate gate until explicitly promoted.

## Table of Contents

1. [Testing Strategy](#1-testing-strategy)
2. [Local Validation](#2-local-validation)
3. [CI Workflows](#3-ci-workflows)
4. [Security And Supply Chain](#4-security-and-supply-chain)
5. [Docker Image Contract](#5-docker-image-contract)
6. [Deployment Contract](#6-deployment-contract)
7. [Dependency Policy](#7-dependency-policy)
8. [Release Automation](#8-release-automation)
9. [Operational Support](#9-operational-support)
10. [Infrastructure Audit Checklist](#10-infrastructure-audit-checklist)

## 1. Testing Strategy

### 1.1 Current Pullbox implementation

Pullbox uses a layered test strategy:

| Layer | Purpose |
|---|---|
| Unit tests | Pure logic such as parsing, matching, events, and model behavior |
| Integration tests | Service, database, and cross-module behavior |
| API tests | Endpoint contracts, auth, errors, pagination, and filtering |
| Provider tests | Download clients, indexers, HTTP mapping, and failure behavior |
| Utility tests | Queue, workers, executors, file tools, and rollback behavior |
| UI tests | Template rendering and UI route contracts |
| E2E tests | Critical browser workflows with live app behavior |
| Accessibility tests | WCAG-oriented browser checks and contrast validation |

Representative test directories:

```text
tests/unit/
tests/integration/
tests/api/
tests/providers/
tests/tasks/
tests/ui/
tests/utilities/
tests/e2e/
```

### 1.2 Required standard

- New behavior starts with tests.
- Unit tests should cover pure logic before higher-level tests are added.
- Integration and API tests should cover database and route contracts.
- Provider tests should mock external services and cover failures.
- Utility tests should cover filesystem safety, rollback, and queue behavior.
- UI changes should include template or browser coverage when behavior changes.
- E2E tests should stay focused on critical workflows, not every button.
- Accessibility checks should stay separate from normal functional E2E.

### 1.3 Current repo nuances

- Tests outside `tests/unit/` are treated as slower coverage.
- E2E tests run a live app and seed through API-style setup rather than relying
  on broad seed scripts.
- Each parallel E2E worker creates its own temporary database, data directories,
  uvicorn server, browser session, and local port.
- Search and parser tests should use real fixture data when release-title edge
  cases matter.
- Coverage percentage is useful, but contract coverage matters more.

### 1.4 Audit checks

- [ ] New behavior has tests at the right layer.
- [ ] Provider and integration tests cover failure paths.
- [ ] UI behavior changes have appropriate UI or browser coverage.
- [ ] Accessibility-sensitive changes run accessibility checks.
- [ ] Real fixtures are used where realistic data matters.

## 2. Local Validation

### 2.1 Current Pullbox implementation

Common local commands:

```bash
make test
make test-unit
make test-integration
make test-api
make test-providers
make test-utilities
make test-a11y
make test-e2e
make coverage
make coverage-check
make validate
make ci-local
make ci-full
```

Key gates:

| Command | Purpose |
|---|---|
| `make validate` | CSS build, lint, format, typecheck, and non-E2E tests |
| `make ci-local` | GitHub-aligned local CI shape |
| `make ci-full` | CI-local plus security and Docker smoke validation |
| `make test-a11y` | Contrast gate plus accessibility browser checks |
| `make workflow-hygiene` | Local workflow linting |
| `make security-check` | Local security checks |

### 2.2 Required standard

- Use focused tests while developing.
- Run `make validate` before ordinary review.
- Run `make test-a11y` for accessibility-sensitive UI work.
- Run browser tests for UI behavior changes.
- Run `make ci-local` before broad or risky PRs.
- Run `make ci-full` before release, Docker, workflow, dependency, security, or
  other infrastructure-sensitive changes.
- Generated CSS must be current when Tailwind input or scan sources change.

### 2.3 Current repo nuances

- Full validation can be slow. Focused tests are still expected during
  development.
- CSS drift is a real failure and should not be hand-waved.
- E2E failures should be diagnosed from logs, traces, screenshots, and artifacts
  before guessing at a fix.

### 2.4 Audit checks

- [ ] Focused tests pass.
- [ ] `make validate` passes for ordinary changes.
- [ ] Browser and accessibility checks run for relevant UI changes.
- [ ] `make ci-full` runs for release or infrastructure-sensitive changes.
- [ ] Generated CSS is current.

## 3. CI Workflows

### 3.1 Current Pullbox implementation

GitHub Actions workflows live in `.github/workflows/`. CircleCI config is not
tracked in the active CI/CD path.

| Workflow | Trigger | Purpose |
|---|---|---|
| `CI` | Pull requests and manual dispatch | Cheap preflight by default; full lint, typecheck, tests, migration, accessibility, and E2E after `ci:full` |
| `Security` | Pull requests and manual dispatch | gitleaks, dependency audits, Bandit, CodeQL, and aggregate security gate after `ci:full` |
| `Workflow Hygiene` | Pull requests and manual dispatch | actionlint and aggregate workflow gate after `ci:full` |
| `Docker Validate` | Pull requests and manual dispatch | Trusted DHI production image validation or reduced public sanity validation |
| `Docker Release Benchmark` | Manual dispatch | GHCR-only distributed AMD64/ARM64 build timing with temporary package cleanup |
| `Docker Release` | Version tag push and manual dispatch | Release image build, Grype scan, smoke test, GHCR/Docker Hub publish, Cosign signing, and signature verification |
| `Release` | Successful `Docker Release` workflow run | GitHub Release creation with curated changelog, generated commit details, Docker pull commands, signature verification, and full changelog link |

### 3.2 Required standard

- Required CI checks must pass before merge.
- Branch rulesets should require only stable aggregate checks:
  `CI Required`, `Security Required`, `Workflow Hygiene Required`, and
  `Docker Validate Required`.
- Python tests run across the supported matrix.
- Migration checks must validate upgrade and downgrade behavior.
- Accessibility checks must stay separate from functional browser checks.
- E2E runs should upload useful artifacts on failure.
- Workflow permissions must stay explicit.
- Workflows should avoid surprising side effects on PRs from untrusted contexts.

### 3.3 Current repo nuances

- PR checks are the authoritative correctness gate. Ordinary `develop` and
  `main` merges should not rerun full CI or publish container images.
- The default PR push path intentionally runs only preflight so automated review
  feedback can land before spending full matrix, E2E, Docker, and security
  credits. Add `ci:full` when the PR is ready for the required aggregate
  checks.
- `ci:full` applies to PRs targeting both `develop` and `main`. Release tags do
  not use this gate and continue to publish automatically.
- Post-release `main` to `develop` sync PRs may use the release-sync fast path
  only when they are same-repository, version-only `feature/sync-develop-*`
  PRs that carry `origin/main` forward and bump `src/pullbox/__init__.py` from
  the released version to the next patch `-dev` version.
- Docker validation is a PR/manual GitHub Actions workflow. It never logs in to
  publish registries and never pushes images.
- Docker release benchmarking is manual-only. It builds AMD64 and ARM64 by
  digest on separate `docker` runners, validates the AMD64 image while ARM64
  continues, creates a temporary multi-platform manifest in an isolated GHCR
  package, and deletes that package after timing. It never publishes to Docker
  Hub or writes version, `latest`, or `edge` tags.
- Reuse the same Docker benchmark `cache_generation` for warm-cache comparisons.
  Change it for a cold-cache comparison. Benchmark summaries exclude cleanup
  time from the measured critical path.
- Docker publication depends on trusted refs and release tags.
- DHI credentials are required for Docker builds that pull `dhi.io` base images.
- Forked or Dependabot PRs may not have repository secrets. PR workflows should
  skip secret-dependent validation in those untrusted contexts rather than fail
  before meaningful validation can start.
- Ordinary CI, security, and workflow hygiene jobs may be routed by
  the repository variable `PULLBOX_CHECKS_RUNNER`:
  - `self-hosted` keeps trusted checks on the local runner
  - `github-hosted` moves trusted checks to `ubuntu-latest`
- Fork and Dependabot pull requests always run ordinary checks on
  `ubuntu-latest`, regardless of `PULLBOX_CHECKS_RUNNER`.
- Trusted compute-intensive test and E2E jobs use the explicit self-hosted `ci`
  runner label when `PULLBOX_CHECKS_RUNNER` is not `github-hosted`.
- Trusted accessibility, dependency-audit, static-analysis, secret-scan, and
  workflow-hygiene jobs use the explicit self-hosted `checks` runner label so
  they cannot occupy a matrix-test runner slot.
- Trusted Docker validation and Docker release jobs use the explicit
  self-hosted `docker` runner label by design. They are not governed by
  `PULLBOX_CHECKS_RUNNER`.
- Two isolated-VM runners carry the `docker` label so benchmark platform builds
  can run concurrently. Production release behavior remains unchanged until a
  benchmarked design is adopted explicitly.
- Untrusted Docker PRs run a reduced public sanity check (`Dockerfile.dev`
  build) instead of the full DHI-backed production build.
- `Docker Validate Required` is the stable aggregate check for Docker
  validation. Same-repository maintainer PRs wait for `ci:full`; untrusted and
  Dependabot PRs use the reduced public Docker sanity path.
- The public-readiness ruleset targets both `main` and `develop`, blocks branch
  deletion and non-fast-forward pushes, requires pull requests, requires
  conversation resolution, and requires the aggregate checks above.

### 3.4 Audit checks

- [ ] Aggregate required checks are green before merge.
- [ ] Branch rulesets target both `main` and `develop`.
- [ ] Python matrix still covers supported versions.
- [ ] Migration check remains active.
- [ ] E2E artifacts are available for failures.
- [ ] Secret-dependent workflow paths handle untrusted PRs safely.

## 4. Security And Supply Chain

### 4.1 Current Pullbox implementation

- GitHub Actions are pinned to full SHAs with version comments.
- Workflow and job permissions are explicit.
- `pull_request_target` is not used.
- gitleaks blocks secret leaks.
- `pip-audit` scans Python dependency vulnerabilities.
- Safety runs as an advisory scanner with artifact output.
- Bandit runs Python static security checks.
- CodeQL runs on public repositories, or while private when
  `PULLBOX_ENABLE_CODEQL=true`; `Security Required` treats CodeQL as
  informational unless `PULLBOX_REQUIRE_CODEQL=true`.
- Grype scans container images.
- Dependabot covers `pip`, GitHub Actions, Docker, and npm/Tailwind tooling.

### 4.2 Required standard

- Actions stay SHA-pinned with version comments.
- Workflow permissions stay least-privilege and explicit.
- Do not add `pull_request_target`.
- Dependency and secret scanning remain active.
- Container scanning remains active before publish.
- Security checks should not silently move from blocking to advisory.
- CodeQL should only become merge-blocking after an explicit policy change and
  backlog triage.
- CodeQL should stay on GitHub-hosted runners and must not expose self-hosted
  runners to untrusted code.
- New secrets must be documented and scoped narrowly.

### 4.3 Current repo nuances

- Workflow changes are supply-chain changes.
- Dependabot branches may need extra care when workflows require secrets.
- Scanner tuning should be specific and justified, not broad suppression.
- Before the repository is made public, scan the current tree, full Git
  history, release notes, PR/issue metadata, and refs for secrets and internal
  tool/provenance references that should not become public.

### 4.4 Audit checks

- [ ] Actions are full-SHA pinned with version comments.
- [ ] Workflow and job permissions are explicit.
- [ ] `pull_request_target` is absent.
- [ ] gitleaks, `pip-audit`, Safety, Bandit, and Grype remain configured.
- [ ] CodeQL status is accurate for the repository visibility.
- [ ] Scanner suppressions are narrow and documented.

## 5. Docker Image Contract

### 5.1 Current Pullbox implementation

The production Dockerfile is `docker/Dockerfile`.

Current image shape:

- builder image: `dhi.io/python:3.14-debian13-dev`
- runtime image: `dhi.io/python:3.14-debian13`
- runtime user: `65532:65532`
- runtime port: `8585`
- app state volume: `/data`
- library mount: `/comics`
- completed-downloads mount: `/downloads`
- manual import/drop-folder mount: `/imports`
- healthcheck: `python -m pullbox.docker_healthcheck`
- entrypoint: `python -m pullbox.docker_entrypoint`

Builder-stage helper packages include:

- `build-essential`
- `ca-certificates`
- `curl`
- `gzip`
- `poppler-utils`
- `p7zip-full`
- `tzdata`

The builder also compiles pinned official RARLAB UnRAR source for CBR
extraction and copies only the resulting `unrar` helper into the runtime image.

The runtime image copies only the required Python environment, app files, and
archive/PDF helper closures. It intentionally does not include a shell, package
manager, or `curl`.

### 5.2 Required standard

- Keep the production Dockerfile under `docker/`.
- Runtime image stays non-root.
- Runtime image stays minimal.
- Added OS packages need a runtime feature justification.
- Prefer the Python healthcheck over copying network tools into the runtime
  image.
- Startup and health behavior should keep working without a shell.
- Docker smoke tests must cover startup, migrations, and `/ping`.

### 5.3 Current repo nuances

- Archive and PDF helpers are product requirements, not random image bloat.
- DHI base image access requires Docker Hardened Images credentials.
- Local development compose files can include conveniences that do not belong in
  production image guidance.

### 5.4 Audit checks

- [ ] Production Docker changes are made in `docker/Dockerfile`.
- [ ] Runtime user remains non-root.
- [ ] Runtime image does not gain a shell, package manager, or curl by accident.
- [ ] Helper packages are justified by product behavior.
- [ ] Docker smoke tests pass.

## 6. Deployment Contract

### 6.1 Current Pullbox implementation

Recommended container mount contract:

| Path | Purpose |
|---|---|
| `/data` | Pullbox-managed state: database, config, logs, backups, temp files |
| `/comics` | Comic library |
| `/downloads` | Completed downloads shared with download clients |
| `/imports` | Manual import/drop-folder sources, including Mylar3 databases |
| `/imports/remote-drop` | Recommended folder for manual folder-import staging |

Common environment variables:

```text
PULLBOX_SECRET_KEY
PULLBOX_DB_URL
PULLBOX_SQLITE_JOURNAL_MODE
PULLBOX_PORT
PULLBOX_BASE_URL
PULLBOX_LIBRARY_ROOT
PULLBOX_COVERS_DIR
PULLBOX_COMICVINE_API_KEY
PULLBOX_LOG_LEVEL
PULLBOX_LOG_SIZE_LIMIT_MB
PULLBOX_LOG_BACKUP_COUNT
PULLBOX_HTTPS_ENABLED
PULLBOX_HTTPS_CERT_PATH
PULLBOX_HTTPS_KEY_PATH
PULLBOX_HTTPS_CERT_ROOT
PULLBOX_DATA_API_BASE_URL
TZ
```

### 6.2 Required standard

- `/data` should be a visible durable appdata bind mount on local storage so
  operators can inspect, copy, back up, and restore database backups and
  `config.xml` without Docker-volume archaeology.
- `/comics`, `/downloads`, and `/imports` should be explicit host or network
  mounts when those workflows are used.
- `/data/config.xml` stores the normal Docker application secret and must be
  durable and backed up separately from database restore-point archives.
- `PULLBOX_SECRET_KEY` is optional in production. When set, it overrides
  `config.xml` and must be stable, secret, and deployment-specific.
- Database restore points contain the SQLite database and metadata only. They
  intentionally do not include `config.xml`, comics, downloaded media, or import
  sources.
- Fresh-install restores must preserve the original `config.xml` or reuse the
  same env-managed `PULLBOX_SECRET_KEY` before restoring the database, otherwise
  encrypted credentials remain present but cannot be decrypted.
- After a database restore, Pullbox should mark post-restore recovery pending.
  The next startup runs derived-state aftercare for cover backfill, ComicVine
  issue catalog sync, and stale metadata refresh. Recovery status is exposed from
  System > Backup.
- Post-restore recovery is metadata aftercare, not filesystem repair. Operators
  should run Utilities > Database Check after restoring onto different storage,
  changing library roots, or moving library files.
- SQLite deployments should use storage that safely supports the configured
  journal mode.
- Download-client remote paths must map to files visible under `/downloads`
  inside the Pullbox container.
- Manual folder imports and Mylar3 database imports must point at paths visible
  inside the Pullbox container, usually under `/imports`. Use
  `/imports/remote-drop` as the recommended manual folder-import staging path.
- The app should expose port `8585` unless a deployment deliberately remaps it.
- Native HTTPS uses the same configured Pullbox listener port. Deployments that
  want a different external HTTPS port should use Docker port mapping or change
  `PULLBOX_PORT`.
- HTTPS certificate and key files should be mounted read-only under
  `/config/certs` or the configured `PULLBOX_HTTPS_CERT_ROOT`.
- Mounted HTTPS certificate and key files must be readable by the production
  runtime user UID/GID `65532:65532`.
- The container runtime user must be able to read mounted import paths and read
  or write the mounted library and downloads paths based on the configured
  workflow.
- Library permission management is chmod-only. It can normalize file and folder
  modes, but it cannot repair ownership, group, NAS ACL, or mount-option
  problems.

Example compose shape:

```yaml
services:
  pullbox:
    image: ghcr.io/pullboxapp/pullbox:latest
    container_name: pullbox
    restart: unless-stopped
    ports:
      - "8585:8585"
    environment:
      - PULLBOX_DB_URL=sqlite+aiosqlite:////data/pullbox.db
      - PULLBOX_SQLITE_JOURNAL_MODE=WAL
      - PULLBOX_LIBRARY_ROOT=/comics
      - PULLBOX_COVERS_DIR=/comics/.covers
      - PULLBOX_LOG_LEVEL=INFO
      # Optional public URL shown in app links and startup output.
      # - PULLBOX_BASE_URL=https://comics.example.com
      # Optional application-secret override. Leave unset unless intentionally
      # managing the secret outside /data/config.xml.
      # - PULLBOX_SECRET_KEY=${PULLBOX_SECRET_KEY}
      # Optional native HTTPS. Uses the normal Pullbox port.
      # - PULLBOX_HTTPS_ENABLED=true
      # - PULLBOX_HTTPS_CERT_PATH=/config/certs/pullbox.crt
      # - PULLBOX_HTTPS_KEY_PATH=/config/certs/pullbox.key
      # - PULLBOX_HTTPS_CERT_ROOT=/config/certs
      # Optional Pullbox Data API override. Leave public default for installs.
      # - PULLBOX_DATA_API_BASE_URL=https://api.pullbox.app
      - TZ=America/Los_Angeles
    volumes:
      # PULLBOX_DATA_PATH should point to a durable local appdata folder.
      - ${PULLBOX_DATA_PATH}:/data
      - /path/to/comics:/comics
      - /path/to/shared-downloads:/downloads
      - /path/to/imports:/imports
      # Optional native HTTPS cert/key mount.
      # - /path/to/certs:/config/certs:ro
```

### 6.3 Current repo nuances

- Some storage layers do not handle SQLite sidecar files and locking well.
  Journal mode should match the deployment reality.
- Download client paths differ by client. What matters is that Pullbox can see
  the completed file under `/downloads`.
- Import source paths differ by deployment. What matters is that Pullbox can see
  manual import folders and Mylar3 databases under an intentional mount such as
  `/imports`.
- The production image runs as UID/GID `65532:65532`. Deployments should make
  mounted paths writable by that runtime identity, by a compatible group, or by
  the storage layer's normal container mapping.
- Linux deployments that expect the host user to browse or maintain
  Pullbox-created library folders should create or reuse a host group with GID
  `65532` and add the operator account to that group before first startup.
- The production image does not consume `PUID` or `PGID` variables. Do not add a
  compose `user:` override or LinuxServer-style identity variables unless a
  deployment intentionally departs from the hardened-image contract.
- Linux hosts can grant access to existing media folders with ACLs such as
  `setfacl -m u:65532:rwx -m d:u:65532:rwx /path/to/comics`; dedicated
  Pullbox-only folders may instead be owned by `65532:65532`.
- Native HTTPS settings can be edited in Settings > General, but env vars
  (`PULLBOX_HTTPS_ENABLED`, `PULLBOX_HTTPS_CERT_PATH`,
  `PULLBOX_HTTPS_KEY_PATH`, and `PULLBOX_HTTPS_CERT_ROOT`) take precedence at
  startup when present.
- The Docker healthcheck uses HTTP by default and switches to HTTPS with local
  certificate verification disabled when native HTTPS is enabled.
- If a mounted filesystem ignores chmod or blocks mode changes, Pullbox should
  treat that as an environment capability issue and log it clearly.
- Recursive library permission utilities should be treated as maintenance tools,
  not as a replacement for correct host, Docker, or NAS setup.

### 6.4 Audit checks

- [ ] `/data` is durable and backed up.
- [ ] `/comics`, `/downloads`, and `/imports` are mounted intentionally for the
      workflows in use.
- [ ] `/data/config.xml` is backed up, or an env-managed
      `PULLBOX_SECRET_KEY` is stable and not committed.
- [ ] Fresh-install restore instructions include restoring `config.xml` before
      the database, or setting the same env-managed `PULLBOX_SECRET_KEY`.
- [ ] Download client path mapping is verified.
- [ ] Manual import and Mylar3 paths are verified inside the container, not only
      on the host.
- [ ] Mounted library and download paths are writable by the runtime user or
      storage mapping.
- [ ] Native HTTPS cert/key mounts, when used, are read-only and readable by
      UID/GID `65532:65532`.
- [ ] Native HTTPS deployments understand that the normal Pullbox port is the
      TLS listener.
- [ ] Permission capability issues are diagnosed as deployment issues, not
      hidden behind generic import or utility failures.
- [ ] `/ping` works through the deployed route.

## 7. Dependency Policy

### 7.1 Current Pullbox implementation

Production dependency categories:

| Category | Examples |
|---|---|
| Web/API | FastAPI, Uvicorn, Jinja2, python-multipart |
| Database | SQLAlchemy, Alembic, aiosqlite, optional asyncpg |
| Validation/config | Pydantic, pydantic-settings |
| HTTP | httpx |
| Auth/security | bcrypt, itsdangerous, cryptography |
| Logging/tasks | structlog, APScheduler |
| Archive/media | rarfile, py7zr, Pillow, pdf2image, tzlocal |

Development dependency categories:

| Category | Examples |
|---|---|
| Testing | pytest, pytest-asyncio, pytest-cov, pytest-httpx, pytest-xdist |
| Browser | Playwright, pytest-playwright |
| Quality | Ruff, mypy, pre-commit |
| Security | pip-audit, Safety, Bandit |
| Test data | factory-boy, type stubs |

### 7.2 Required standard

- Keep production dependencies minimal.
- Every production dependency should justify its runtime value.
- Prefer standard library tools when they are good enough.
- Use compatible-release pinning where practical.
- Avoid unnecessary dependency churn.
- Dependency updates should run the relevant validation gate.
- Security updates should be prioritized and reviewed for behavior changes.

### 7.3 Current repo nuances

- Some dev dependencies exist solely to keep scanners or typecheckers healthy.
- Docker base image updates can change both security posture and runtime
  behavior.
- npm/Tailwind updates can change generated CSS and require visual review.

### 7.4 Audit checks

- [ ] New dependencies have a clear purpose.
- [ ] Production dependencies are not added for dev-only convenience.
- [ ] Security updates include relevant validation.
- [ ] CSS/tooling updates include regenerated artifacts where needed.
- [ ] Docker base image updates run Docker validation.

## 8. Release Automation

### 8.1 Current Pullbox implementation

- Version tags trigger GitHub Actions release and Docker automation.
- GitHub Actions `Docker Release` builds, scans, smoke-tests, publishes, signs, and
  verifies release images only for version tags or explicit release dispatches.
- GitHub Actions `Docker Validate` validates Docker-sensitive PR changes without
  publishing.
- Published container images are signed with keyless Sigstore/Cosign using
  GitHub Actions OIDC after the registry push completes. `Docker Release`
  verifies GHCR and Docker Hub signatures by digest before reporting success.
- GitHub Actions creates or updates GitHub Releases for tagged commits after
  the Docker Release workflow succeeds.
- GitHub Release notes start with the curated `CHANGELOG.md` release section,
  then append generated commit details grouped by conventional commit prefixes.
- The root `CHANGELOG.md` is curated manually during release prep.
- Release tags are expected to be signed.
- GHCR and Docker Hub are the current image registries.
- GHCR may display SBOM/provenance attestation manifests as `unknown/unknown`.
  Those entries are expected supply-chain metadata, not runnable Pullbox images.

### 8.2 Required standard

- Follow `docs/development/GIT_WORKFLOW.md` for release prep.
- Release tags should be signed and verified locally before push.
- Curated `CHANGELOG.md` entries should be moved from Unreleased into the
  release section before the release PR.
- `make release-changelog-check VERSION=X.Y.Z` should pass before a release tag
  is pushed.
- Docker publication should happen only from trusted refs and should not publish
  ordinary untagged `main` merges.
- Release images should pass Grype and smoke tests before publication.
- Release images should publish SBOM/provenance attestations and pass Cosign
  digest signature verification before Docker Release succeeds.
- GHCR and Docker Hub tags should be reviewed after release.
- Unwanted tag aliases should be deleted deliberately, not ignored.

### 8.3 Current repo nuances

- Docker metadata rules may publish semver aliases in addition to exact version
  and SHA tags during release-tag builds.
- Pre-release tags can exercise the full pipeline before a stable release.
- Pre-release tags must not update `latest`; only final release tags should move
  that alias.
- If Docker publish succeeds but registry tags look wrong, treat that as release
  hygiene work before moving on.

### 8.4 Audit checks

- [ ] Release tag is signed.
- [ ] Docker Release workflow succeeds.
- [ ] GitHub Release points at the expected tag.
- [ ] GHCR and Docker Hub exact version and SHA tags exist.
- [ ] GHCR package metadata includes the Pullbox description from OCI index
      annotations.
- [ ] Multi-arch manifests include `linux/amd64`, `linux/arm64`, and
      SBOM/provenance attestation manifests.
- [ ] GHCR and Docker Hub release image signatures verify with Cosign by digest.
- [ ] Unwanted GHCR and Docker Hub aliases are reviewed and cleaned up.

## 9. Operational Support

### 9.1 Current Pullbox implementation

- Container startup is handled by `python -m pullbox.docker_entrypoint`.
- Startup output is mirrored to `/data/logs/startup.log`.
- The entrypoint creates required state directories, runs migrations, starts the
  app, and honors graceful restart behavior.
- Health checks run through `python -m pullbox.docker_healthcheck`.
- Production-safe management commands run through installed Python modules, for
  example `python -m pullbox.cli reset-password`.

### 9.2 Required standard

- Startup logs should give enough confidence that the app is running correctly.
- Health checks should not require shell or curl in the runtime image.
- Migrations should run before the app starts serving traffic.
- Operational commands should respect the `/data` state contract.
- Support guidance should avoid telling operators to edit files inside the image
  when a volume or environment variable is the right path.

Useful state-volume commands:

In-app database restore points are useful for app-level rollback, but they are
not full disaster-recovery archives. For host migration or bare-metal recovery,
back up the full `/data` appdata folder too.

Create a full `/data` state backup, including `config.xml`, logs, database, and
database restore-point archives:

```bash
tar czf pullbox-state-backup.tgz -C "$PULLBOX_DATA_PATH" .
```

Restore a full `/data` state backup into an empty appdata folder before starting
the replacement container:

```bash
mkdir -p "$PULLBOX_DATA_PATH"
tar xzf pullbox-state-backup.tgz -C "$PULLBOX_DATA_PATH"
```

After restoring only a database restore point, restart Pullbox and watch System >
Backup for post-restore recovery. Recovery rebuilds cover cache entries, syncs
ComicVine issue catalogs, and refreshes stale series metadata. It does not repair
filesystem path drift, so run Utilities > Database Check when the restored
library lives on different storage.

```bash
ls -la "$PULLBOX_DATA_PATH"
```

```bash
cp -R "$PULLBOX_DATA_PATH/logs" ./pullbox-logs
```

Reset a user's password from a production or production-like container:

```bash
printf '%s\n' 'NewPass1!' | docker exec -i pullbox \
  python -m pullbox.cli reset-password --user admin --password-stdin
```

Use the actual container name for non-default stacks, such as
`pullbox-prod-test` for the local production-image test compose file. This
command is the supported hardened-image path; it does not require `make`, a
shell, or the source-tree `scripts/` directory inside the image.

### 9.3 Current repo nuances

- Because the runtime image is shell-less, support workflows often use a helper
  container mounted to the same volume.
- Dozzle or `docker logs` are useful for runtime logs, while `/data/logs` keeps
  persisted app logs.
- `make reset-password` and `scripts/reset_password.py` are local source-tree
  development helpers. Production guidance should use `python -m pullbox.cli`.

### 9.4 Audit checks

- [ ] Startup logs remain useful.
- [ ] Healthcheck works without curl.
- [ ] Migrations run before serving traffic.
- [ ] Password reset works through `python -m pullbox.cli reset-password`.
- [ ] Support guidance works with a shell-less runtime image.
- [ ] State-volume backup and log-copy commands remain accurate.

## 10. Infrastructure Audit Checklist

Use this checklist when changing tests, CI, Docker, release automation, or
dependencies:

- [ ] Local validation command exists for the change.
- [ ] CI workflow permissions are explicit.
- [ ] GitHub Actions are full-SHA pinned with version comments.
- [ ] `pull_request_target` is absent.
- [ ] Secret-dependent workflow paths handle untrusted PRs safely.
- [ ] Python matrix still covers supported versions.
- [ ] Migration checks remain active.
- [ ] Accessibility checks remain separate from functional E2E.
- [ ] Docker image stays non-root and minimal.
- [ ] DHI credential requirements are documented where needed.
- [ ] Grype scans run before image publish.
- [ ] Docker smoke tests verify startup and `/ping`.
- [ ] Release tags are signed.
- [ ] Release image signatures verify for GHCR and Docker Hub.
- [ ] GHCR and Docker Hub tags are reviewed after publish.
- [ ] New dependencies are justified and validated.
- [ ] Operator deployment docs match the actual runtime contract.
