# Pullbox Architecture Overview

**Author:** Adam Hernandez
**Version:** 1.1
**Last Modified:** 2026-07-29

## Purpose

This document gives contributors a practical map of how Pullbox is put
together. It explains the major layers, runtime flows, integration seams, and
current repo layout without trying to restate every rule from the standards
docs.

The goal is orientation. A contributor should be able to open this document,
understand where a change belongs, then jump to the right implementation
standard before editing code.

## Current Baseline Notes

- Pullbox is a self-hosted, server-rendered FastAPI application.
- The app follows a layered monolith design with explicit boundaries between
  routes, services, providers, models, and shared runtime infrastructure.
- Python 3.14 is the primary production container runtime while project
  compatibility starts at Python 3.12.
- SQLite is the default database for self-hosted installs, with PostgreSQL
  support kept in the data layer.
- Runtime database access uses async SQLAlchemy.
- The UI is built with Jinja2, HTMX, Alpine.js, and compiled Tailwind CSS.
- Background work is scheduled in-process through the app scheduler and task
  registry.
- Provider integrations use protocol-shaped boundaries for metadata, indexers,
  and download clients.
- Composition helpers centralize shared wiring for providers, services, and
  event buses.
- Detailed implementation rules live in the focused standards documents listed
  below.

## Table Of Contents

1. [Related Standards](#1-related-standards)
2. [System Shape](#2-system-shape)
3. [Runtime Entry Points](#3-runtime-entry-points)
4. [Project Map](#4-project-map)
5. [Data Model Map](#5-data-model-map)
6. [Provider And Integration Model](#6-provider-and-integration-model)
7. [Search, Download, And Processing Flow](#7-search-download-and-processing-flow)
8. [Import And Library Ingest Flow](#8-import-and-library-ingest-flow)
9. [Background Work And Events](#9-background-work-and-events)
10. [Frontend Architecture](#10-frontend-architecture)
11. [Configuration, Security, And Logging](#11-configuration-security-and-logging)
12. [Required Standards](#12-required-standards)
13. [Current Repo Nuances](#13-current-repo-nuances)
14. [Architecture Audit Checklist](#14-architecture-audit-checklist)

## 1. Related Standards

**Current Pullbox implementation**

- `docs/development/CODE_STANDARDS.md` owns coding style, typing, error
  handling, async behavior, tests, and validation gates.
- `docs/development/DATABASE_STANDARDS.md` owns session handling, schema
  conventions, migrations, query safety, and database performance checks.
- `docs/development/SECURITY_STANDARDS.md` owns authentication, CSRF, secrets,
  security headers, dependency scanning, rate limiting, and deployment security.
- `docs/development/INFRASTRUCTURE.md` owns CI, Docker, dependency maintenance,
  release automation, and operational expectations.
- `docs/development/LOGGING_STANDARDS.md` owns structured logs, event naming,
  sensitive-data filtering, debug logging, and audit-oriented logging.
- `docs/development/DESIGN_SYSTEM.md` owns UI layout, component contracts,
  tokens, table behavior, empty states, and frontend polish rules.
- `docs/development/ACCESSIBILITY_STANDARDS.md` owns accessibility behavior and
  review checks.
- `docs/development/UI_COPY_STYLE_STANDARDS.md` owns product copy, labels,
  helper text, empty states, errors, and confirmation wording.
- `docs/development/GIT_WORKFLOW.md` owns branch, commit, PR, release, and
  signed-tag workflow.

**Required standard**

- Keep this document architectural and navigational.
- Put implementation rules in the focused standards docs.
- Update this document when a new subsystem, cross-cutting runtime seam, or
  major ownership boundary changes.
- Do not duplicate detailed endpoint lists, model fields, or configuration
  tables here unless they are needed to explain the shape of the system.

**Current repo nuances**

- Some older code paths still carry historical naming or compatibility seams.
  The standards docs describe the current preferred behavior.
- This overview is allowed to be less detailed than the code. It should stay
  accurate enough to route contributors to the right files and standards.

## 2. System Shape

**Current Pullbox implementation**

Pullbox is a layered monolith. It runs as one application process, but the code
is split into clear internal layers:

```text
Browser / API client
    |
Presentation layer
    |  FastAPI routes, UI routes, Jinja templates, HTMX fragments, static assets
    |
Application layer
    |  Services, workflow helpers, scoring, matching, import and processing logic
    |
Domain and persistence layer
    |  SQLAlchemy models, Pydantic schemas, domain events, database sessions
    |
Infrastructure layer
    |  Providers, filesystem work, archive readers, HTTP clients, scheduler
```

The monolith is intentional. Pullbox targets self-hosted installs where simple
deployment matters more than distributed-service boundaries. Internal seams are
kept explicit so code can be tested and refactored without splitting the
runtime into multiple services.

**Required standard**

- Keep presentation code thin.
- Put business behavior in services or focused helper modules.
- Keep provider-specific behavior inside provider modules.
- Keep database access behind async SQLAlchemy sessions.
- Keep cross-cutting runtime wiring in composition helpers instead of hiding it
  inside unrelated routes or tasks.

**Current repo nuances**

- Some service modules are intentionally larger because they coordinate complex
  workflows. New behavior should still be placed in the smallest responsible
  module.
- The UI and API often call the same service layer, but they do not always
  expose the same response shape because UI routes may return full pages or
  fragments.

## 3. Runtime Entry Points

**Current Pullbox implementation**

- `src/pullbox/app.py` creates the FastAPI application.
- The app lifespan configures logging, restores runtime settings, registers
  event subscribers, reconciles runtime library paths, starts scheduled work,
  mounts static files, and wires API plus UI routes.
- API routes live under `src/pullbox/api/v1/`.
- UI routes live under `src/pullbox/ui/`.
- Background tasks live under `src/pullbox/tasks/`.
- Shared runtime composition lives under `src/pullbox/composition/`.
- The production container starts the app through the Docker entrypoint and
  serves the same FastAPI application.

**Required standard**

- Startup work belongs in the application lifespan or an explicitly called
  bootstrap helper.
- Route modules should not perform heavyweight startup behavior at import time.
- Task modules may register scheduled work at import time through the task
  registry pattern.
- Runtime wiring that is shared by routes and tasks belongs in
  `src/pullbox/composition/`.

**Current repo nuances**

- `src/pullbox/app.py` remains the best first stop when debugging startup,
  middleware, static file mounting, lifecycle, or subscriber wiring.
- Several startup helpers read persisted `SystemConfig` values after the
  database is available, so environment defaults are not the only source of
  runtime behavior.

## 4. Project Map

**Current Pullbox implementation**

```text
src/pullbox/
  app.py                  Application factory and lifespan
  config.py               Settings model and environment-backed defaults
  database.py             Engine, session factory, request dependency
  logging.py              Application logging setup
  startup_messages.py     Startup banner and ready summary

  api/                    JSON API routes, dependencies, middleware
  composition/            Shared provider, service, and event wiring
  core/                   Cross-cutting runtime utilities
  models/                 SQLAlchemy ORM models
  providers/              Metadata, indexer, and download integrations
  schemas/                Pydantic request and response schemas
  services/               Business workflows and application behavior
  tasks/                  Scheduled and background jobs
  ui/                     Server-rendered routes, templates, JS, CSS
  utilities/              Library utility executors and support code
```

**Required standard**

- Put new ORM entities in `models/`.
- Put request and response validation in `schemas/`.
- Put business behavior in `services/`.
- Put external-system adapters in `providers/`.
- Put route-only HTTP concerns in `api/` or `ui/`.
- Put reusable infrastructure in `core/` only when it crosses multiple
  features.
- Put shared object construction in `composition/` when more than one runtime
  path needs the same wiring.

**Current repo nuances**

- `core/` is intentionally broad, but it is not a dumping ground. A helper
  belongs there only when it is genuinely shared infrastructure.
- Feature-specific helpers can live beside the owning service when that keeps
  behavior easier to trace.

## 5. Data Model Map

**Current Pullbox implementation**

The database model is organized around a comic library and the workflows that
keep it current.

```text
Publisher
  -> Series
       -> Issue
            -> LibraryFile

Creator
  -> IssueCreator
       -> Issue

StoryArc
  -> IssueArc
       -> Issue

LibraryRoot
  -> LibraryFile

IndexerConfig
DownloadClientConfig
SystemConfig
User
APIKey
AuditLog
SearchLog
BlocklistEntry
ImportJob
PendingMatch
MatchingSuggestion
SchedulerTaskStat
HealthCheckResult
```

The highest-traffic domain path is usually:

```text
Series -> Issue -> Search results -> Download -> Post-processing -> LibraryFile
```

**Required standard**

- Follow `docs/development/DATABASE_STANDARDS.md` for all data-layer work.
- Keep relationships explicit and bidirectional when the runtime needs both
  sides.
- Keep enum values backed by typed Python enum classes.
- Keep timestamps timezone-aware through the shared model types.
- Add migrations with model changes.
- Treat database compatibility as part of the feature, especially when behavior
  touches SQLite locking, foreign keys, or bulk updates.

**Current repo nuances**

- SQLite is the default deployment path, so concurrency assumptions should stay
  conservative.
- PostgreSQL support matters, but new features should not introduce
  PostgreSQL-only behavior without a graceful SQLite path.
- Some tables exist mainly for observability and support workflows, such as
  search logs, audit logs, health results, and scheduler stats.

## 6. Provider And Integration Model

**Current Pullbox implementation**

External integrations are split into provider families:

- Metadata providers under `src/pullbox/providers/metadata/`.
- Indexer providers under `src/pullbox/providers/indexer/`.
- Download client providers under `src/pullbox/providers/download/` and the
  session-oriented AirDC++ adapter under `src/pullbox/providers/airdcpp/`.

The active implementation includes:

- ComicVine metadata.
- Pullbox Data public release payloads for What's New and opt-in telemetry.
- Newznab, Torznab, Prowlarr, and Jackett indexers.
- SABnzbd, NZBGet, qBittorrent, Transmission, and Deluge download clients.
- Experimental AirDC++ search, queue reconciliation, and completed-file import.

Provider composition happens through `src/pullbox/composition/providers.py`.
Enabled database configuration is read, secrets are decrypted, clients are
constructed, and provider instances are registered for service use.
AirDC++ uses `src/pullbox/composition/airdcpp.py` because its authenticated REST
session and WebSocket lifecycle are supervised per exact configured client.

**Required standard**

- Keep provider quirks inside provider modules.
- Keep secret decryption at composition or clearly owned boundary points.
- Keep provider interfaces async.
- Give provider calls explicit timeout behavior.
- Return normalized provider data to services instead of leaking raw external
  payloads upward.
- Log provider failures with useful context and without secrets.

**Current repo nuances**

- Prowlarr-synced Torznab indexers are aggregated through a single Prowlarr
  search path.
- Prowlarr-synced Newznab indexers are kept as individual Newznab proxy
  endpoints because the direct proxy behavior can produce better category and
  result fidelity.
- Jackett is configured through the same test-and-sync workflow as Prowlarr,
  but each configured tracker is registered as an independent Torznab source.
  Jackett owns downstream challenge resolution; Pullbox preserves local source
  policy and retires missing manager rows instead of deleting their history.
- Download clients may be cached across task cycles when config values have not
  changed.
- Pullbox Data is not a general metadata proxy. Installed clients default to
  the public `https://api.pullbox.app` release API, while deployments may use
  `PULLBOX_DATA_API_BASE_URL` for an intentional private-network override.

## 7. Search, Download, And Processing Flow

**Current Pullbox implementation**

Search and acquisition are coordinated through several focused service modules:

- `search_service.py` is the main search orchestration facade.
- `search_targets.py` builds search targets from series and issue data.
- `search_query_helpers.py` builds query variants.
- `search_indexers.py` calls configured indexers.
- `release_parser.py` and matching helpers parse release titles.
- `release_validator.py` rejects mismatches and unsafe candidates.
- `search_scoring.py` and `search_evaluation.py` rank acceptable releases. The
  configured Search Priority orders the `usenet`, `torrent`, and `direct`
  source lanes. Within each lane, the deterministic quality score includes the
  individual indexer or direct-provider priority; lower numeric priority is
  preferred.
- `download_service.py` sends selected releases to a configured download
  client.
- `wanted_search_sweep.py` persists the complete fair-order target snapshot for
  bounded Search Wanted continuation.
- `search_source_selection.py` applies the existing deterministic scorer to
  indexer and direct-provider candidates, including fallback order.
- `direct_provider_quota.py` owns provider-generic capacity observations and
  the automatic-download reserve policy.
- Post-download and library services process completed files into the library.

The normal happy path looks like this:

```text
Wanted issue or manual search
  -> Build search target
  -> Build query variants
  -> Search enabled indexers
  -> Parse release titles
  -> Validate against series, issue, year, and type
  -> Order accepted results by source lane, then score within each lane
  -> Grab the selected result, falling through on an expected queue failure
  -> Monitor download
  -> Process completed files
  -> Match or intervene
  -> Move into library
```

**Required standard**

- Preserve explainability when changing search behavior.
- Add characterization tests before changing matching, rejection, or scoring
  behavior.
- Keep manual and automated search on shared parsing, validation, and scoring
  primitives so behavior does not drift.
- Apply source and provider priority consistently in manual-result presentation,
  automated winner selection, and acquisition fallback.
- Make mode-specific search differences explicit, covered, and visible in
  rejected-result explanations.
- Prefer targeted query and scoring changes over broad matching looseness.
- Keep rejected-result visibility useful for support and troubleshooting.

**Current repo nuances**

- Search behavior is intentionally split into smaller modules so parsing,
  validation, scoring, indexing, and orchestration can be tested separately.
- Manual search may fan out more broadly than automated wanted search.
- Search Wanted snapshots every eligible Wanted issue, processes at most 100
  per batch, and resumes the same restart-safe sweep hourly until complete.
  Never-searched issues run first, followed by least-recently searched issues;
  pending intervention rows are excluded from the snapshot.
- Direct-source quota, authentication, stale-candidate, and temporary-source
  failures do not create intervention rows. Automatic routing continues through
  the remaining candidates already accepted by the unchanged matcher. Semantic
  uncertainty and actionable resolver/host configuration still use
  intervention.
- An expected indexer queue failure also advances to the next accepted
  candidate. Database errors and unexpected programming failures still surface
  instead of being mistaken for a source failure.
- Quota-capable providers default to five reserved manual downloads. Automatic
  search stops at the configured reserve; manual grabs may consume it. Only the
  latest provider-generic capacity observation is stored, never account download
  history.
- Blocklist behavior is part of the search trust model. A blocked release
  should not keep reappearing as a normal candidate.

## 8. Import And Library Ingest Flow

**Current Pullbox implementation**

Import is a review-first workflow. The import job owns durable progress,
review state, user decisions, and final execution. The broad path is:

```text
Step 1 source selection
  -> Step 2 scan, inspect archives, parse source metadata, and match files
  -> Step 3 Reconcile for Import
  -> Step 4 place, convert, rename, and write ComicInfo.xml
  -> Step 5 results, retryable failures, and deferred unresolved work
```

Step 3 is the active decision point:

- Needs Series Match rows do not yet have a usable ComicVine series.
- Needs Issue Match rows know the series, but one or more files still need an
  issue assignment, skip decision, safety exception, or provisional issue.
- Conflict rows present competing series or file candidates for explicit user
  choice.
- Matched and In Library rows can be selected only by the user; state changes
  must not silently auto-select files for import.

Step 4 is the only place files move into the library. It may reuse Step 2/3
matched summaries, create targeted series and issue rows first, and then mark
the issue catalog as hydrating while full ComicVine issue metadata is fetched
in the background. This keeps imports responsive without pretending the catalog
is complete before hydration finishes.

**Required standard**

- Preserve matching quality before optimizing import speed.
- Treat Step 3 decisions as the authoritative import-review state.
- Keep global Unmatched as deferred post-import work, not the normal path for
  active Step 3 reconciliation.
- Do not move, convert, rename, or write ComicInfo.xml before Step 4.
- Keep safety exceptions explicit and narrow, especially archive size and
  decompression-bomb overrides.
- Keep provisional issue creation tied to an already-selected series and a
  user decision.
- Keep partial and hydrating catalog states visible in the UI until background
  hydration succeeds or fails.

**Current repo nuances**

- Targeted-first metadata is a performance optimization, not a semantic
  shortcut. Trusted direct ComicVine volume IDs may avoid search, but uncertain
  metadata still goes through normal matching and review.
- Import progress uses durable snapshots for recovery and live events for
  current-item detail. Refresh, reconnect, pause, resume, and restart behavior
  depend on that split.
- ComicInfo.xml writing merges existing XML but Pullbox-authoritative fields
  are refreshed from the matched series and issue data.
- Import and post-processing may share lower-level file placement utilities,
  but their history rows and failure outcomes remain product-distinct.

## 9. Background Work And Events

**Current Pullbox implementation**

Background jobs live under `src/pullbox/tasks/` and are registered through the
task registry. The scheduler runs recurring jobs for search, downloads,
metadata refresh, health checks, imports, backups, dashboard refresh, blocklist
cleanup, and related operational work.

The event bus is intentionally small and in-process. It supports domain side
effects such as:

- Download completed handling.
- Download failed handling.
- File matched handling.
- Series added handling.

Composition helpers make event-bus intent explicit:

- `build_domain_event_bus()` returns the shared application event bus.
- `build_scoped_event_bus()` returns an isolated bus for workflows that should
  not trigger global side effects.

**Required standard**

- Keep scheduled jobs idempotent where practical.
- Give background jobs clear session ownership.
- Keep long-running task behavior observable through logs, stats, or history
  tables.
- Use the shared event bus for domain side effects.
- Use scoped event buses only when isolation is intentional and documented by
  the caller.

**Current repo nuances**

- Some task code owns helper construction because it needs task-specific
  dependencies or session lifetimes.
- Shared composition helpers should be preferred when route and task wiring
  would otherwise drift.

## 10. Frontend Architecture

**Current Pullbox implementation**

Pullbox uses server-rendered HTML with progressive enhancement:

- Jinja2 renders full pages, partials, and reusable components.
- HTMX handles partial swaps, polling, boosted navigation, and modal content.
- Alpine.js handles small client-side state for interactive controls.
- Tailwind CSS is compiled from the repo CSS entrypoint into the static CSS
  file.

The UI stack is intentionally not a separate single-page application. Server
routes and templates keep state close to the backend behavior and make
self-hosted deployment simpler.

**Required standard**

- Follow `docs/development/DESIGN_SYSTEM.md` for visual contracts.
- Follow `docs/development/ACCESSIBILITY_STANDARDS.md` for semantic behavior,
  keyboard behavior, labels, and focus management.
- Follow `docs/development/UI_COPY_STYLE_STANDARDS.md` for labels, helper
  text, empty states, errors, and confirmations.
- Keep templates readable and route handlers thin.
- Keep JavaScript small and local to the interaction it supports.
- Keep direct navigation and HTMX history restore working for URLs that can be
  pushed into browser history.

**Current repo nuances**

- Some routes return either full pages or fragments depending on request
  context.
- `HX-Request` is not the only reliable signal for full-page versus fragment
  behavior because boosted navigation can intentionally strip or alter request
  context.
- Table, empty-state, modal, and action-column behavior should use the shared
  contracts already documented in the design system.

## 11. Configuration, Security, And Logging

**Current Pullbox implementation**

Configuration comes from environment-backed settings plus persisted
`SystemConfig` values. Runtime values such as log levels, integration settings,
feature behavior, paths, and UI options may be read from the database once the
app is running.

Bootstrap configuration also includes native HTTPS startup resolution. HTTPS
can be configured through Settings > General, but `PULLBOX_HTTPS_*`
environment values take precedence and make the corresponding UI fields
read-only. Native HTTPS uses the normal Pullbox listener port.

Security-sensitive behavior includes:

- Session authentication for the UI.
- API-key authentication for API clients.
- CSRF protection for unsafe session-authenticated requests.
- Fernet encryption for stored integration credentials.
- Sanitized logs and error responses.
- Security middleware for request handling and headers.
- Optional local-auth bypass with explicit trust-boundary checks.

Logging is structured and event-oriented. Application logs, utility logs, audit
logs, provider logs, and search diagnostics each serve different support needs.

**Required standard**

- Follow `docs/development/SECURITY_STANDARDS.md` for all auth, secrets,
  request safety, dependency security, and deployment security work.
- Follow `docs/development/LOGGING_STANDARDS.md` for log fields, event names,
  severity, and sensitive-data handling.
- Follow `docs/development/INFRASTRUCTURE.md` for CI, Docker, dependency, and
  release automation behavior.
- Keep user-controlled values out of logs unless sanitized, bounded, and useful
  for support.
- Keep runtime config changes visible enough to debug.

**Current repo nuances**

- Environment defaults can be overridden by database settings after startup.
- Environment-managed runtime values should be shown as runtime-managed in the
  UI rather than pretending they are editable database settings.
- Some debug logging behavior is intentionally temporary and expires
  automatically.
- `/ping` remains the lightweight unauthenticated health check used by
  containers and uptime monitors.

## 12. Required Standards

- Start with the owning layer before editing code.
- Add or update tests before changing behavior.
- Keep route handlers thin.
- Keep services responsible for business behavior.
- Keep provider-specific quirks out of services.
- Keep import review, file placement, and metadata hydration boundaries clear.
- Keep shared runtime wiring in composition helpers.
- Keep database work within the session and migration standards.
- Keep security-sensitive changes covered by focused tests and scanner checks.
- Keep UI changes aligned with the design, accessibility, and copy standards.
- Keep documentation updates close to architectural changes so this overview
  does not drift.

## 13. Current Repo Nuances

- Pullbox has several mature workflows that are split across many focused
  helper modules. More files does not automatically mean more architecture. The
  important question is whether ownership is clearer and behavior is easier to
  test.
- Some modules remain natural orchestration points, especially routes, search,
  import, health, and dashboard intelligence. They can stay as facades when
  they delegate real behavior to focused helpers.
- Compatibility facades can be useful when they protect callers during a
  refactor. They should not become permanent hiding places for unrelated
  behavior.
- The event bus is deliberately small. It is not a message queue and should not
  be used as a replacement for direct service calls when the caller needs an
  immediate result.
- Search correctness is more important than search optimism. Returning fewer
  trustworthy results is better than grabbing noisy false positives.
- Import correctness is more important than import speed. Performance work must
  reduce redundant provider/file work without weakening review decisions.
- The app favors self-hosted simplicity. Architecture choices should reduce
  operational burden unless there is a clear user benefit.

## 14. Architecture Audit Checklist

- The owning layer for the change is clear.
- Route changes are mostly validation, dependency handling, response shaping,
  and service calls.
- Service changes keep business behavior testable without a live external
  provider.
- Provider changes normalize external quirks before data reaches services.
- Database changes include migrations, downgrade behavior, and SQLite-friendly
  validation.
- Search changes include matching, rejection, scoring, and visibility tests.
- Import changes preserve Step 3 review semantics and Step 4 file-placement
  authority.
- Background task changes define session ownership, retry behavior, and logging.
- Event-bus changes document whether the shared bus or a scoped bus is
  intentional.
- UI changes preserve full-page and HTMX fragment behavior where both are
  supported.
- Security-sensitive changes include tests for denied and edge-case paths.
- Logs provide enough context to troubleshoot without leaking secrets.
- New architecture concepts are added to this overview only when they help
  contributors find the right code.
