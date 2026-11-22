# Prepare a SQLite library for PostgreSQL

Start with the read-only source and destination checks below. A disposable
rehearsal or a retained **offline staging copy** is a separate, explicit action.
None of these commands switches your running library.

## Check the source

1. On the SQLite instance, open **System > Backup** and choose **Backup now**.
   Use the resulting ZIP, not the live `pullbox.db`. Backup now includes committed
   changes even when SQLite is using a write-ahead log.
2. Run the command below with the actual backup path visible inside your
   container. Replace `pullbox` if your container has a different name.

   ```sh
   docker exec pullbox python -m pullbox.cli migration-preflight --backup /data/backups/pullbox_backup_YYYYMMDD_HHMMSS.zip
   ```

3. If a check blocks, follow the message in Pullbox, take a fresh backup, and
   rerun the command. Do not delete SQLite journals or mark recovery records
   complete by hand. For an older schema, upgrade a separately restored copy
   first; keep the original backup intact.

The report checks the supported schema revision and layout, SQLite integrity,
foreign keys, and outstanding work such as an unfinished import rollback or
file write. Saved search results that never started a download are counted as
data, not active work. Add `--json` for row counts by table and blocker codes;
the report does not include comic titles or credentials. Exit status is `0`
when source checks pass, `1` when blocked, and `2` for invalid command syntax.

The ZIP needs temporary space for its uncompressed database plus 256 MiB.
For a database larger than 32 GiB, use a standalone SQLite snapshot produced
by a consistent backup operation. A raw copy of a running database is not a
substitute, even if it appears readable.

## Check the destination

Before initializing the destination, check a **new, dedicated PostgreSQL database**.
Use an ordinary login that owns the database and can create objects in its
default `public` schema. Do not use a superuser, a maintenance database, or
an existing Pullbox installation. Do not start Pullbox against the target.

Run this in the source Pullbox container. At the hidden prompt, enter the
**target** connection URL, not your running instance's database URL:

```sh
docker exec -it pullbox python -m pullbox.cli migration-check-target --url-stdin
```

The URL format is `postgresql+asyncpg://USER:PASSWORD@HOST:5432/DATABASE?ssl=verify-full`.
Percent-encode special characters in credentials. For a remote connection the
server certificate must be trusted and match the hostname. Only loopback permits
a connection without verified TLS. If you run the CLI outside Docker, install
Pullbox with its `postgres` extra. For scripted use, redirect a protected
one-line URL file into `docker exec -i` and add `--json`; keep the URL out of
shell arguments, logs and support reports.

The checker runs a read-only transaction, reports the server major and explains
blockers. It refuses populated databases (including unrelated objects), ambiguous
ownership, read-only databases, and targets with other connected sessions or
pending prepared transactions. Use a fresh target rather than clearing a database
that fails inspection. The same exit codes as the source check apply. Target
inspection and application schema initialization have been rehearsed on
PostgreSQL 16 and 18, including upgrades from a populated test database.
On both versions, the packaged application passed first-run setup, sign-in,
and opening the dashboard, Library, and Story Arcs. Populated rehearsals also
verified that only pending matching suggestions count toward review and that
retry-pending download history is preserved. These checks do not transfer a
SQLite library or qualify the full application for cutover.

**Neither pass is permission to cut over.** The checks do not reserve the
target, stop live writers, initialize a schema, transfer rows, or verify a copy.
Keep backups of your configuration and encryption secret, covers, and comic
files separately; the Pullbox ZIP contains the database and its manifest only.

## Rehearse a copy

From a checkout of this version of Pullbox, on a trusted local Docker host with
Python and Pullbox's `postgres` dependencies installed, run:

```sh
docker pull postgres:16-bookworm
python scripts/rehearse_postgres_transfer.py --backup /path/to/pullbox_backup.zip --postgres-major 16
```

For PostgreSQL 18, use `postgres:18-bookworm` and `--postgres-major 18`.
The command only uses an already-local image. Do **not** mount the Docker
socket into a running Pullbox container. Use a verified backup, never a live
SQLite file. The external GCD catalog and comic files are not inputs.

The rehearsal checks the backup before starting a temporary database. It has
no destination URL option and does not change your running installation. It
copies all application columns, compares row counts and ordered content
digests, validates foreign keys, and tests generated IDs, including retired
Story Arc IDs. Its temporary transaction is rolled back and the container is
removed. The JSON report identifies tables and blockers, not comic titles,
credentials, or individual row contents.

Exit code **0** means the database rows passed that trial; **1** means the
rehearsal was blocked. An outstanding file operation must be resolved in Pullbox,
not cleared from a backup. For an incompatible value, keep the backup intact
and report the table/column blocker. The trial uses up to 4 GiB of container
memory, with a 3 GiB temporary database and a 32 MiB SQLite record limit. A
larger library can exceed those limits without being corrupt. If the host
process is forcibly killed, inspect `docker ps -a --filter label=pullbox.offline-transfer=disposable`
and remove only its leftover trial container.

**A successful rehearsal is not a migration.** It does not check the production
encryption key, hash physical comics, stop writers, retain a PostgreSQL copy,
or enable cutover. The report deliberately leaves `cutover_ready` false.

## Keep an offline staging copy

Use this when you need to retain the verified database rows instead of discarding
them after the rehearsal. Use a backup and a **new, empty PostgreSQL 16 or 18
database created only for this stage**. Never select a running Pullbox database.
Allow enough database storage for the rows, indexes and PostgreSQL transaction log.
This may temporarily take more space than the SQLite backup.

From the same checkout and Python environment as the rehearsal:

```sh
python scripts/stage_postgres_transfer.py --backup /path/to/pullbox_backup.zip --create-offline-stage
```

At the hidden prompt enter the dedicated target URL described above. For
unattended use, pass `--url-stdin` and redirect a protected one-line file; do
not put the URL in the command line. Use a direct PostgreSQL connection, not
a transaction pooler. The non-admin database owner must also have permission
to connect to the `postgres` maintenance database on that same server. The
tool uses that second connection to close **only the selected staging database**
to new logins; it does not initialize or copy rows into `postgres`.

Before schema initialization the tool rechecks that no other client or data
has claimed the target. An occupied or already labelled database is not a
fresh target. Once reserved, the target stays closed even if the tool fails
or is killed. The process never edits Pullbox configuration or comic files.

Exit code **0** and `rows_verified: true` mean that all application tables
were copied and checked, and the staged copy was committed. Save the JSON
report; `snapshot_sha256` identifies the uncompressed SQLite snapshot. An
interruption can leave rows present **without** a successful verification:
do not infer success by counting rows. From a separate connection to
`postgres`, you can check the stage without opening it (example for psql):

```sql
\prompt 'Staging database name: ' stage_db
SELECT datallowconn AS accepts_connections,
       shobj_description(oid, 'pg_database') AS stage_status
FROM pg_database WHERE datname = :'stage_db';
```

Both `pullbox:offline-stage:incomplete` and `pullbox:offline-stage:rows-verified`
must show `accepts_connections = f`. On an incomplete stage, keep the source,
correct the reported blocker and use a **fresh** empty target. The command
does not resume, reopen or delete the old stage. Have the database owner
confirm its identity before retiring it. Do not enable connections manually.
Even a successful stage is **not ready for normal startup**.

## Review the copied library

To inspect the database as part of staging, add **`--review-library`** when
creating a new stage. Use a private interactive terminal. For example:

```sh
python scripts/stage_postgres_transfer.py --backup /backups/pullbox.zip \
  --create-offline-stage --review-library > stage-report.json
```

First enter the dedicated PostgreSQL **owner URL** at the hidden prompt. Once
the rows have been copied, verified and committed, enter an existing **Pullbox
username and password from the backup**. These prompts are separate; a database
password does not sign you into Pullbox. The review checks an active account
without creating a browser session, changing its last-login time, or enabling
new database connections. No scheduler, provider requests or file recovery run.

The private terminal shows series, issue numbers and recorded ownership status,
Story Arc chapters in reading order, and the saved metadata search order,
including per-domain priority overrides. For imports in
Follow-up it shows the job and its series/file status counts. These counts
include completed records in that job; they are **not** all unresolved files.
Sections show at most 25 entries per page. Use **next**, **back**, or enter a page
number. Enter **confirm** only if the records look right. Enter **quit** if they
do not. The review never asks you to test a provider or write to a comic.

Only the JSON report goes to standard output; the terminal can display private
titles, so do not record or share its transcript. A completed review reports
`maintenance_review.status: confirmed` with counts, not titles or credentials.
Three failed login attempts, an interrupted review, invalid saved provider
settings, or a database read error block confirmation. An empty section means
there are no entries on that page. Ten minutes idle at a prompt can expire the
read-only database transaction.

The review uses the staging tool's last open connection; it cannot reconnect to
an already closed stage. If you need to repeat it, keep the backup and use a
**new, empty target**. Quitting or failing the review does not undo a successful
row transfer. Do not unlock the old stage, change an account, or clear import
history to pass. Check the key and comic mounts separately below. Even after a
confirmed review, `cutover_ready` remains false.

## Check the key and comic files

Use `migration-check-assets` from a Pullbox version that includes it. Supply
the **same backup** and the `snapshot_sha256` from a successful stage report.
This read-only command checks the backup whose rows matched PostgreSQL; it
does not connect to PostgreSQL or reopen the stage. You can correct a mount or
configuration and retry without repeating the transfer.

1. With the original configuration and comic mounts available, record the
   reference fingerprint. Example for library root ID 1:

   ```sh
   python -m pullbox.cli migration-check-assets --backup /backups/pullbox.zip \
     --snapshot-sha256 "$STAGED_SNAPSHOT_SHA256" --root 1=/comics --covers-dir /comics/.covers
   ```

2. Run the same command in an isolated environment with the **proposed**
   configuration and read-only comic mounts. Add
   `--compare-sample "$ORIGINAL_SAMPLE_SHA256"`, using the `sample_sha256`
   from the successful **original** run. The two paths may differ, but the
   library root IDs must refer to the same libraries. Repeat `--root ID=/mount`
   for each nonempty root. Omit mappings to see root IDs and inventory counts;
   an unmapped nonempty root blocks the check. Never map the filesystem root.

Use the effective cover-cache directory for this installation, not necessarily
`/comics/.covers`. If Pullbox also uses a separate legacy cover cache, include
`--legacy-covers-dir /path/to/legacy/cache` on both runs. Without `--covers-dir`,
only cover files in mapped series folders can be checked; a pass with
`cover_cache_mapped: false` is not evidence about a separate cache. Generate a
new original fingerprint with this version; earlier comic-only fingerprints
cannot verify Story Arc files or artwork.

The report distinguishes these checks:

- **Key:** `key_evidence: verified` means the effective application key decrypted
  the saved encrypted values, including metadata sources, download clients,
  indexers, direct providers, artifact hosts and resolver credentials. No provider
  was contacted; this does not verify that a token is current or that a user can
  sign in. `no_ciphertext` means the backup cannot prove which key was used.
  Retain the original key even then. Use the original secret environment setting
  or saved `config.xml`, following normal Pullbox precedence. Never send a key
  in chat or put it in the command arguments. A missing configuration is not
  created and legacy plaintext is not accepted as decryption evidence.
- **Files:** Every registered library file, managed or referenced, must be a
  readable regular file beneath its explicitly mapped root, with the recorded
  size and any saved signature timestamp. Symlinks and paths outside the
  mapped root block the check. File errors include counts and up to five
  numeric library file IDs, not filenames. Verify original mounts before
  treating a missing file as lost; do not clear import or recovery history.
- **Content:** The default hashes the first, middle and last file by ID in each
  nonempty root. It checks any saved SHA-256 or placement digest for those files.
  `sample_compared: true` means the proposed mount also matched the independent
  original fingerprint. A newly recorded sample alone is not a comparison.
  To compare every byte of every registered comic, use `--all-files` on **both**
  runs. This can read the entire library twice.
- **Story Arc files:** Every recorded placement is checked against its canonical
  comic and saved ownership evidence. A hardlink must still share the file's
  identity, a symlink must point to the exact canonical path in the selected
  mount layout, and a copy must still be independent. Adopted files stay
  referenced. Run the check with the same container paths that Pullbox will use,
  particularly for absolute symlinks. A changed inode or device can block the
  check even when the bytes survived: the app cannot safely use its old ownership
  proof. Review the placement in Story Arcs; do not delete files or edit the
  database to pass. A purely logical reading order needs no physical placement.
- **Local artwork:** The fingerprint includes local series, issue and arc artwork
  found in supported folder/cache locations, even without a saved cover URL.
  A missing issue cover is not satisfied by a series fallback. Empty files,
  links and unsupported local references are not certified. No remote artwork
  is downloaded. This compares bytes; it does not decode images or back up
  unrelated files. Counts are per artwork reference, not unique inodes.

Exit code 1 means the check was blocked; do not use a fingerprint from a failed
run. A passing check does not freeze the source, inspect unrelated assets,
start workers, or authorize switching databases.
