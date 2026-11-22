# Check a SQLite backup before a PostgreSQL migration

Pullbox can inspect a backup and tell you whether its **SQLite source checks**
pass. It does not copy rows to PostgreSQL, switch databases, or change your
library. Do not change your database URL based on this report.

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

**A pass is not permission to cut over.** This command does not verify an
empty PostgreSQL target, stop live writers, or prove a successful transfer.
Keep backups of your configuration and encryption secret, covers, and comic
files separately; the Pullbox ZIP contains the database and its manifest only.
