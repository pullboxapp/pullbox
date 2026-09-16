# Removing an unused library root

In **Settings > Media Management > Library roots**, disable a root and choose
**Remove root**. Review the checks, choose **Confirm removal**, then confirm the
root's name and path in the standard confirmation dialog.

This removes configuration, including root-specific naming settings. It never
deletes, moves, scans, or changes permissions on folders or comics. An offline
or missing directory can be removed if nothing still depends on its root.

Removal is blocked while a root is enabled, is the default destination, or has
registered files, current/preferred series associations, Story Arc destinations
or placements, or default destination settings. The preview gives exact counts
and the next step for each blocker. Disabling a root does not detach its files.
Relocate or explicitly remove the associated library entries first; root
removal is not a bulk-delete-files shortcut.

Finish or cancel active/paused imports and file utilities before removal.
Pending import placement or rollback work also blocks removal. Confirmation
rechecks dependencies; a changed or expired preview must be reviewed again.

Historical imports do not trap unused roots forever. Their original root name,
path, and ID are preserved, and their history and rollback records remain.
After removal, retry/recovery cannot silently use the current default root:
start a new import and explicitly choose a valid destination. Existing imported
library contents are not removed by this operation.

## Developer safeguards

All live root foreign keys use `RESTRICT`, and the ORM does not delete registered
files when their root is deleted. Historical import destinations are explicitly
detached with a retained snapshot in the same transaction as removal. Only the
root's own naming policy is discarded. Previews are operator-bound, signed, and
valid for 15 minutes. SQLite confirmation takes a short write reservation before
rechecking; final deletion also checks dependencies and relies on FK enforcement.

The migration rebuilds affected SQLite tables on a dedicated Alembic
connection with foreign-key enforcement disabled for migrations only, then
checks referential integrity. Back up the database before applying migrations.
Runtime connections keep enforcement enabled. Existing inline constraints are
preserved when downgrading so older migrations continue to work correctly.
Downgrade is refused once history contains a removed-root snapshot, because old
code cannot honor its retry guard. PostgreSQL uses ordinary FK alterations.
