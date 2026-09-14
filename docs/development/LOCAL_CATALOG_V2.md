# Local Comic Vine catalog v2 client

## Scope

The catalog is an optional, derived SQLite database on the **Pullbox server**,
separate from the user's library database. Settings → Metadata → Download catalog
starts the first download. Browsers display status and progress; they do not
download or unzip the database onto the browser device.

The Pullbox Data API serves only signed publications and catalog artifacts. This
feature adds no remote search or individual metadata endpoints. ComicRack/v1,
MEGA, arbitrary file imports, and user-configurable signing keys are not supported.

After installation:

- Add Series, automatic import matching, manual match search, orphan recovery,
  explicit Comic Vine ID lookup, and basic issue-list hydration use the catalog.
- A local miss does not silently fall back to online requests. The user can check
  for a catalog update; existing review and override decisions remain intact.
- Without an installed catalog, existing Comic Vine discovery behavior remains.
- Full metadata refresh and post-import ComicInfo enrichment retain their direct
  Comic Vine provider path, including its batch cache and rate controls. They
  still need the user's API key. Basic local matching does not need that key.
- Catalog installation changes no library rows or files. Imports still require
  the existing review/confirmation steps before Step 4 materializes files.
- New basic records use `metadata_source=pullbox_catalog`. Series freshness uses
  the publication's source cutoff, not download time. A completed full Comic Vine
  series refresh is not demoted or overwritten by basic catalog hydration.
  Existing live Comic Vine issue records likewise retain their identity and
  fields; basic hydration adds missing issues without reverting live metadata.

## Download and activation contract

`services/catalog/contract.py` owns the Ed25519 public verification key ring.
The release currently trusts `catalog-2026-09`, verified against the publisher's
public key and production publication. A future signing-key rotation must ship
the next public key in a Pullbox release before the publisher switches. Unknown
keys fail closed. No signing secret is distributed with Pullbox.

1. Fetch `/api/v2/catalog/latest` from `PULLBOX_DATA_API_BASE_URL` (defaults to
   `https://api.pullbox.app`), using the cached ETag when available.
2. Verify the signature over compact sorted JSON, payload SHA-256, schema,
   versions, lineage, sizes, and exact same-API download coordinates. Reverify
   the cached signed envelope after a 304. Do not follow redirects.
3. Stream the required snapshot or cumulative patch into a checksum-named
   `.part` file. Interrupted transfers resume with Range/If-Range. A server that
   returns 200 starts a fresh transfer; 206 must match the expected byte range.
4. Verify compressed byte count and SHA-256 **before decompression**. Decompress
   with a bounded window and output ceiling, checking free space as it expands.
5. Validate the SQLite application/user versions, dataset identity, source
   cutoff, row counts, logical content hash, foreign keys, integrity, and FTS
   index. Reject unsupported tables, views, or triggers.
6. Reconstruct each daily cumulative patch from a **fresh copy of its immutable
   weekly base**, never from yesterday's patched database. Apply child-first
   deletes and parent-first upserts transactionally, replace the dataset
   manifest, rebuild FTS, and verify the target logical hash.
7. Flush and atomically move the validated generation into place, then atomically
   replace `active.json`. Retain the previous reference and file. Readers open a
   read-only immutable generation for each query; long disk work is offloaded
   from the event loop. Cancellation waits for an owned disk operation to finish
   before releasing the update lock.

When a new weekly base is published, it is downloaded in full. Between weekly
bases, only the latest cumulative patch is needed. If no current patch exists,
the signed publication's full snapshot is installed. A healthy newer local
version is never downgraded by a stale API response.

Limits: manifest 1 MiB, compressed artifact 2 GiB, decompressed artifact 4 GiB,
zstd window 128 MiB; search input 256 characters / 16 words; up to 1,000 results.
Disk-space checks reserve a safety margin, and patching checks space for copying
the weekly base. The byte-progress bar applies to transfer; unpacking,
verification, and installation are separate indeterminate stages.

## Persistent storage and recovery

Under `<data_dir>/catalog/`:

```text
active.json             installed generation and source cutoff
previous.json           last installed generation before a successful update
state.json              opt-in, update preference, coarse status, timestamps
manifest.json           signed publication cache and ETag
update.lock             cross-process advisory update lock
bases/<version>.db      immutable weekly snapshots
versions/<version>.db   validated reconstructed daily generations
downloads/<sha>.part    resumable compressed transfer
staging/catalog-*.db    uncommitted work, never used for searches
```

An async lock and filesystem lock prevent simultaneous installers. Failed
downloads or patches leave the installed catalog active. Retry resumes a matching
partial transfer. Checksum failures discard the bad transfer. Retrying repairs a
missing or invalid file at the currently published version. Unknown signing keys
or formats require a client update, not bypassing verification. An unreadable
active catalog reports an actionable error rather than making live metadata
requests. Symlinked catalog storage is rejected.

Cleanup runs under the update lock: abandon unfinished staging files, remove
compressed downloads after success, and expire unreferenced generations/partials
older than two days. Keep active, previous, and both referenced weekly bases.
Only recognized catalog-owned filenames are eligible; no library paths are used.
The grace period accommodates readers that started before activation.

## Scheduling and local control API

`catalog_update` appears as **Local Catalog Update** in the normal task system.
It checks daily at 06:30 in the scheduler timezone, with up to 30 minutes of jitter.
Automatic runs do nothing until the user requests the first download or when
automatic updates are disabled. A startup check catches overdue work; a 23-hour
freshness guard allows the next day's jitter to be earlier than yesterday's.
Manual checks bypass the age guard. Startup checks use the same installer lock
and status but do not create a separate scheduler history entry.

| Local route | Access | Result |
|---|---|---|
| `GET /api/v1/catalog` | Authenticated | Safe status, version, cutoff, byte progress, timestamps, error |
| `POST /api/v1/catalog/sync` | Interactive operator + CSRF | 202 queued/already queued/already running; 503 if unavailable |
| `PATCH /api/v1/catalog/preferences` | Interactive operator + CSRF | `{ "automatic_updates": true/false }`; updated status |

These routes control this Pullbox instance; they are not new Pullbox Data API
distribution routes. Machine API keys cannot trigger downloads or change the
preference. Settings polls only while the page is mounted and live updates are
enabled; a download continues when the page is closed. Errors never include
credentials or raw response bodies.

## Verification

`tests/unit/test_catalog_*` covers signed-manifest failures, hash and lineage
checks, corrupted/unsupported SQLite artifacts, cumulative reversion, interrupted
transfers, installation failure preservation, overlap, repair, cleanup, realistic
zstd windows, search escaping, exact issue IDs and fractions, source provenance,
and live enrichment separation. `tests/ui/test_catalog_controls.py` covers
settings, no-key local search, session/CSRF and machine-key boundaries.

The external-artifact SQLite adapter is deliberately separate from the ORM
application database. Its SQL identifiers come exclusively from a closed contract
allowlist; all search terms, IDs and filters are bound parameters. Existing
application database access remains SQLAlchemy-based.
