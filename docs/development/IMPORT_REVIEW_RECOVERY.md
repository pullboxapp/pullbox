# Recheck A Saved Import Review

Use this maintenance procedure when a review was generated before the Mylar
sidecar parser and comic-content checks were corrected. It works for Mylar and
folder imports. This is not a full rescan, a database restore, or an import.

## Safety And Scope

- Stop Pullbox and back up its database before running the command. `--offline`
  is the operator's acknowledgement, not an automatic container stop.
- The job must be idle at Step 3 (`REVIEW`). Do not run this against a scan,
  import, rollback, or job with a pending control request.
- By default only automatically rejected series with
  `trusted_source_identity_conflict` are examined. Repeat `--series-id` to
  narrow the operation to specific **import-review series IDs**, not ComicVine
  or library series IDs.
- An entire series is left untouched if it has manual overrides, selected
  files, explicit skips, approved exceptions, or other completed file decisions.
  The report counts these as `skipped_series`; discuss them individually.
- `--source-root` must name a specific directory visible inside the container.
  Repeat it for multiple mounts. Both lexical and resolved paths must remain
  inside an explicitly permitted root; traversal, sensitive paths, and escaping
  symlinks are not accepted.
- No sources, Mylar databases, ComicInfo files, or library files are changed.
  The command inspects only saved candidates, using bounded database pages and
  one folder-sidecar read per folder in a series. It makes no provider requests.
- This is deliberately transactional. Without `--apply` no changes persist.
  With `--apply`, failure before the final commit rolls back the recheck.

## Docker Compose Example

Replace `pullbox.yml`, the service name, job ID, and source root with the actual
deployment values. The image must contain this command. Keep all existing data,
source, and config mounts on the one-off maintenance container.

```bash
docker compose -f pullbox.yml stop pullbox
```

Back up the stopped instance's database with the deployment's normal backup
procedure. Keep any SQLite WAL sidecars together with a filesystem backup;
do not copy a database file alone while writers are running.

Preview first:

```bash
docker compose -f pullbox.yml run --rm --no-deps --entrypoint python pullbox \
  -m pullbox.cli recheck-import --job 1 --source-root /mnt/comics --offline
```

The JSON result reports `series_prepared`, `files_checked`, `blocked_files`,
and `skipped_series`. `applied: false` confirms it was only a preview. Review
the counts before running the same command with `--apply`:

```bash
docker compose -f pullbox.yml run --rm --no-deps --entrypoint python pullbox \
  -m pullbox.cli recheck-import --job 1 --source-root /mnt/comics --offline --apply
docker compose -f pullbox.yml start pullbox
```

The successful command stages affected series for local matching. Normal
startup recovery resumes from `MATCHING`, preserving the directory inventory
and unaffected review decisions, and returns to Step 3. It does not select
files or start Step 4. Genuine source-identity conflicts remain in review.

## Replaced Files

Changed or missing scan signatures normally remain blocked. After deliberately
replacing a defective file, preview a targeted recheck with both `--series-id`
and `--accept-replaced-files`. This explicitly accepts new scan evidence only
after containment and archive checks; it is not an archive-safety override.
Then repeat the reviewed command with `--apply` if appropriate.

A renamed file at a different path is not automatically discovered by this
command. Do not use broad filename guessing to repair ownership.

## Content Outcomes

- `archive_no_pages`: there are no non-empty supported image members. A
  `ComicInfo.xml` declaring 27 pages does not establish that those pages exist.
  Replace or skip the file; it cannot be allowed once.
- `single_page_comic`: possibly an alternate cover, but also possibly an
  intentional one-page comic. Inspect it and approve individually or skip it.
  Bulk archive-size approval does not approve these files.
- Archive read failures remain distinct from empty archives. An unavailable
  RAR backend, corrupt archive, permissions problem, or disappearing source is
  not evidence that the archive has zero pages.
- Two or more image members pass this content heuristic, not a full image
  integrity guarantee. It deliberately avoids decoding every page or using an
  arbitrary file-size cutoff. PDF/EPUB keep their existing validation paths.

Verify the resulting review and diagnostic logs before asking the user to
confirm an import. A reported host-restart recovery is useful evidence, but
does not replace checking the current job's durable state.
