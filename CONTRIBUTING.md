# Contributing To Pullbox

Thanks for taking a look at Pullbox. This guide is meant to get a contributor
from a fresh checkout to a working development environment quickly, then point
to the standards that keep changes consistent.

Pullbox supports two local workflows:

- Docker dev: best for a clean one-command environment with container path
  parity.
- Local venv: best for fast Python and test iteration on a machine that already
  has Python and Node ready.

For GitHub Actions, maintainers can route ordinary trusted checks with the
repository variable `PULLBOX_CHECKS_RUNNER`. Fork and Dependabot pull requests
always use GitHub-hosted runners for ordinary checks, while trusted Docker
publish and release automation stay on the self-hosted runner.

Protected branches use stable aggregate required checks: `CI Required`,
`Security Required`, and `Workflow Hygiene Required`. Docker PR validation still
runs for Docker-sensitive changes, but it is intentionally not a required
branch check until it emits an always-present status.

## Table Of Contents

1. [Prerequisites](#1-prerequisites)
2. [Quick Start](#2-quick-start)
3. [Docker Development](#3-docker-development)
4. [Local Venv Development](#4-local-venv-development)
5. [Configuration Model](#5-configuration-model)
6. [Seed Data](#6-seed-data)
7. [Common Commands](#7-common-commands)
8. [Testing And Validation](#8-testing-and-validation)
9. [Development Workflow](#9-development-workflow)
10. [Troubleshooting](#10-troubleshooting)
11. [Pull Request Checklist](#11-pull-request-checklist)
12. [Contributor References](#12-contributor-references)

## 1. Prerequisites

Required for local venv development:

- Python 3.12 or newer.
- Node.js 20 or newer.
- Git.
- Make.

Required for Docker development:

- Docker Desktop or Docker Engine with the Docker Compose plugin.
- Make.

Useful for broader validation:

- Playwright browsers for E2E tests.
- A ComicVine API key for metadata refresh testing.
- Enough disk space for Docker images, local databases, browser artifacts, and
  test fixtures.

## 2. Quick Start

### Option A: Docker Dev

Run one command:

```bash
make dev-docker
```

That command builds a local development image, starts the app in Docker, waits
for `/ping`, and seeds starter data.

Open `http://127.0.0.1:8585`.

Default seeded login:

| Username | Password |
| --- | --- |
| `admin` | `pullbox` |

Follow logs in another terminal:

```bash
make dev-docker-logs
```

Stop the stack:

```bash
make dev-docker-down
```

### Option B: Local Venv

Run one command:

```bash
make dev-local
```

That command creates the virtual environment, installs dependencies, runs
migrations, seeds starter data, and starts the reload server.

Open `http://127.0.0.1:8585`.

Default seeded login:

| Username | Password |
| --- | --- |
| `admin` | `pullbox` |

For UI work, run the CSS watcher in a second terminal:

```bash
make css-watch
```

## 3. Docker Development

Docker dev is the easiest way to start from a fresh machine. It uses
`docker/Dockerfile.dev`, which is based on the public Python image. The
production image still uses Docker Hardened Images through `docker/Dockerfile`,
but a normal contributor does not need DHI access for local Docker dev.

The local Docker stack:

- Builds from the current checkout.
- Bind-mounts the repo at `/app`.
- Sets `PYTHONPATH=/app/src` so reload uses the bind-mounted source.
- Runs the same app through the Docker entrypoint, so migrations run before
  startup.
- Uses `/data` for app state inside the `pullbox-dev-state` Docker volume.
- Uses `media-docker-dev/comics` as the host-visible comic library mount.
- Uses `media-docker-dev/downloads` as the host-visible completed-downloads
  mount.
- Uses `media-docker-dev/imports` as the host-visible manual import/drop-folder
  mount.
- Runs on `http://127.0.0.1:8585`.

Common Docker dev commands:

```bash
make dev-docker        # Build, start, wait, and seed
make dev-docker-up     # Build and start without reseeding
make dev-docker-seed   # Seed starter data again
make dev-docker-logs   # Follow logs
make dev-docker-shell  # Open Python in the local Docker dev container
make dev-docker-down   # Stop containers
```

The seed command is idempotent. Running it again skips records that already
exist.

If port `8585` is already in use, pick another host port:

```bash
DEV_DOCKER_PORT=8586 make dev-docker
```

## 4. Local Venv Development

Local venv development is better for quick test runs and editor integration.

First-time setup:

```bash
make dev-local
```

Manual setup, step by step:

```bash
make setup
make migrate
make seed
make css-build
make run
```

Useful local commands:

```bash
make run          # Start reload server
make css-watch    # Rebuild Tailwind CSS as files change
make reset-db     # Rebuild database and seed full data
make reset-password u="admin" p="NewPass1!"  # Reset a local dev password
make clean        # Remove caches and local build artifacts
```

The local venv workflow stores runtime data under `data/`, which is ignored by
git.

## 5. Configuration Model

Pullbox has three configuration layers:

| Layer | Purpose |
| --- | --- |
| `.env` | Local bootstrap values for the process or dev server |
| `config.xml` | Persistent host secret generated under the runtime data directory |
| `system_config` | Editable app settings stored in the database |

Important notes:

- `.env` is a local convenience file and is ignored by git.
- `config.xml` stores the host secret used for signing and encryption.
- `PULLBOX_SECRET_KEY`, when set, overrides the `config.xml` secret at runtime.
- Changing the resolved host secret after credentials are saved prevents those
  secrets from decrypting.
- Settings changed in the UI usually live in `system_config`, not `.env`.
- Runtime paths, bind address, and port come from bootstrap configuration and
  are shown read-only in the app.
- Native HTTPS can be configured in Settings > General or through
  `PULLBOX_HTTPS_*` bootstrap values. Environment-managed HTTPS settings are
  shown as runtime-managed in the UI and require restart after changes.
- `.env.example` documents local bootstrap values only. Do not add CI-only,
  build-only, or one-off test variables there unless they are part of the
  contributor runtime contract.

The local venv data layout:

```text
data/
  backups/
  comics/
    .covers/
  logs/
  tmp/
  pullbox.db
  config.xml
```

The Docker dev data layout:

```text
Docker volume: pullbox-dev-state -> /data
Host folder: media-docker-dev/comics -> /comics
Host folder: media-docker-dev/downloads -> /downloads
Host folder: media-docker-dev/imports -> /imports
```

## 6. Seed Data

Pullbox has two seed levels.

Minimal seed:

```bash
make seed
```

Creates:

- Admin user.
- Sample publishers.
- Sample series.
- Sample issues.
- Disabled sample indexer and download-client configs.
- Default system configuration rows.

Full seed:

```bash
make seed-full
```

Adds realistic records for:

- Downloads.
- Blocklist entries.
- Search history.
- Intervention queue.
- Audit logs.
- Comic files on disk.
- Local path configuration.

Reset local venv state with full seed data:

```bash
make reset-db
```

Docker dev can be reseeded with:

```bash
make dev-docker-seed
```

### Development Library Cleanup

Import testing can accumulate large comic files quickly. The reusable prune
script removes imported issue files over a size threshold, keeps all series
records, resets pruned issues to `SKIPPED`, removes the associated
`library_files` rows, and empties the library `.trash` folder. It is dry-run by
default.

Run it from an environment that can see both the Pullbox database and library
paths. For the local Docker dev container:

```bash
docker exec -i pullbox-dev-live /opt/venv/bin/python \
  /app/scripts/prune_large_library_issues.py --threshold-mib 100
```

Add `--execute` after reviewing the dry-run output:

```bash
docker exec -i pullbox-dev-live /opt/venv/bin/python \
  /app/scripts/prune_large_library_issues.py --threshold-mib 100 --execute
```

When running outside the container against database paths like `/comics`, map
container paths to host paths with `--path-map`, for example:

```bash
PULLBOX_DB_URL=sqlite+aiosqlite:////path/to/pullbox.db \
  .venv/bin/python scripts/prune_large_library_issues.py \
  --threshold-mib 100 \
  --path-map /comics=/shared-drive/live-comics
```

## 7. Common Commands

Run `make help` to list every target.

Most common commands:

| Command | Purpose |
| --- | --- |
| `make dev-docker` | One-command Docker development environment |
| `make dev-local` | One-command local venv environment |
| `make setup` | Create venv and install Python plus Node dependencies |
| `make run` | Start local reload server |
| `make css-build` | Compile committed Tailwind CSS |
| `make css-watch` | Rebuild Tailwind CSS during UI work |
| `make test-unit` | Fast unit tests |
| `make test` | Non-E2E test suite |
| `make validate` | CSS, lint, format, typecheck, and tests |
| `make ci-local` | GitHub-aligned local CI without Docker smoke |
| `make ci-full` | Full local gate including security and Docker smoke |
| `make docker-smoke` | Build production image and run smoke tests |
| `make security-check` | Local security scan lane |
| `make reset-db` | Rebuild local database and seed full data |
| `make clean` | Remove local caches and generated artifacts |

## 8. Testing And Validation

Fast feedback:

```bash
make test-unit
```

Normal validation before pushing:

```bash
make validate
```

Full CI parity without Docker smoke:

```bash
make ci-local
```

Full local gate:

```bash
make ci-full
```

Test groups:

| Command | Scope |
| --- | --- |
| `make test-unit` | Fast isolated tests |
| `make test-api` | API route tests |
| `make test-integration` | Cross-module tests |
| `make test-providers` | Provider tests with mocked HTTP |
| `make test-utilities` | Utility subsystem tests |
| `make test-e2e` | Browser tests in Chromium and Firefox |
| `make test-a11y` | Contrast checks and accessibility E2E tests |

Install Playwright browsers when E2E tests complain about missing browser
binaries:

```bash
.venv/bin/playwright install chromium firefox
```

## 9. Development Workflow

Pullbox uses test-first development for behavior changes.

Recommended loop:

1. Read the owning code and nearby tests.
2. Add or update a failing test.
3. Implement the smallest behavior change that makes the test pass.
4. Run the focused test.
5. Run `make validate`.
6. Manually verify UI behavior when templates, CSS, or browser interactions
   changed.
7. Commit one logical change at a time.

Commit messages use conventional commit style:

```text
feat: add search history details panel
fix: keep blocklist action visible in manual search
ci: route trusted checks to configured runners
perf: cache ComicVine issue fetch planning
refactor: split import review query helpers
test: cover padded issue number matching
docs: refresh contributor setup guide
style: tighten series detail hero actions
chore: bump version for release prep
```

Commit prefixes feed generated GitHub Release notes. The curated root
`CHANGELOG.md` is maintained separately during release prep. See
`docs/development/GIT_WORKFLOW.md` for the full prefix mapping and release
workflow.

UI changes need a little extra care:

- Run `make css-build` before committing.
- Use `make css-watch` during active UI work.
- Check light and dark themes when visual behavior changes.
- Check keyboard and focus behavior for dialogs, dropdowns, tables, and forms.
- Keep empty states, table actions, and copy aligned with the development
  standards docs.

## 10. Troubleshooting

### `make dev-docker` cannot pull or build the image

The dev stack uses `docker/Dockerfile.dev` and should not require DHI access. If
Docker still reports auth problems, confirm the compose file is current:

```bash
docker compose -f docker/docker-compose.dev.yml config | grep Dockerfile.dev
```

If the build cache is stale:

```bash
docker compose -f docker/docker-compose.dev.yml build --no-cache
make dev-docker-up
```

### Port 8585 is already in use

Stop an existing local server or Docker stack:

```bash
make dev-docker-down
```

Find the process on macOS or Linux:

```bash
lsof -i :8585
```

Or start Docker dev on a different host port:

```bash
DEV_DOCKER_PORT=8586 make dev-docker
```

### Docker dev starts but no data appears

Run the seed target:

```bash
make dev-docker-seed
```

If the database needs a clean reset, remove the Docker volume and start again:

```bash
make dev-docker-down
docker volume rm pullbox-dev-state
make dev-docker
```

### Local venv startup reports a weak secret

Run `make setup` again if `.env` is missing. If `.env` exists, set the local
development `PULLBOX_SECRET_KEY` to a stable random-looking value with at least
32 characters. Production Docker installs normally leave `PULLBOX_SECRET_KEY`
unset and use the generated `/data/config.xml` secret instead.

### Local venv database is broken or stale

Reset it:

```bash
make reset-db
```

### Tailwind CSS drift check fails

Rebuild CSS and include the generated file in the commit:

```bash
make css-build
```

### Playwright tests fail because browsers are missing

Install the browsers:

```bash
.venv/bin/playwright install chromium firefox
```

### `npm install` or Tailwind commands fail

Remove local Node dependencies and reinstall:

```bash
rm -rf node_modules
npm install
make css-build
```

### Docker logs are needed

Follow the live Docker dev logs:

```bash
make dev-docker-logs
```

Copy logs out of the Docker state volume:

```bash
mkdir -p tmp/dev-logs
docker run --rm \
  -v pullbox-dev-state:/data \
  -v "$(pwd)/tmp/dev-logs:/out" \
  alpine sh -c 'cp -R /data/logs/. /out/'
```

### Production-image behavior needs local verification

Use the production-image test stack:

```bash
make prod-test-refresh
```

It runs on `http://127.0.0.1:18585` so it does not collide with the normal dev
server.

Useful production-image commands:

```bash
make prod-test-pull
make prod-test-up
make prod-test-logs
make prod-test-shell
make prod-test-down
```

The production image is intentionally shell-less, so do not document
`make reset-password`, `sh`, or scripts under `/app/scripts` as production
support paths. The supported hardened-image password reset command is:

```bash
docker exec pullbox python -m pullbox.cli reset-password \
  --user admin \
  --password 'NewPass1!'
```

For the local production-image test stack, replace `pullbox` with
`pullbox-prod-test`.

## 11. Pull Request Checklist

Before opening a PR:

- `make validate` passes.
- `make ci-local` passes for larger changes.
- `make ci-full` passes for Docker, dependency, runtime, or release-sensitive
  changes.
- Tests cover new behavior and edge cases.
- UI changes have manual verification notes.
- Tailwind CSS is rebuilt when templates, classes, or CSS entrypoints changed.
- Database changes include migrations and downgrade behavior.
- Security-sensitive changes include denied-path tests.
- Logs do not expose secrets, tokens, cookies, or credentials.
- Commit messages use conventional commit style.
- `CHANGELOG.md` is updated when a user-facing change should be called out in
  curated release notes.

## 12. Contributor References

Start here for standards and architecture:

- `docs/development/ARCHITECTURE_OVERVIEW.md`
- `docs/development/CODE_STANDARDS.md`
- `docs/development/DATABASE_STANDARDS.md`
- `docs/development/SECURITY_STANDARDS.md`
- `docs/development/INFRASTRUCTURE.md`
- `docs/development/LOGGING_STANDARDS.md`
- `docs/development/DESIGN_SYSTEM.md`
- `docs/development/ACCESSIBILITY_STANDARDS.md`
- `docs/development/UI_COPY_STYLE_STANDARDS.md`
- `docs/development/GIT_WORKFLOW.md`

Security reporting and deployment guidance:

- `SECURITY.md`

Project overview and quick Docker run examples:

- `README.md`
