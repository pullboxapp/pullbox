# Check a SQLite backup before a PostgreSQL migration

Pullbox can inspect a backup and tell you whether its **SQLite source checks**
pass. It does not copy rows to PostgreSQL, switch databases, or change your
library. Do not change your database URL based on this report.

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
PostgreSQL 16 and 18, including upgrades from a populated test database. This
does not transfer a SQLite library or qualify the full application for cutover.

**Neither pass is permission to cut over.** The checks do not reserve the
target, stop live writers, initialize a schema, transfer rows, or verify a copy.
Keep backups of your configuration and encryption secret, covers, and comic
files separately; the Pullbox ZIP contains the database and its manifest only.
