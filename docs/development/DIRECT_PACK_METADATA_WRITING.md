# Direct Pack Metadata Writing

The development-only `PULLBOX_METADATA_PAIRED_DIRECT_WRITER_ENABLED` flag now
covers separable same-series packs as well as single-comic direct downloads.
Embedded metadata updates must also be enabled by the effective import policy.
The Import flag does not control this consumer. Production defaults remain off.

## Batch Boundary

The existing direct executor still owns acquisition state, download history and
the final database commit. Extraction, issue eligibility, naming, permission
application and registration retain their existing services. Already-owned
members are left alone unless the initiating issue explicitly requests a
replacement; non-initiating members still require Wanted or Downloading status.

Before registration, the pack stages all eligible paired outputs through the
same verified metadata assembly and archive reconciler as manual imports.
It then locks and rechecks the acquisition, selected artifact, history,
metadata, issue eligibility, effective policy and destination. Registration
publishes the prepared output without overwriting an unrelated destination.
There are no intermediate per-member commits or separate readers after the
batch starts mutating canonical state.

Both XML documents derive from one canonical metadata state. An unresolved
metadata conflict prevents every pack member from being imported and appears
as a metadata-review intervention. The downloaded pack remains available for
correction and retry; no provider call is needed to serialize the verified pair.

## Safety And Cancellation

Allow-once size approval must match the exact downloaded pack signature. It
raises only the resource bound for its members, not dangerous-path, payload,
integrity or metadata-identity checks. The configured global limit is unchanged.

Archive preparation is interruptible. A process cancellation drains an
already-started short atomic publication before cleanup. A later registration
failure or cancellation removes only the exact unchanged paired outputs owned
by this batch, rolls back registration and leaves history unimported. Existing
replacement-stash rollback still restores the previous comic. Normal successful
completion retains the executor's existing quarantine cleanup.

This is not a new durable pack publication journal. These tests do not establish
hard-crash or lost-final-commit recovery guarantees beyond the existing executor.
Combined books are not split into guessed page boundaries, and mixed-series
packs remain rejected by the existing extractor.

## Qualification

`tests/integration/metadata_identity/test_direct_pack_writer_boundary.py`
exercises the actual executor on disposable SQLite and PostgreSQL databases.
It covers independent writer flags, XML identity/number agreement, later-member
conflicts, concurrent state changes, size approval, native conversion, existing
ownership/replacement, publication failure, cancellation and retry.

Set `PULLBOX_METADATA_TEST_POSTGRES_URL` to the dedicated
`pullbox_metadata_contract_test` database to run the PostgreSQL lane. Tests must
never point at a development or production library database.
