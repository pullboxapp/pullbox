# Recheck A Saved Import Review

Use this maintenance procedure when a review was generated before the Mylar
sidecar parser and comic-content checks were corrected, or when a completed
import has retryable source-change failures. It works for Mylar and folder
imports. This is not a full rescan, a database restore, or an import.

## Safety And Scope

- Stop Pullbox and back up its database before running the command. `--offline`
  is the operator's acknowledgement, not an automatic container stop.
- The job must be idle at Step 3 (`REVIEW`) or finished (`COMPLETED`). Do not
  run this against a scan, import, rollback, or job with a pending control
  request.
- For a `REVIEW` job, only automatically rejected series with
  `trusted_source_identity_conflict` are examined by default. Repeat
  `--series-id` to narrow the operation to specific **import-review series
  IDs**, not ComicVine or library series IDs.
- For a `COMPLETED` job, only failed files carrying retryable source
  revalidation evidence are examined. Imported siblings and series decisions
  are never reset. The recheck updates safe source evidence; the existing
  **Retry failed** action performs the actual retry after startup.
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

For a Step 3 review, the JSON result reports `series_prepared`,
`files_checked`, `blocked_files`, and `skipped_series`. For a completed import,
it reports `files_prepared`, `files_checked`, `blocked_files`, and
`skipped_files`. `applied: false` confirms it was only a preview. Review the
counts before running the same command with `--apply`:

```bash
docker compose -f pullbox.yml run --rm --no-deps --entrypoint python pullbox \
  -m pullbox.cli recheck-import --job 1 --source-root /mnt/comics --offline --apply
docker compose -f pullbox.yml start pullbox
```

The successful command stages affected series for local matching. Normal
startup recovery resumes from `MATCHING`, preserving the directory inventory
and unaffected review decisions, and returns to Step 3. It does not select
files or start Step 4. Genuine source-identity conflicts remain in review.

For a completed import, restart Pullbox, open that import's results, and choose
**Retry failed**. Only the rechecked failures are prepared; successful files
and series remain untouched. A file that is still missing, outside an approved
root, unreadable, or unsafe stays failed with refreshed diagnostics.

## Replaced Files

Changed or missing scan signatures normally remain blocked. After deliberately
replacing a defective file, preview a targeted recheck with
`--accept-replaced-files` and, when useful, one or more `--series-id` filters.
This explicitly accepts new scan evidence only after containment and archive
checks; it is not an archive-safety override. Then repeat the reviewed command
with `--apply` if appropriate.

A renamed file at a different path is not automatically discovered by this
command. Do not use broad filename guessing to repair ownership.

## Stale Mylar Filenames

Use `reconcile-import-paths` for a different problem: Mylar remembers a filename
such as `Firefly Bad Company #1 (2019).cbr`, while the saved review already has
`Firefly Bad Company 001 (2019).cbz` matched in the same folder. This command
does not enumerate the library or restart matching. The job stays in Step 3.

The image must include this command. Stop Pullbox and back up its database as
above, keeping the deployment's existing mounts on the maintenance container.
Preview first:

```bash
docker compose -f pullbox.yml run --rm --no-deps --entrypoint python pullbox \
  -m pullbox.cli reconcile-import-paths --job 1 --source-root /mnt/comics --offline
```

Repeat `--source-root` for additional approved mounts. Use `--series-id` to
limit the preview to particular **import-review series IDs**, not ComicVine IDs.
The report includes:

- `missing_references`: missing entries in the requested scope.
- `candidates_checked`: entries with one matched same-folder counterpart and
  the same stored ComicVine issue ID. This alone does not authorize a repair.
- `references_reconciled`: entries that pass current filesystem, signature,
  archive safety, content, and independent ComicInfo identity checks.
- `remaining_missing_references`: entries that would remain after applying.
- `retained_reasons`: counts explaining why entries remain, including
  `no_unique_matched_counterpart`, `review_or_source_protected`,
  `source_check_failed`, `file_safety_review`, and `identity_unconfirmed`.
- `samples` and `retained_samples`: bounded examples, including original paths.

After reviewing the preview, repeat with `--apply`:

```bash
docker compose -f pullbox.yml run --rm --no-deps --entrypoint python pullbox \
  -m pullbox.cli reconcile-import-paths --job 1 --source-root /mnt/comics --offline --apply
docker compose -f pullbox.yml start pullbox
```

Only the obsolete review reference is removed. Its original path and review ID
remain in the real file's reconciliation diagnostics. The real file keeps its
match and selection state; counters are recomputed and a summary is logged.
An interrupted command rolls back; repeating a successful command is safe.
The grouped candidate query runs once and streams bounded batches rather than
rescanning the database for every page.

Safeguards:

- Require independent, equal ComicVine issue IDs from Mylar and inspected
  ComicInfo, compatible series/issue/type evidence, and no identity conflicts.
- Require one missing reference and one existing counterpart in the same
  folder. Ambiguous copies and cross-folder guesses remain unresolved.
- Refuse changed files, symlinks, root escapes, unreadable files, corrupt or
  content-blocked archives, and files requiring a safety override.
- Leave an entire series alone when it has manual matches, selections, skips,
  approvals, or completed decisions. Never delete a referenced review row.
- Do not edit Mylar's database, rename files, rewrite ComicInfo, download
  metadata, or import anything. Existing source files remain untouched.

New scans perform the same identity check after archive inspection, reusing
the cached member evidence. Folder imports share the identity/content safety
rules but have no stale Mylar database references to repair. Missing-path copy
does not assume a file disappeared after the scan: it may never have existed
under the database's recorded name.

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
