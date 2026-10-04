# Pullbox Database Standards

**Author:** Adam Hernandez
**Version:** 1.0
**Last Modified:** 2026-05-15

## Purpose

This document is the working database reference for Pullbox contributors. It
captures how the app talks to the database today, the standards new code should
follow, and the checks that keep the data layer predictable as the project
keeps growing.

The goal is practical consistency, not ceremony. Database code should be easy
to reason about, safe under SQLite's single-writer constraints, and ready for
PostgreSQL where the code already supports it.

## Current Baseline Notes

- Pullbox uses SQLAlchemy async sessions through a shared database module.
- Request database access flows through the canonical `get_db()` dependency.
- Background jobs create their own short-lived sessions through the shared
  session factory.
- SQLite connections enable WAL mode, foreign keys, a busy timeout, and normal
  synchronous behavior.
- Database maintenance work coordinates through a gate-aware session layer so
  app traffic does not race exclusive maintenance.
- Static SQL is allowed for health checks, PRAGMAs, Alembic metadata, migration
  work, and narrow diagnostics. User-influenced business SQL is not allowed.
- Runtime model timestamps use `UTCDateTime`.
- Runtime enum columns are backed by typed Python enum classes.
- Runtime foreign keys declare explicit delete behavior.
- Bulk database work is preferred when behavior is uniform. Per-row work stays
  when domain side effects, audit events, or validation matter.
- Query-count guards exist for representative high-risk list rendering. Future
  query-count checks should stay targeted to real risk.

## Table of Contents

1. [Session Management](#1-session-management)
2. [Query Patterns](#2-query-patterns)
3. [Write Operations](#3-write-operations)
4. [Schema Conventions](#4-schema-conventions)
5. [SQLite Specifics](#5-sqlite-specifics)
6. [Migration Standards](#6-migration-standards)
7. [Connection Pooling And Performance](#7-connection-pooling-and-performance)
8. [N+1 Prevention](#8-n1-prevention)
9. [Database Audit Checklist](#9-database-audit-checklist)
10. [Primary References](#10-primary-references)

## 1. Session Management

### 1.1 Canonical Request Session Pattern

**Current Pullbox implementation**

- `src/pullbox/database.py` defines the canonical `get_db()` dependency.
- Request sessions are `AsyncSession` instances created from the shared
  `async_sessionmaker`.
- Request sessions use `GateAwareAsyncSession`.
- The request dependency commits on success and rolls back on exception.
- `expire_on_commit=False` is set on the shared session factory.
- Gate-aware sessions wait before database-touching operations such as
  `execute()`, `stream()`, `get()`, `flush()`, `commit()`, `delete()`,
  `merge()`, and `refresh()`.

**Required standard**

- Route handlers obtain database access through the shared dependency.
- Route handlers should not construct ad hoc request sessions.
- Services accept an `AsyncSession`, not a session factory.
- Request-scoped services may call `flush()`.
- Request-scoped commits belong to the dependency or another clearly owned
  lifecycle boundary.

**Current repo nuances**

- Some lifecycle, maintenance, startup, and task code owns its session boundary.
  Explicit commits are fine in those paths when the ownership is clear.
- A `commit()` call is not automatically a bug. First ask who owns the session
  and whether the transaction boundary is obvious to the caller.

**Audit checks**

- [ ] Routes use the shared request session dependency.
- [ ] Services accept `AsyncSession` directly.
- [ ] Request handlers do not manually open ad hoc sessions unless there is a
  documented exception.
- [ ] Request-scoped services avoid hidden commits.

### 1.2 Background Tasks And Concurrent Work

**Current Pullbox implementation**

- Background tasks create their own sessions with `get_session_factory()`.
- Long-running task flows are split into shorter read, work, and persist phases
  where practical.
- Completed-download processing uses short-lived per-item or phased sessions so
  SQLite write locks are not held across slow work.

**Required standard**

- Never share one `AsyncSession` across concurrent tasks.
- Every concurrent unit of work gets its own session.
- Task sessions stay scoped around database work only.
- Provider HTTP calls, filesystem scans, archive extraction, and image work
  should happen outside open write transactions whenever practical.
- Subscriber and task workflows should prefer this shape:
  1. load a small snapshot in a short session
  2. perform network, filesystem, or archive work outside the session
  3. persist results in a short follow-up session

**Current repo nuances**

- Some workflows still use per-row domain services because they need side
  effects, audit events, or state transitions.
- Do not convert those flows to bulk SQL unless behavior is proven equivalent.

**Audit checks**

- [ ] No task shares one `AsyncSession` across concurrent coroutines.
- [ ] Long-running tasks release database sessions before slow HTTP or file
  work.
- [ ] Background task transactions stay intentionally short.
- [ ] Subscriber side effects do not hold sessions open across network or file
  downloads.

### 1.3 Async-Only Access

**Current Pullbox implementation**

- Runtime database access is async.
- Guard coverage exists to catch accidental synchronous SQLAlchemy `Session`
  imports in runtime code.

**Required standard**

- Runtime code uses `AsyncSession`.
- New synchronous `Session` usage is not allowed in application runtime code.
- Every `session.execute()` call must be awaited.

**Current repo nuances**

- Alembic migration internals and standalone tooling are separate from runtime
  request and task code. Review those with their execution context in mind.

**Audit checks**

- [ ] Runtime code uses `AsyncSession`, not synchronous `Session`.
- [ ] All `session.execute()` calls are awaited.
- [ ] No new sync-only database helper APIs are introduced in app code.

## 2. Query Patterns

### 2.1 ORM/Core First, Raw SQL By Exception Only

**Current Pullbox implementation**

- Pullbox is ORM/Core-first.
- Static `text()` and driver SQL exist for narrow cases:
  - `SELECT 1` health probes
  - SQLite version checks
  - Alembic version checks
  - SQLite PRAGMAs
  - metadata diagnostics
  - reviewed migration data movement
- Static guard coverage rejects SQL f-strings passed to application
  `execute()` or SQLAlchemy `text()`.

**Required standard**

- Request-driven application queries use SQLAlchemy ORM/Core.
- Static SQL text is allowed only when it has no user input and serves a narrow
  liveness, metadata, PRAGMA, diagnostic, or migration purpose.
- User input must never be interpolated into SQL strings.
- Dynamic business queries should be built with ORM/Core expressions.

**Current repo nuances**

- "No raw SQL" in older docs should be read as "no ad hoc or user-influenced
  business SQL." It does not ban the static diagnostics Pullbox uses safely.
- Migration SQL is acceptable only inside migration files or tightly scoped
  migration helpers. It must not become request-driven application query code.

**Audit checks**

- [ ] No user input reaches raw SQL text.
- [ ] `text()` usage remains narrow, static, and locally auditable.
- [ ] There is no ad hoc SQL string formatting for business queries.

### 2.2 Eager Loading Strategy

**Current Pullbox implementation**

- Pullbox uses both `joinedload()` and `selectinload()` in service queries.
- Relationship-level `lazy="joined"` is reserved for reviewed, high-value
  exceptions.
- Blocklist and search-history display paths use query-level loading or
  denormalized fields rather than broad model-level joined defaults.

**Required standard**

- Prefer query-level eager loading so data access is visible at the call site.
- Use `selectinload()` for collections and one-to-many relationships.
- Use `joinedload()` for scalar relationships where row multiplication is
  controlled.
- Avoid implicit async lazy-loading surprises and `MissingGreenlet` failures by
  loading needed relationships up front.
- Templates and response serialization should not discover they need database
  access after the query phase is over.

**Current repo nuances**

- Relationship-level eager defaults are not banned, but they need a clear reason.
- The cost of eager defaults should be judged against real query shapes, not
  style preference.

**Audit checks**

- [ ] Relationship access patterns are explicit and intentional.
- [ ] Collection-heavy pages use `selectinload()` where appropriate.
- [ ] Scalar relationship loads use `joinedload()` only where row multiplication
  is acceptable.
- [ ] Existing relationship-level eager defaults are reviewed and justified.

### 2.3 Pagination

**Current Pullbox implementation**

- `src/pullbox/schemas/pagination.py` defines the shared pagination envelope:
  `items`, `total`, `limit`, `offset`, and `has_more`.
- List endpoints are expected to stay bounded.

**Required standard**

- List endpoints must use bounded queries.
- Use `limit` and `offset`, or a page abstraction that compiles to bounded SQL.
- Count queries should avoid unnecessary `ORDER BY`.
- New endpoints must not expose full-table scans by default.

**Current repo nuances**

- Database-layer pagination should match the existing API envelope rather than
  invent a second shape.
- UI-only tables still need bounded data access behind the scenes.

**Audit checks**

- [ ] List queries are bounded.
- [ ] Pagination metadata matches the current API envelope.
- [ ] Count queries are efficient and do not retain unnecessary `ORDER BY`.

### 2.4 Filtering And Search

**Current Pullbox implementation**

- User filters and search terms are expected to flow through SQLAlchemy
  expressions.
- Search terms may be used in expressions like `ilike()` without turning them
  into raw SQL strings.

**Required standard**

- User-supplied filters and search terms remain parameterized through ORM/Core.
- Dynamic sort fields must come from an explicit allowlist.
- Client input must not select arbitrary model attributes, SQL fragments, or
  column names.

**Current repo nuances**

- Search correctness has its own domain logic. Do not loosen database filters to
  compensate for parser or matcher behavior.

**Audit checks**

- [ ] User filters are ORM/Core parameterized.
- [ ] Sort field selection, if dynamic, is allowlisted.
- [ ] There are no unbounded export-style endpoints hidden behind weak defaults.

## 3. Write Operations

### 3.1 Flush Vs Commit

**Current Pullbox implementation**

- Pullbox uses `flush()` to obtain generated IDs before transaction completion.
- Request commits are usually handled by the request dependency.
- Background tasks, startup code, shutdown code, scheduler code, utilities, and
  maintenance paths may commit explicitly because they own the session lifecycle.

**Required standard**

- Use `flush()` when generated identifiers are needed before commit.
- Use `flush()` when constraint failures should surface before the end of a
  larger unit of work.
- Do not call `commit()` from request-scoped service methods unless the service
  explicitly owns the whole session lifecycle.
- Task code may commit, but only at deliberate transaction boundaries.

**Current repo nuances**

- Commit ownership matters more than the raw presence of `commit()`.
- Changing commit boundaries can change behavior, especially around side
  effects, task retries, and audit entries.

**Audit checks**

- [ ] Request services use `flush()` appropriately.
- [ ] Request services avoid hidden commits.
- [ ] Explicit commits have clear lifecycle ownership.

### 3.2 Transaction Scope

**Current Pullbox implementation**

- Several task flows already avoid holding database sessions open across slow
  provider, filesystem, and archive work.
- Some import, search, utility, and post-processing paths still deserve extra
  attention when they are touched.

**Required standard**

- Keep write transactions short.
- Do not hold write transactions open across slow provider HTTP calls,
  filesystem scans, archive extraction, or image processing.
- For large jobs, batch work and commit incrementally when correctness allows
  it.
- Retry paths must not leave transactions open accidentally.

**Current repo nuances**

- SQLite's single-writer behavior makes transaction duration one of the most
  important performance and reliability constraints in the app.
- The safest refactor shape is usually snapshot, work outside the transaction,
  then persist.

**Audit checks**

- [ ] Write transactions do not span slow network or file operations
  unnecessarily.
- [ ] Bulk jobs batch work where safe.
- [ ] Retry paths close, roll back, or finish transactions predictably.

### 3.3 Bulk Operations

**Current Pullbox implementation**

- Series bulk monitoring updates use one set-based update.
- Side-effectful deletes, imports, downloads, and task loops often remain
  per-row because domain behavior matters.

**Required standard**

- Prefer set-based updates and deletes when every affected row gets the same
  behavior.
- Keep per-row loops when they are needed for event emission, audit logging,
  domain transitions, validation, or external side effects.
- Do not replace domain services with bulk SQL unless the lost behavior is
  explicitly accounted for.

**Current repo nuances**

- Bulk SQL is a tool, not a badge. Use it when the behavior is truly uniform.

**Audit checks**

- [ ] Bulk updates are used where behavior is uniform.
- [ ] Per-row loops are justified when retained.
- [ ] Bulk refactors preserve audit and domain side effects.

## 4. Schema Conventions

### Metadata Identity Review

Canonical series, issue, and Story Arc provider identities retain append-only
evidence separately from active ownership. Automatic writers use the shared
attachment service; non-verifying observations cannot acquire ownership or clear
a rejected/conflicted decision. Stale and conflicted owners retain uniqueness.

The interactive `/api/v1/metadata-identities/{kind}/{local_id}` endpoints list
bounded saved claims, preview one saved event, and confirm/reject that event.
Writes require operator authentication, shared CSRF protection, and a current
review fingerprint/revision. The server supplies the actor; clients cannot
supply a provider identity or actor ID in a decision body. A replay receipt
proves historical completion, not current ownership. Replacing an assignment
requires explicit rejection first, and dependent issue identities must be
reviewed before detaching a series. Files and archive metadata are not changed
by identity review.

Review, observation, and attachment share parent lock ordering and caller-owned
transactions. Compatibility ComicVine columns and retained history change
atomically with ownership. Do not bypass these services with ad hoc ID updates.

### Metadata Source Configuration

`metadata_source_configs` stores one revision-checked policy per executable source,
using the normal integer identity and timestamp mixins plus a unique source slug.
It has no cascading relationship to provider identities. Reads do not create rows;
the migration seeds default ComicVine local/API policies and disabled other sources.
The canonical ComicVine credential remains in its existing configuration key.
Other API tokens use encrypted columns, never the ordinary settings JSON or read DTOs.

Services leave transaction ownership to callers. Settings writes use revision
checks and row locks (plus a SQLite write transaction before the savepoint).
Health checks perform no provider I/O inside a database transaction. Their short
result write applies only to the tested revision and cannot replace a newer probe.
Cached health is diagnostic, not proof that a source is safe or available now.

### Metadata Account Admission

`metadata_source_accounts` keeps at most one current credential-scope record per
remote source, separate from series retries, response caches and library metadata.
It stores a one-way account scope key, bounded status, UTC retry/probe deadlines
and a positive revision, not credentials or remote error payloads. Source policy
ordering changes cannot bypass a known account failure. Local catalogs have no
account row. Downgrade removes transport admission state only.

The account guard is a transport lifecycle boundary with engine-bound sessions
of its own, like the source response cache. It never commits the calling library
session. Admission commits before provider I/O; outcomes use conditional updates
against the captured credential scope and revision. An expired cooldown admits
one 45-second recovery probe, acquired after local request capacity is available.
Cancellation releases the probe while retaining the prior failure; a crashed
process recovers through lease expiry. Reads fail closed if admission storage
cannot be checked, instead of repeatedly submitting known-failing requests.

An explicit Test connection may lease an authentication-held account while its
retry deadline remains null. Only success clears that hold; cancellation retains
it. Account recovery and matching credential/configuration-scoped entity retry
deadlines commit atomically, with revision increments invalidating stale work.
Scheduler wakeups happen after commit. Downgrading the authentication-probe
migration clears active authentication leases and advances their revisions without
discarding the hold, library or entity retries. Operator read DTOs expose status,
deadlines and bounded task counts, never account/configuration keys or credentials.

### Canonical Metadata Baselines

Series, issue and Story Arc metadata baselines use separate real foreign keys,
one row per local target, positive revisions and bounded versioned snapshot JSON.
They are retained provenance, not a provider-response cache. Entity deletion
cascades the corresponding baseline; source configuration deletion does not.
Migration does not backfill provider ownership of existing descriptive values.

Baseline writes run through the shared service in caller-owned transactions.
They lock arcs, series parents, then issues in ID order, verify current exact
ownership and compare the expected baseline revision before updating. Batched
creation avoids per-issue reads. SQLite obtains its write transaction before the
savepoint; PostgreSQL uses row locks. A stale batch rolls back even when its caller
catches the conflict. Future refresh writers must additionally revalidate their
captured source-policy and ownership revisions and current user values before
applying network results; the baseline service is not a substitute for that check.

Source-bound Story Arc commands save their canonical arc baseline inside the
existing revision-claimed catalog savepoint. Newly seeded parent/issue baselines
share that transaction; pre-existing rows are not retrospectively attributed to
the incoming source. Issue baseline writes are batched at 200. Refresh reloads
current mapped fields after claiming the arc, retaining user changes and clears.
Corrupt provenance aborts the entire refresh, including membership additions.
Source-bound multi-source refresh captures an immutable read set before provider
I/O and releases its read transaction. It locks source policies before the arc,
then compares mapped fields, baseline revision/payload, identity claims/history,
arc revision/lifecycle/monitoring and managed-placement presence before saving.
A stale read aborts the complete membership and metadata write, including when a
caller catches the domain error. Secondary sources never acquire membership or
identity ownership through descriptive enrichment.

Fresh cached public release context may fill missing series publisher/start-year/
volume and existing issue store dates during normal source-aware series refresh.
The series must already have a verified LOCG link. Issue dates reuse the existing
exact provider-ID or unique number/date release resolver, in 200-release batches;
ambiguous variants, unknown IDs and unsupported date proofs cannot fill a gap.
Passive issue provenance retains a verified native issue identity, normalized
number, observed store date and distinct many-to-one release IDs. Baseline saves
also verify the issue's actual parent still owns the confirmed LOCG series link.
Cache facts and issue proofs are rechecked under the existing refresh locks after
provider I/O. This adds no issue identities, provider requests or local issues.
User edits/clears, archive disagreement and stronger values remain protected;
store dates never become cover dates. Refresh itself writes no archive or sidecar.

Issue snapshots can retain up to 128 descriptive creator credits. Each credit
has a bounded name and normalized role list compatible with the existing
Creator/IssueCreator columns. Relations and baseline values commit together;
provider absence does not clear local credits. Refresh includes current creator
names/roles in its read set to detect edits, clears and concurrent changes.
Credit reads and writes use 200-issue batches, not per-issue queries. Writers
retain existing linked creator IDs when possible and never rename a shared
creator or claim another provider's creator ID from a name match. The legacy
ComicVine writer likewise resolves known IDs exactly; name-only reuse is limited
to unclaimed descriptive rows. No credit schema migration or provenance backfill
is needed for this additive snapshot field.

Canonical field origins may retain the fixed embedded document names ComicInfo.xml
and MetronInfo.xml. This local provenance cannot also claim a provider source,
provider freshness, derivation or explicit user override. Older version-one
snapshot JSON without the field remains valid. Archive-derived empty credits are
not provider-enrichment gaps; subsequent edits and clears become user overrides.
Observed archive identities stay unverified and cannot pass the baseline writer's
ownership check as verified identities. Archive review/preservation diagnostics
persist across refreshes until a new archive comparison updates them. The caller
still owns independent file/target matching and atomic application to entity rows;
baseline persistence alone neither establishes that match nor permits file writes.

### Archive Publication Intent

`archive_metadata_publications` stores versioned, bounded evidence before a paired
archive replacement. A unique operation ID supports exact replay; active file and
canonical-path reservations reject concurrent attempts. Nullable `SET NULL` file
links preserve the immutable intent when the library row is deleted; the path
reservation and evidence survive. There is no age-based lease takeover.

Services use caller-owned transactions and the shared SQLite write/savepoint
boundary. Recording new intent first acquires the shared file-mutation admission
mutex, before any owner/entity locks. Import-owned publication locks its job, imported file and action;
publication then locks source policies, series, issue, root, file, then the
journal row. Generic filesystem classification only locks the journal row;
import-owner recovery uses the full owner/binding lock order. Large file hashing, rendering
and staging stay outside write transactions; only the short, offloaded atomic
publication and directory-sync boundary retains the lock. An intent must commit
before publication. A failed post-rename transaction leaves evidence for later
inspection instead of authorizing blind replacement. Published records remain
reserved pending coherent canonical/file-state finalization. Downgrade refuses
to discard any intended, published or review record; only abandoned-only history
may be removed. No existing archive is backfilled or mutated by the migration.

Finalization uses the same policy/parent/issue/root/file/journal lock order and
requires a published receipt, independently inspected output and an unchanged
database binding. Canonical entity values, descriptive credits, baselines and
LibraryFile size/timestamp/hash commit together with the finalized marker and
reservation release. Unchanged snapshots do not advance their baselines. File
checks run off-thread before and after DB writes; finalization does not rewrite
archives. Naming/source ownership evidence and catalog lifecycle timestamps are
not repurposed. A completed replay reports historical completion without
reapplying values or claiming present ownership. Finalized receipts survive file
deletion and block downgrade to the old state contract. Stale, interrupted or
failed finalization retains the published reservation; caught errors still roll
back the nested write. Production workflow rollback and restart integration remain
required before existing writers switch to this boundary.

An optional versioned-plan import owner binds deferred enrichment to its exact
completed import record and managed placement action. Bounded digests retain the
original action and pending-work snapshot; legacy journal JSON without an owner
remains readable. Owner cancellation, rollback, deletion or changed evidence
cannot authorize record, publication or finalization. Completed replay remains
historical even after the owner is deleted.

Finalization stores the successor publication reference on the import action and
acknowledges deferred enrichment in the same transaction as canonical/file state.
It never replaces the original destination signature or source/naming evidence.
Import rollback rejects active publications, unverified successor pointers and
reassigned registrations. A successor must be finalized for that exact unchanged
action, and its verified fingerprint/digest must still match the actual archive.
The original source remains subject to the existing rollback policy.

A stopped owner may receive a `settled` receipt for a proven published successor.
It updates only registered file accounting and the import's successor reference,
marking enrichment cancelled without applying canonical snapshots or baselines.
The immutable original placement proof and exact current registration must still
match. Settled receipts release reservations and survive rollback; downgrade
refuses to discard them. A proven untouched intent can instead be abandoned.
Changed or unproven files retain reservations and cannot fall through to legacy
enrichment. Neither recovery path writes an archive.

The recovery task owns a clean session and commits bounded keyset reads before
inspection/hashing, then commits each recovered receipt independently. It rejects
pending caller edits and nested transactions. Startup/enrichment and rollback
orchestration invoke it under the existing process-local import lane. Shared
mutation fencing across all other writers and paired enrichment-worker activation
remain required; the process-local lane is not a cross-process filesystem lock.

### Short File Mutation Coordination

`services/library_mutation_coordination.py` coordinates Library browser renames
and series-folder renames with new archive-publication admission. SQLite acquires
its writer transaction with a no-op ORM update; PostgreSQL uses the fixed
transaction-scoped advisory key pair `0x50554C4C / 0x46494C45`. Acquire this mutex
before entity locks and retain it through the caller's commit or rollback. No
provider calls, archive rewriting or hashing may run under it. Existing bulk
series-folder rename retains its caller-owned transaction; it is not a concurrent
or background archive-conversion lane.

Under the mutex, renames inspect bounded pages of active publication evidence,
including source and private stage paths and directory descendants. Orphaned
reservations survive deleted LibraryFile rows. Invalid or inconsistent plans fail
closed. A terminal released publication does not block later renames. Publication
work between commits remains protected by its durable reservation, not by a
long-running advisory lock. This is cooperative application coordination, not a
lock against external programs or a durable rename-recovery journal.

Library browser renames join file work, commit and compensation on cancellation.
Compensation reacquires admission, rechecks reservations/reference protection and
moves back only the owned inode into an unclaimed destination. A completed commit
is not undone when the request is cancelled. Conversion, deletion, bulk utility
writers, import placement and other mutation paths still need the shared contract
before paired background writing can be enabled.

### Library Conversion Journal

`library_conversions` retains bounded original, staged output and independent trash
backup evidence. Preparation uses interruptible child processes outside database
write transactions. The Library browser owns the session lifecycle: it commits
intent before exclusive publication, then commits the existing LibraryFile's new
path before removing the unchanged original. Reader state and issue identity keep
their existing registration. Copying the backup into private staging on the trash
filesystem supports cross-filesystem trash without a long SQLite write lock.

Conversion reservations share admission with archive metadata publication and
short renames. They cover original, output, backup and both stage paths, survive
LibraryFile deletion, and do not expire. Startup recovery examines bounded batches,
hashes outside write transactions, and rechecks the files under admission before
registration or cleanup. It never republishes private stages. Proven untouched
intent is abandoned; proven output/backup can finish registration and cleanup;
changed or unproven evidence remains reserved for review. A lost commit response
never permits deleting a registered output. Downgrade refuses retained completed
or unresolved conversion evidence. Bulk utilities and other mutation entry points
still require coordinated lifecycles before paired background writers activate.

### Library Removal Journal

`library_removals` retains bounded immutable source, root, private-stage and
optional trash evidence independently of LibraryFile/series lifetimes. Admission
reserves overlapping paths against archive publication, conversion and rename.
The owner commits intent before a short exclusive staging rename, then commits
the journal's detached state together with its selected database removals.
Startup restores only uncommitted staging; it never resumes destructive cleanup.

An explicit delete/trash disposition is required for cleanup. Historical plans
default to retain-only. `cleanup_json` retains bounded copy/publication/deletion
receipts; downgrade refuses to discard them. Cleanup owns clean-session
transactions and holds a non-expiring OS file lock across its lifecycle, not a
database write transaction across slow work. Same-filesystem trash uses exclusive
rename; cross-filesystem trash copies privately, verifies content, and publishes
without replacing an existing destination before removing its private source.
Cancellation joins workers and leaves retryable proof. Unknown replacements are
preserved. The old public source path is never used for cleanup.

`workspace_cleaned` records proven terminal staging-directory cleanup, separately
from payload disposition and retained cleanup evidence. Its composite index serves
bounded startup recovery; completed markers avoid repeated filesystem scans.
Upgrade defaults existing records to unverified without touching files. Downgrade
drops only the marker/index and preserves all authorization and cleanup receipts.
Only inactive complete/abandoned records may be pruned. Descriptor-relative empty
directory removal and lock checks run under short admission; payloads, replacements
and unknown contents are never recursively removed by terminal housekeeping.

These are owner-integration primitives, not yet the public delete implementation.
Series/browser/bulk deletion must adopt the complete lifecycle before paired
background writing is enabled. POSIX file locking is implemented;
other platforms currently fail closed at private cleanup admission.

Trash maintenance now owns an idle session and commits each short, identity-checked
unlink separately. The startup and utility route callers finish configuration
reads before entering that lifecycle; the service rejects an existing transaction
instead of committing unrelated changes. Shared admission protects recovery
dependencies until deletion finishes, including cancellation. Traversal and
bounded directory-page construction occur outside transactions. The indexed
`library_removals.trash_path_key` is a derived literal-path lookup, not identity
authority: the bounded original plan and cleanup receipt are revalidated before
using their publication time to protect a whole trash tree. Upgrade backfills this
index in bounded batches without filesystem access; downgrade discards only the
derived key, never the retained plan/receipt. Existing legacy trash files keep
mtime-based retention until their writers adopt journaled ownership.

### Source Catalog Checkpoints

`series_catalog_checkpoints` records complete issue-catalog progress separately
for each series/source pair. Real cascading foreign keys bind it to the series,
source configuration and active identity row. The saved source revision, identity
revision and exact external ID must still match before a cursor is usable. A
local-source checkpoint also retains the catalog generation; incremental readers
must compare that generation before applying a partial window.

Full Add/refresh writes persist the checkpoint in the same caller-owned
transaction as catalog rows and canonical baselines. A captured checkpoint
revision participates in refresh revalidation. Checkpoint timestamps record
the live request start, never completion or an unvalidated cache hit. Explicit
full revalidation bypasses response-cache coalescing for that catalog so a
joined earlier fetch cannot be mistaken for a new modification boundary.
Migration leaves existing global timestamps untouched and creates no synthetic
provider checkpoints. Downgrade discards sync progress only.

### 4.1 Base Model Mixins

**Current Pullbox implementation**

- `src/pullbox/models/base.py` defines:
  - `IdentityMixin`
  - `TimestampMixin`
  - `UTCDateTime`

**Required standard**

- New ordinary tables use the shared identity and timestamp mixins unless there
  is a specific reason not to.
- Junction tables may use composite keys instead of `IdentityMixin`.
- New timestamp columns use `UTCDateTime`, not raw `DateTime`.

**Current repo nuances**

- Older examples that show raw `DateTime(timezone=True)` are stale for runtime
  models.
- Migration history can contain older timestamp forms. That does not make them
  the current model standard.

**Audit checks**

- [ ] Ordinary new models use shared identity and timestamp conventions.
- [ ] Junction models document intentional composite-key choices.
- [ ] New runtime timestamp columns use `UTCDateTime`.

### 4.2 Naming Conventions

**Current Pullbox implementation**

- Runtime model and table names generally follow simple snake-case conventions.

**Required standard**

- Table names use `snake_case`.
- Column names use `snake_case`.
- Foreign key columns use `{related_model}_id`.
- Python enum members use clear member names with lowercase string values.
- Explicit constraints and indexes should have predictable, descriptive names.

**Current repo nuances**

- Do not rename established database objects just for neatness. Renames need a
  migration, compatibility review, and a real benefit.

**Audit checks**

- [ ] New tables and columns follow `snake_case`.
- [ ] FK column names are predictable.
- [ ] Explicit index and constraint names are readable.

### 4.3 UTC DateTime Handling

**Current Pullbox implementation**

- `UTCDateTime` normalizes timezone-aware datetimes to UTC on write.
- `UTCDateTime` re-tags naive SQLite reads as UTC on read.
- Logging timestamps are UTC.
- Scheduler timestamps use UTC.
- Guard coverage catches bare `datetime.now()` in runtime source.

**Required standard**

- Persisted backend timestamps are UTC.
- Prefer `datetime.now(UTC)` over naive timestamps.
- Do not introduce `datetime.utcnow()`.
- Do not persist local-time values.
- Runtime enum columns use `SQLAlchemyEnum(SomePythonEnum)`, not anonymous
  string lists.

**Current repo nuances**

- SQLite does not preserve timezone information the way PostgreSQL can. The
  project standard handles that at the type layer so application code does not
  have to remember it every time.

**Audit checks**

- [ ] New database datetime columns use `UTCDateTime` unless explicitly
  justified.
- [ ] Runtime code uses `datetime.now(UTC)`.
- [ ] No new local-time persistence paths are introduced.
- [ ] Runtime enum columns are backed by Python enum classes.

### 4.4 Foreign Keys, Relationships, And Indexes

**Current Pullbox implementation**

- SQLite runtime connections enable foreign-key enforcement.
- Runtime ORM foreign keys declare explicit delete behavior.
- Parent-owned collections use ORM `delete-orphan` where the parent owns child
  lifecycle.
- Nullable reference and context links use `ON DELETE SET NULL`.

**Required standard**

- Foreign keys must be real database constraints.
- Foreign keys declare explicit delete behavior.
- `ON DELETE SET NULL` is only valid on nullable FK columns.
- Relationships should exist for important graph traversal.
- Frequently filtered or sorted columns should be indexed when measurement or
  query shape supports it.
- Composite indexes should match real filter order and selectivity.

**Current repo nuances**

- Broad FK index expansion is intentionally evidence-driven. Add indexes for
  measured or obvious query needs, not just because a column is an FK.
- Duplicate and overlapping indexes should be cleaned up carefully because every
  index has write cost.

**Audit checks**

- [ ] FK constraints declare explicit `ondelete` behavior.
- [ ] `SET NULL` FK columns are nullable.
- [ ] FK columns and common query columns are indexed where warranted.
- [ ] Duplicate or redundant indexes are identified.
- [ ] Composite index ordering matches actual query patterns.

## 5. SQLite Specifics

### 5.1 Current SQLite Settings

**Current Pullbox implementation**

`src/pullbox/database.py` sets these SQLite PRAGMAs on connect:

- `PRAGMA journal_mode=WAL`
- `PRAGMA foreign_keys=ON`
- `PRAGMA busy_timeout=15000`
- `PRAGMA synchronous=NORMAL`

Pullbox also includes sidecar recovery logic for stale or corrupt `-wal` and
`-shm` files in a narrow failure mode.

**Required standard**

- WAL remains the default SQLite journal mode.
- Configured SQLite journal mode values must be allowlisted before PRAGMA use.
- Accepted journal modes are `WAL` and `DELETE`; invalid values fall back to
  `WAL`.
- Foreign-key enforcement must remain enabled explicitly.
- Busy timeout must remain configured and documented.
- SQLite recovery behavior must fail safely and log clearly.

**Current repo nuances**

- `busy_timeout=15000` is the runtime standard for normal app connections.
- Some focused maintenance or diagnostic paths may use a different timeout when
  the reason is local and explicit.

**Audit checks**

- [ ] SQLite PRAGMAs are set on every SQLite connection.
- [ ] Journal mode configuration is allowlisted.
- [ ] Sidecar recovery behavior is documented accurately.

### 5.2 Maintenance Coordination

Database-size health thresholds allow for large collections and retained logs:
the size sub-check reports the current file size as informational data. Database
health is determined by integrity, representative latency, bloat, and available
storage rather than a fixed size threshold. The file-size observation does not
impose a storage limit or an automatic cleanup policy. Disk-space, integrity, query
latency, and database-bloat checks remain independent; a smaller database does
not suppress failures in those checks.

**Current Pullbox implementation**

- Database maintenance windows coordinate app traffic through the shared
  maintenance gate.
- Gate-aware sessions pause new transactions while existing transactions have
  up to five seconds to finish. If they cannot drain, maintenance yields with
  a retryable busy result rather than holding the gate indefinitely.
- Manual backup, restore, and optimization requests release their own request
  transaction before entering maintenance. Cancellation does not reopen the
  gate until an already-running filesystem or SQLite worker has stopped.
- An exclusive nightly task runs SQLite `REINDEX` and
  `PRAGMA optimize=0x10002` at 04:30, then verifies the database with
  `PRAGMA quick_check`. The all-tables mask is intentional because the
  maintenance connection has no prior query history.
- Full SQLite `VACUUM` compaction remains an explicit operator action because
  it rewrites the database and can require substantial temporary disk space.
- Routine integrity health probes use a separate read-only SQLite connection
  with a five-second execution budget. An interrupted or busy probe reports
  incomplete verification, not confirmed database corruption.
- Search-log retention deletes at most 500 rows per transaction and releases
  the writer between batches.

**Required standard**

- Maintenance work must continue to coordinate database access through the
  shared maintenance gate.
- Direct task and lifecycle sessions should use the shared gate-aware factory
  unless they are explicitly standalone tooling.
- Maintenance code should avoid racing active application sessions.

**Current repo nuances**

- For long-lived SQLite deployments, prefer `PRAGMA optimize` during maintenance
  or connection-lifecycle boundaries instead of broad manual `ANALYZE` routines
  on request paths.
- PostgreSQL and in-memory SQLite deployments skip the SQLite file-maintenance
  task.

**Audit checks**

- [ ] Maintenance work uses the shared gate when app traffic may be active.
- [ ] Direct sessions are gate-aware unless intentionally standalone.
- [ ] Maintenance routines avoid request-path heavyweight analysis.

### 5.3 Single-Writer Reality

**Current Pullbox implementation**

- Pullbox is designed around SQLite as a common self-hosted default.
- WAL improves read/write concurrency, but it does not remove SQLite's single
  writer constraint.

**Required standard**

- Keep write transactions short.
- Avoid holding write locks while doing network, filesystem, archive, or image
  work.
- Treat database-locked paths as reliability bugs, not just performance noise.

**Current repo nuances**

- Search, import, post-processing, and utility workflows should keep moving
  toward snapshot, work, persist phases when touched.

**Audit checks**

- [ ] Long write transactions are identified and shortened.
- [ ] Database-locked contention paths are handled predictably.
- [ ] Slow task workflows avoid unnecessary write locks.

## 6. Migration Standards

### 6.1 Alembic Rules

**Current Pullbox implementation**

- Alembic has a single migration head.
- Current migrations define both `upgrade()` and `downgrade()`.
- Local and CI validation include migration hygiene checks.

**Required standard**

- Every migration defines both `upgrade()` and `downgrade()`.
- Alembic should have one head unless a deliberate branch strategy is documented.
- Autogeneration is a starting point, not a substitute for review.
- Migration files are immutable after merge.
- Prefer one logical schema change per migration.
- Separate data migrations from schema migrations when the change is non-trivial.

**Current repo nuances**

- Migration history can preserve old implementation details. Current runtime
  model standards apply to new model code, not necessarily every historical
  migration line.

**Audit checks**

- [ ] Alembic has one head.
- [ ] Upgrade and downgrade both work.
- [ ] Migration files are reviewed, not blindly accepted from autogenerate.
- [ ] Manual migration SQL is minimal and justified.

### 6.2 Migration Workflow

**Current Pullbox implementation**

- Migration checks are part of the normal validation path.

**Required standard**

1. Change SQLAlchemy models.
2. Generate the migration.
3. Review generated operations manually.
4. Upgrade locally.
5. Downgrade locally.
6. Upgrade again.
7. Run broader validation.

**Current repo nuances**

- A downgrade does not need to preserve data for destructive changes unless the
  migration explicitly promises it. It does need to restore schema shape
  predictably.

**Audit checks**

- [ ] Upgrade, downgrade, and upgrade-again paths work in a disposable database.
- [ ] Data-loss behavior is explicit for destructive migrations.
- [ ] Migration validation remains active in local or CI workflows.

## 7. Connection Pooling And Performance

### 7.1 Current Engine Configuration

**Current Pullbox implementation**

- Common engine settings include `pool_pre_ping=True`.
- PostgreSQL and asyncpg add:
  - `pool_size=5`
  - `max_overflow=10`
  - `pool_timeout=30`
  - `pool_recycle=3600`
- In-memory SQLite drops `pool_pre_ping`.
- File-based SQLite does not set PostgreSQL-style pool sizing knobs.

**Required standard**

- Do not pretend SQLite and PostgreSQL should share the same pool-tuning rules.
- PostgreSQL pool settings may be tuned for real load, but changes must be
  backed by measurement.
- SQLite tuning should focus on transaction duration, lock contention, and
  query shape rather than larger pools.

**Current repo nuances**

- Pullbox is still primarily optimized for a self-hosted SQLite deployment while
  keeping PostgreSQL support practical.

**Audit checks**

- [ ] PostgreSQL pool guidance matches current engine settings.
- [ ] SQLite guidance focuses on contention and transaction duration.
- [ ] Pool changes are backed by evidence.

### 7.2 Query Performance

**Current Pullbox implementation**

- Targeted query-count guards cover representative relationship-heavy paths.
- Some broad performance work remains intentionally evidence-driven.

**Required standard**

- Measure before tuning.
- Slow query investigation order:
  1. bound the query
  2. inspect eager loading
  3. inspect indexes
  4. inspect projection size
  5. inspect transaction scope
- On PostgreSQL, use `EXPLAIN` or `EXPLAIN ANALYZE` when evaluating real query
  slowness.

**Current repo nuances**

- Guessing at indexes can make writes slower without fixing the real read path.
- Query-count tests should focus on screens and endpoints where a regression
  would matter.

**Audit checks**

- [ ] No unbounded high-cardinality list queries remain.
- [ ] Slow pages are analyzed with query shape and index coverage.
- [ ] PostgreSQL query-plan changes are validated with `EXPLAIN` evidence when
  relevant.

## 8. N+1 Prevention

### 8.1 Standard

**Current Pullbox implementation**

- Pullbox uses query-level eager loading on relationship-heavy paths.
- Some targeted query-count guards protect high-risk UI rendering paths.

**Required standard**

- Relationship-heavy UI and API queries must explicitly load the graph they
  need.
- Templates should not trigger unexpected relationship access on unloaded async
  ORM objects.
- New list and detail endpoints should be reviewed for query counts as part of
  implementation.

**Current repo nuances**

- `selectinload()` is usually the best default for collections.
- `joinedload()` is usually best for bounded scalar relationships.
- Multiple joined relationships can multiply rows quickly, so check the query
  shape before assuming it is faster.

**Audit checks**

- [ ] High-traffic list and detail endpoints are reviewed for N+1 behavior.
- [ ] Template render paths do not rely on async lazy loading.
- [ ] Existing relationship defaults are measured, not assumed.

## 9. Database Audit Checklist

Use this checklist when touching database-facing code. It is not a release
ceremony. It is a quick way to avoid the common footguns.

### Sessions

- [ ] Request-scoped sessions use the canonical dependency.
- [ ] Background tasks use their own short-lived sessions.
- [ ] No `AsyncSession` is shared across concurrent tasks.
- [ ] Explicit commits have clear lifecycle ownership.

### Queries

- [ ] No user-driven raw SQL exists.
- [ ] Static `text()` usage is narrow and justified.
- [ ] Pagination remains bounded and aligned to the existing API envelope.
- [ ] Sort and filter inputs are allowlisted or ORM-parameterized.

### Writes

- [ ] Request services use `flush()` appropriately and avoid hidden commits.
- [ ] Background write transactions stay short.
- [ ] Bulk operations are used where semantics allow.
- [ ] Per-row loops are retained when side effects or audit behavior require it.

### Schema

- [ ] UTC datetime handling is consistent.
- [ ] Runtime enum columns use typed Python enums.
- [ ] Foreign-key delete behavior is explicit.
- [ ] Index changes are tied to real query shapes.

### SQLite

- [ ] WAL, foreign keys, busy timeout, and synchronous PRAGMAs are active.
- [ ] Long transactions and lock contention paths are identified.
- [ ] Sidecar recovery behavior is documented accurately.
- [ ] Maintenance work uses the shared gate where app traffic may be active.

### Migrations

- [ ] Alembic has one head.
- [ ] Upgrade and downgrade both work.
- [ ] Migration files are reviewed, not blindly accepted from autogenerate.
- [ ] Data-loss behavior is explicit for destructive migrations.

### Performance

- [ ] PostgreSQL pool guidance matches current engine settings.
- [ ] SQLite guidance focuses on contention and transaction duration.
- [ ] N+1 risks are reviewed for high-traffic routes.
- [ ] Query-plan and FK-index work remains evidence-driven.

## 10. Primary References

- SQLAlchemy asyncio docs: <https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html>
- SQLAlchemy relationship loading docs: <https://docs.sqlalchemy.org/en/20/orm/queryguide/relationships.html>
- SQLite WAL docs: <https://sqlite.org/wal.html>
- SQLite PRAGMA reference: <https://sqlite.org/pragma.html>
- SQLite ANALYZE / PRAGMA optimize guidance: <https://sqlite.org/lang_analyze.html>
- PostgreSQL EXPLAIN guide: <https://www.postgresql.org/docs/current/using-explain.html>
