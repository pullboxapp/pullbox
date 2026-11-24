# Prepare a SQLite library for PostgreSQL

Start with the read-only source and destination checks below. A disposable
rehearsal or a retained **offline staging copy** is a separate, explicit action.
None of these commands switches your running library.

**First-start checks.** In a disposable trial, sign in with a migrated account,
open a known owned issue and its Story Arc, and check **System > About** for
the PostgreSQL server version. An old SQLite backup that contains lowercase
issue classifications cannot transfer those rows unchanged into a readable
library. Upgrade a separately restored copy: the migration normalizes known
labels and fixes the default without reimporting or editing comics. It stops
on unknown labels. On SQLite it rebuilds the issues table, so allow temporary
space and keep the original backup. If it reports custom issue triggers or
expression indexes, do not remove them from the live database to force an upgrade.

Bootstrap logs from older versions may contain the database URL. If a log was
shared with a PostgreSQL password in it, rotate that credential and update the
deployment secret; an application upgrade does not redact existing log files.

If an already running PostgreSQL library shows **Library temporarily unavailable**,
check that its PostgreSQL server is accepting connections. Once it is back,
use **Retry this page**; a temporary connection failure does not require a new
login, another migration, or a switch back to SQLite. If the message followed a
change rather than a page load, check the issue or job's current status before
submitting the change again.

## Back up and restore a PostgreSQL library

This procedure is for an **already running PostgreSQL library**, not for
opening an offline migration stage. On PostgreSQL, **System > Backup** explains
the native procedure. Pullbox does not schedule SQLite ZIP backups, prune old
ZIPs or apply SQLite post-restore actions to PostgreSQL. Arrange your own
backup schedule and off-host storage.

1. Pause writers, finish outstanding operations, then stop Pullbox and keep it
   stopped while capturing the database **and** matching files. Include your
   comics, Story Arc placements, covers, configuration, encryption secret and
   deployment settings. A database dump alone cannot undo a later file edit or
   prove that a journaled operation is safe to replay.
2. Use PostgreSQL client tools matching the server's major version. On a private
   backup host set `PGHOST`, `PGPORT`, and `PGUSER` explicitly; use a protected
   password file or your normal secret mechanism. The database owner needs
   read access to the entire library. This example dumps one database, not
   cluster roles or privileges. Preserve the role and access configuration
   separately. Set `BACKUP_DIR` to an existing private directory and
   `SOURCE_DB` to the running library's database name. In a shell:

   ```sh
   (
     set -eu
     umask 077
     capture=$(mktemp -d "${BACKUP_DIR:?}/pullbox-pg.XXXXXXXX")
     pg_dump --format=custom --dbname="${SOURCE_DB:?}" --file="$capture/library.partial"
     pg_restore --list "$capture/library.partial" >/dev/null
     mv "$capture/library.partial" "$capture/library.dump"
     printf 'Database archive: %s\n' "$capture/library.dump"
   )
   ```

   A failed dump is not a restore point. Protect the completed backup and key
   separately; custom format does not encrypt the dump. A readable table of
   contents is **not** a restore test.
3. Rehearse on an isolated server with **no Pullbox instance connected**.
   Set the connection variables to that server, `DUMP` to your trusted archive
   and `RESTORE_DB` to a **new** database name. Use a dedicated database owner
   authorized to create this rehearsal database; do not grant the running
   app this privilege. Reapply any site-specific grants separately.

   ```sh
   (
     set -eu
     : "${DUMP:?}" "${RESTORE_DB:?}"
     createdb --template=template0 -- "$RESTORE_DB"
     pg_restore --single-transaction --exit-on-error --no-owner --no-privileges \
       --dbname="$RESTORE_DB" "$DUMP"
   )
   ```

   If the name is occupied, creation fails and the restore does not run.
   Do not add `--clean` or `--create`, restore over your live database, or
   reopen a sealed stage. Leave a failed rehearsal unconnected and investigate.
4. With workers still off, compare the restored schema revision, ownership,
   import follow-up, Story Arc reading order, file journals and at least one
   change made **after** migration. Verify the original key can decrypt a saved
   credential without printing it, and verify matching files against their
   saved ownership evidence. If any evidence differs, do not start workers.
   After new PostgreSQL writes, the old SQLite snapshot is **not** a current
   rollback target. This rehearsal does not authorize a live switch.

See PostgreSQL's [pg_dump reference](https://www.postgresql.org/docs/18/app-pgdump.html)
and [pg_restore reference](https://www.postgresql.org/docs/16/app-pgrestore.html).

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

An import waiting at Step 3 still blocks migration even if no files are being
written. In the original SQLite instance, go to **Import > History > Open review**.
For a Mylar issue with conflicting embedded metadata, correct the source or linked
identity and use **Recheck source**, or use **Skip this file** to leave it out.
You can also import just the ready files and leave unresolved files in **Follow-up**.
Wait for the import to finish and take a fresh backup. A completed import does
not block migration simply because it has follow-up items. After migration, use
**Follow-up > Review conflicts** for unresolved Mylar identity conflicts in an
existing series. Open the recorded issue to import a corrected copy, or skip
individual references. Other unresolved files and previously imported comics
are left alone.

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

## Check whether the Docker source is offline

The online backup above is suitable for rehearsal. Before preparing a final
snapshot, check that the source is actually offline. From a checkout on the
**local Docker host**, run:

```sh
python scripts/check_sqlite_source_offline.py --container pullbox
```

You can run this while the source is live to see what blocks. The command
does not stop or restart anything. For an offline check, first settle
pending file operations in Pullbox, suspend any service that automatically
redeploys it, disable the container restart policy, and stop it cleanly.
Keep the stopped container for comparison; do not remove it or its volumes.
A crash or forced kill is not accepted as a clean stop.
If the report lists `other_writer_ids`, inspect those containers before doing
anything to them. Even a client writing to a shared downloads folder can
invalidate the offline assumption. Read-only peer mounts do not block.

Save the successful `checkpoint_sha256`. Check again with the same source
and fingerprint after the offline work:

```sh
python scripts/check_sqlite_source_offline.py --container pullbox --compare-checkpoint "$OFFLINE_CHECKPOINT"
```

A restart followed by another stop, a replacement source, or a newly observed
writable peer invalidates the earlier evidence. A failed check returns exit
code 1 and no usable fingerprint. This is a **Docker state fingerprint, not
a backup checksum or a write lock**. The check alone does not create a
snapshot, verify application journals, or authorize cutover.

This check is for one standalone local Linux Docker Engine (including Docker
Desktop) with an explicit absolute `PULLBOX_DB_URL` and persistent data and
library directories. It compares writable bind paths and ordinary local
volumes, including parent and child mounts. It does not qualify orchestrators,
remote/shared storage, host writers, other daemons, filesystem aliases or
writers created and removed between observations. Do not rely on it for those
setups. Keep external automation disabled and the source offline throughout.

## Capture the offline SQLite snapshot

From the same host checkout with Pullbox dependencies installed, keep the source
stopped and use the `checkpoint_sha256` you recorded above. Choose a **new**
file in a trusted local directory you own, outside the source mounts:

```sh
(umask 077; set -C; python scripts/check_sqlite_source_offline.py --container pullbox \
  --compare-checkpoint "$OFFLINE_CHECKPOINT" --snapshot /private-backups/pullbox-offline.db \
  > /private-backups/capture.json)
```

The command runs Python from the stopped container's exact local image without
starting Pullbox or downloading an image. The helper has no network, mounts
the database storage read-only and writes only to private scratch. It uses the
SQLite backup API, including committed WAL data. If the WAL index is absent,
it rebuilds the index on a private working copy, not in the library. Use an
ordinary bind or local volume with no volume subpath. The local filesystem
must support hard links and directory sync. Allow space for the database plus
its WAL and 256 MiB, or twice the database-plus-WAL size and the same reserve if
the index needs rebuilding. Do not delete or modify journals to pass the check.

The file is published with owner-only permissions only after schema, integrity,
foreign-key and outstanding-work checks pass, the source files still have the
same identity, size and timestamps, and the Docker checkpoint still matches.
An existing file, symlink or SQLite sidecar at the destination blocks publication.
If blocked, resolve the stated cause; if you restart the app, get a **new**
offline checkpoint. Do not use scratch files left by a forced interruption.
If the host process was killed, list only its helpers with
`docker ps -a --filter label=pullbox.offline-source=snapshot` and confirm
that no capture is active before removing a leftover helper.

Save the successful report and use this exact file for the transfer. Compare
its **`snapshot_sha256`** with the staging report; that digest identifies the
database contents, unlike the Docker checkpoint. This does not save your
configuration, encryption key, artwork or comics. The source stays stopped,
and **`cutover_ready` remains false**.

## Back up and rehearse restoring the configuration

Keep the SQLite source offline. The database contains editable application settings and
encrypted credentials, but it does **not** contain the key needed to read them. Preserve
the **SQLite deployment as it actually ran**, not the edited settings you plan to use
for PostgreSQL.

1. In the directory containing the verified `pullbox-offline.db`, make a **new**
   owner-only directory named `settings` (`mkdir -m 700 settings`). Copy the original
   Compose file or exported deployment definition, overrides, environment files and
   required deployment-specific configuration there. Keep their relative layout.
   Do not put comics, catalogs, logs or database files inside `settings`.
2. For a file-managed key, copy the existing `config.xml` into `settings/config.xml`.
   For example, inside this private directory use
   `docker cp pullbox:/data/config.xml ./config.xml` with the **stopped** source's actual
   data path, then `chmod 600 config.xml`. Do not start Pullbox to generate a new file.
3. If the source used **`PULLBOX_SECRET_KEY`**, save its original value from your
   protected secret store as `settings/application-key`, a mode-600 file containing one
   UTF-8 line. Preserve any existing `config.xml` too. The override wins even if the XML
   contains a different key. Do not dump the container environment, put secrets in
   commands, or share these files or their diffs. The recovery file is not a runtime
   environment setting; on a real restore, supply that value through your secret store.
4. Back up the snapshot and settings together using protected, preferably encrypted
   backup storage. **Restore from that backup into a separate empty directory**.
   Keep the original reference settings for comparison. For example, with `CAPTURE`
   pointing to the snapshot's directory, `STORE` to a private encrypted backup
   location, and `RESTORE` to a new directory:

   ```sh
   (umask 077; set -C; tar -C "$CAPTURE" -cf - settings pullbox-offline.db > "$STORE/pullbox-pre-migration.tar")
   mkdir -m 700 "$RESTORE"
   tar -C "$RESTORE" -xf "$STORE/pullbox-pre-migration.tar"
   ```

   This example does not encrypt the archive itself. Only extract your trusted backup,
   and keep the encryption key for the backup storage recoverable separately.
5. From a checkout with Pullbox dependencies installed, run:

   ```sh
   python -m pullbox.cli migration-check-restore \
     --backup "$RESTORE/pullbox-offline.db" --snapshot-sha256 "$SNAPSHOT_SHA256" \
     --original-settings "$CAPTURE/settings" --restored-settings "$RESTORE/settings" \
     --key-source config-xml
   ```

   Use `--key-source secret-file` for an environment-managed key. If your deployment
   definition is not `compose.yaml`, give its relative path with `--deployment-file`.
   Use the **database digest** from the original snapshot report, not its Docker
   checkpoint. Recheck the Docker checkpoint separately before going further.

A successful report means the restored database passed preflight and matched its
digest, the supplied configuration trees have identical names and file contents,
and the explicitly selected restored key was validated. `key_evidence: verified`
means it also decrypted the saved credentials. `no_ciphertext` means there were none
to test: matching copies do not establish which key the historical app used. A key
in the checking shell or its local `.env` cannot make the restore pass.

The check reads only; it does not render or execute the deployment definition, set
file ownership for your container, or discover files you forgot to preserve. Both
settings directories must be private and independent, without symlinks or hardlinked
files (up to 128 entries, 4 MiB per file and 32 MiB per set). Preserve media, artwork,
external mount definitions, and any other required appdata separately. Keep the report,
not the private recovery material, for troubleshooting. **Do not cut over on this
report alone.**

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

### Check the intended deployment without starting it

To bind a **new stage** to the intended deployment, generate a fresh secret
in a private operator-owned directory. Do not reuse a Pullbox password,
encryption key, or token from a previous attempt:

```sh
(umask 077; set -C; openssl rand -hex 32 > first-start.token)
python scripts/stage_postgres_transfer.py --backup /private-backups/pullbox-offline.db \
  --create-offline-stage --review-library --first-start-token-file first-start.token \
  --source-capture-report /private-backups/capture.json
```

After you sign in and confirm the library, the report should say
`first_start_bound: true` and `source_capture_bound: true`. The stage is still
closed. Only fingerprints are stored, never the secret. A capture report for
different snapshot bytes is rejected before the target is reserved. Cancelling
review does not bind the target.

In the **proposed PostgreSQL deployment**, mount that token read-only at
`/run/secrets/pullbox-first-start`, readable by the Pullbox process only
(a regular file with mode 0400 or 0600). Provide the proposed database URL
through the service's normal protected configuration. Use this image's CLI
instead of its normal entrypoint; do **not** start the app:

```sh
docker compose run --rm --no-deps --entrypoint python pullbox -m pullbox.cli \
  migration-check-first-start --token-file /run/secrets/pullbox-first-start \
  --snapshot-sha256 "$STAGED_SNAPSHOT_SHA256"
```

Use your actual Compose service name. This command connects only to the
`postgres` maintenance database, not the library. Exit 0 and
`first_start_binding_verified: true` mean that the configured owner, running
server, closed database, snapshot and token match the review. A PostgreSQL
restart invalidates this pending binding; use a fresh stage rather than
editing the database label. Keep the token out of logs and support bundles.

**This is not permission to switch.** The check cannot prove nobody previously
reopened the target. It does not verify the source is still stopped,
validate the independent backup or mounts, enable
connections, or start workers. Pullbox refuses ordinary startup and migrations
on a bound stage, including a clone containing its internal marker. Leave both
the stage and the SQLite backup intact. Do not remove the marker or manually
enable connections.

### Recheck the stopped source against the reviewed target

On the Docker host that owns the **still-stopped SQLite container**, use the
original capture report and token. Choose an existing, trusted scratch directory
outside the source mounts with enough space for another offline snapshot:

```sh
python scripts/check_postgres_handoff.py --container pullbox \
  --capture-report /private-backups/capture.json --token-file first-start.token \
  --scratch-dir /private-backups
```

Enter the reviewed PostgreSQL URL at the hidden prompt. The command verifies the
closed target, repeats the SQLite backup with committed WAL data, compares it
with the snapshot and source file state bound to the review, checks the Docker
writer inventory again, and verifies the target a second time. Scratch is removed
on success or a reported failure. The original snapshot is never replaced.

Exit 0 and `source_and_target_verified: true` mean both sides still matched at
the time of the check. A host edit can fail this check even if Docker reports
that the source never restarted. If the source data changed, keep both copies;
take a new verified snapshot and review a fresh stage. An older token-only
stage can still pass the deployment check above, but cannot pass this handoff.
Do not edit the capture report or database label to make them match.

This command does not lock out host processes or change Compose. **Do not
start either installation based on this report.** The configuration restore,
file checks and explicit cutover are separate; `cutover_ready` remains false.

### Approve and check the intended deployment

Use this after the source-bound stage and independent backup restore above. It
checks what the **proposed container actually sees**, not just what a host-side
file browser sees. Keep the source stopped and automatic restarts disabled.

1. From a private terminal on the source Docker host, request an approval:

   ```sh
   python scripts/check_postgres_handoff.py --container pullbox \
     --capture-report /private-backups/capture.json --token-file first-start.token \
     --scratch-dir /private-backups --write-approval /private-backups/approval.json \
     --original-backup /private-backups/pullbox-offline.db \
     --restored-backup /restored/pullbox-offline.db \
     --original-settings /private-backups/settings --restored-settings /restored/settings \
     --key-source secret-file
   ```

   Use `config-xml` instead for an installation whose effective key came from
   `config.xml`. The tool checks that key against the stopped source, even
   when there are no saved provider credentials. It never prints the key.
   Confirm the snapshot identifier at the prompt. A private approval file is
   written **only if** the two backups, settings, source media, stopped source
   and reviewed target pass their checks. Enter cancels. An existing approval
   is not overwritten. An approval expires in one hour.

   Media paths come from the source database and Docker mounts. This host
   workflow requires host-readable bind mounts without nested mounts under a
   checked media directory, including access to the original data directory for
   the legacy cover cache and, when applicable, the effective `config.xml`.
   It does not guess paths for Docker-managed volumes.
   The default checks every registered comic's file evidence and hashes a
   representative sample per root, plus placement and cover evidence. Use
   `--all-files` to hash every registered comic on both sides.

2. In the proposed Compose deployment, temporarily mount **only the restored
   SQLite backup file**, approval file and first-start token read-only. Keep
   the secret files owner-only and readable by the Pullbox user. Use the
   proposed service's normal key, comic mounts and cover settings.

   For example, add these entries to the **target** service's existing
   `volumes` list. Set each variable to the absolute path of an existing file
   on the Docker host. Do not point the backup entry at the original SQLite
   database or mount its parent directory.

   ```yaml
   volumes:
     - type: bind
       source: "${PULLBOX_RESTORED_SQLITE:?set the restored backup file path}"
       target: /run/migration/pullbox-offline.db
       read_only: true
       bind:
         create_host_path: false
     - type: bind
       source: "${PULLBOX_CUTOVER_APPROVAL:?set the approval file path}"
       target: /run/secrets/pullbox-approval.json
       read_only: true
       bind:
         create_host_path: false
     - type: bind
       source: "${PULLBOX_FIRST_START_TOKEN:?set the token file path}"
       target: /run/secrets/pullbox-first-start
       read_only: true
       bind:
         create_host_path: false
   ```

   A missing file should stop the container from starting, not create an empty
   directory. Keep these mounts for both the check below and the one-time
   startup. Remove them from Compose only after **Cutover committed**.

   ```sh
   docker compose run --rm --no-deps --entrypoint python pullbox -m pullbox.cli \
     migration-check-first-start --snapshot-sha256 "$STAGED_SNAPSHOT_SHA256" \
     --token-file /run/secrets/pullbox-first-start \
     --approval-file /run/secrets/pullbox-approval.json \
     --restored-backup /run/migration/pullbox-offline.db
   ```

   Exit 0 and `deployment_approval_verified: true` mean the deployed key,
   backup, actual media paths, content fingerprint and closed PostgreSQL target
   matched the approval. A writable backup mount is rejected. Neither this
   command nor issuing the approval enables PostgreSQL writes, runs migrations,
   starts recovery, or edits Compose. If it expires, use a new approval filename
   and repeat the host check. The container cannot tell whether someone restarted
   SQLite after the approval was issued. Leave the stage closed.

### Start the reviewed PostgreSQL library

This is the **write boundary**, not another preview. Use two private terminals,
the reviewed database itself (not a clone), and a separate Compose project or
service for the target. Do not replace or restart the stopped SQLite container.
Keep external writers and auto-redeployment disabled throughout. Pullbox does
not edit Compose or start a container on your behalf.

1. Prepare the target service with its normal protected PostgreSQL URL, key,
   separate persistent appdata, and the media and covers you verified. Do not
   mount the original SQLite appdata writable. Retain the three read-only
   mounts from the approval check. Disable the target restart policy for the
   first start. Ensure no other instance uses this database.
2. In terminal one, start the intended service with this one-time entrypoint.
   Substitute your target service name; check the service ports are free.

   ```sh
   docker compose run --name pullbox-pg-first --service-ports --no-deps \
     --entrypoint python pullbox -m pullbox.cli migration-start \
     --token-file /run/secrets/pullbox-first-start \
     --approval-file /run/secrets/pullbox-approval.json \
     --restored-backup /run/migration/pullbox-offline.db \
     --snapshot-sha256 "$STAGED_SNAPSHOT_SHA256"
   ```

   Wait for **First-start challenge**. The container checks its own key and
   mounts, then compares every application table in the original PostgreSQL
   database with the restored SQLite snapshot. It checks generated ID sequences
   without consuming any IDs. Only its inspection connection remains; normal
   startup and workers are still blocked. No database content is repaired
   automatically. Stopping this container while it waits cancels that attempt.
3. In terminal two on the source Docker host, use the displayed challenge.

   ```sh
   python scripts/check_postgres_handoff.py --container pullbox \
     --capture-report /private-backups/capture.json --scratch-dir /private-backups \
     --token-file first-start.token --authorize-start "$CHALLENGE"
   ```

   If the new container uses the **same writable comics bind** as the source,
   add `--waiting-container pullbox-pg-first`. This allows only the named,
   non-restarting first-start process displaying that challenge. Its comics
   mount must use the same container path; it cannot also share writable source
   appdata or downloads. Leave unrelated mounts out of the first start, or use
   independently restored paths. All other writable peers still block.

   Confirm the snapshot at the prompt. The host rechecks the stopped container,
   WAL-aware snapshot and target, then sends confirmation directly to the waiting
   session. There is no response code to copy or save. A cancelled host prompt
   sends nothing. A source restart requires a new capture and stage; an expired
   approval requires a new approval. Do not re-use a challenge from another run.
4. In terminal one, look for **Cutover committed** followed by normal startup.
   Sign in with a migrated account, check the PostgreSQL version under
   **System > About**, and open a known owned issue, its Story Arc and import
   follow-up. For subsequent starts use the normal image entrypoint and the
   same PostgreSQL configuration and files. Stop the first instance before
   starting the regular service. The token, approval and restored SQLite mount
   can now be removed. Keep the backups themselves.

**Test a disposable migrated library before switching your live one.** Use
independent copies of your app data and media. Sign in, open a known wanted
issue, and use **Import File** with a test CBZ. If the incoming folder is
read-only, use **Copy**: migration preserves your existing transfer setting,
and **Move** requires permission to remove the source. Check that the issue
is owned, the imported pages are readable, and the source copy is intact.

In **Import Follow-up**, preview **Recheck deferred files** for a completed
import. A record proven to match an already registered file should not create
a second copy. A file without sufficient evidence should remain in Follow-up.
Afterward, test restoring a new PostgreSQL backup alongside the matching media;
the restored database should show those decisions. Do not perform test writes
on an offline stage you plan to use for a real cutover.

If you use **What's New > Watch**, also open **Pull List > Watching**. A saved
Watch should still be listed, and a previously cancelled Watch should not be.
Use **Find & Add** on an active Watch with a fresh release, choose a provider
result, and check the library in the preview. It should be the library you chose
when you watched the series, even if another library is the default. Cancel the
preview to leave the Watch and files untouched. On the disposable rehearsal
only, try cancelling a different Watch, take a PostgreSQL backup, and check that
the cancellation persists after restore.

If first-start approval reports `source_root_not_mapped`, compare the reported
library root ID with the **stopped SQLite container's** bind mounts. Check every
configured library, not just the default one. Correct the mapping to the original
folder, not an empty replacement; if the container changes, repeat capture and
staging rather than reusing its approval.

**If startup is interrupted:** do not start SQLite based on a lost terminal or
a failed health check. Check the database status from the maintenance database
as shown above. A still-bound label means cutover did not commit; an interruption
during inspection can leave connections enabled but normal startup still fenced.
Do not remove markers or open it manually; use a fresh target if it is not closed.
After successful cutover the database accepts connections, the bound label is
gone and `pullbox_stage.completed_first_start` exists **inside that database**.
If these signals disagree, keep both apps stopped and investigate. Once cutover
commits, treat PostgreSQL as authoritative even if app startup then fails.
Repair it or restore a current coordinated PostgreSQL backup; do not switch the
URL back to the old SQLite snapshot.

If you cancelled **before** commit, confirm the bound stage is closed and the
temporary first-start container has exited. You can then remove **only that
exited container** (`docker rm pullbox-pg-first`, without `-v`) to reuse its
name. Do not remove the stopped SQLite container. Rerun the approval check if
it expired; restart the target command to get a fresh challenge.

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
