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
Credential decryption, physical file verification and explicit cutover are
separate steps; even a successful stage is **not ready for normal startup**.
