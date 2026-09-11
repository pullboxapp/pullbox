# Recheck A Saved Import Review

## Guided Import Contract

Collection imports use the same five stages for Mylar and folder sources:
Source, Analyze, Review, Import, and Finish. The normal path is intentionally
task-oriented:

- Step 1 asks for the source and copy versus keep-in-place behavior. Pullbox
  automatically uses the sole or default writable managed root for copied
  imports. **Where new files go** appears only when there is a real choice or
  no writable destination is available; layout overrides and manual path
  mappings remain under progressive disclosure.
- Mylar path analysis groups a shared root problem into one actionable card.
  When at least one source is available, missing or stale Mylar references are
  retained as non-blocking follow-up instead of requiring an acknowledgement.
- Step 3 shows Ready, Needs attention, and Deferred follow-up first. **Import
  all ready comics** selects the safe canonical set without requiring the user
  to visit every deferred group. Detailed status tables remain available under
  **Review details**.
- Trusted, complete Mylar or ComicInfo Story Arc evidence may create a logical
  Story Arc automatically. Inferred or incomplete arc evidence is retained for
  later review and never blocks canonical comic import.
- Source-mutating actions are not presented during Review. Import Follow-up
  owns unresolved matching and cleanup actions. Import History owns the
  optional clean-library organizer.
- In-place imports keep existing files associated with their current roots. If
  a selected series spans multiple roots, Step 3 requires one writable root for
  future downloads and replacements without relocating the existing files.

Use this maintenance procedure when a review was generated before the Mylar
sidecar parser and comic-content checks were corrected. Normal completed-job
recovery is available in Import Follow-up and does not require an
offline command. It works for Mylar and folder imports. This is not a full
rescan, a database restore, or an import.

## Import Follow-up

The Follow-up tab groups actionable work by import job rather than rendering
one flat, cross-import backlog. Selecting an import opens its matching,
recovery, cleanup, Story Arc, failed-file, and exhausted metadata work in one
bounded workspace. Each bulk action has an exact count, three representative
filenames, and an on-demand detail view limited to 25 files per page. Imports
with only successful, duplicate, skipped, or still-hydrating metadata outcomes
do not appear.

Step 5 remains a concise completion receipt. When follow-up exists, it shows an
above-fold count and a direct link to that import's Follow-up workspace.

Available recovery actions are intentionally narrow:

- **Dismiss stale Mylar references** marks missing database references skipped.
  It does not delete a review record or touch Mylar's database.
- **Skip one-page archives** excludes one-page image archives while leaving the
  source files intact. A one-page archive may be cover art, a damaged archive,
  or an intentional one-page comic, so Pullbox does not delete it automatically.
- **Move a reviewed source to Trash** is an individual post-import option for a
  confirmed cover or unwanted one-page source. It appears only in Follow-up,
  uses a red warning modal and an actor-bound signed preview, and
  requires configured Trash plus source write permission. Reference-only Mylar
  roots cannot use it; the non-destructive skip remains available instead.
- **Skip unusable files** excludes empty, unsupported, and page-less files.
- **Allow oversized files once** retries only decompression-size blocks marked
  overrideable. It does not change the global archive safety policy or approve
  dangerous archive content.
- **Retry source inspection** rechecks files that were unreadable, changed, or
  temporarily could not be inspected, then resumes only work that now passes.
- **Recognize already-owned issues** clears conflicts whose issue already has a
  registered library file.
- **Accept recommended conflict choices** applies only when a conflict group has
  exactly one high-confidence preferred file and the issue is not already
  owned. Alternatives in that group are skipped and the preferred file alone
  is retried.
- **Resolve mixed-folder files** uses exact ComicInfo or trusted sidecar series
  and issue identity to correct files assigned to the wrong imported series or
  issue. Ambiguous titles, filename-only guesses, conflicting target files,
  stale references, and managed files remain review-only. The source path and
  source artifact are never changed.

During a Mylar scan, Pullbox can also reconcile one stale recorded path with a
file found in another series folder when the embedded ComicInfo issue ID is an
exact match. If Mylar's issue ID is stale, Pullbox can use a stricter fallback
only when the embedded ComicInfo series ID, series title, issue number, and
issue ID are trusted; the recorded filename is exact; and the missing record
and candidate are both unique. The real embedded issue ID is preserved. Pullbox
links one canonical file in place for import and classifies only byte-identical
extra copies from the exact-ID path as duplicates. Filename guesses, ambiguous
records, conflicting embedded identity, and non-identical candidates remain
untouched for review.

Follow-up keeps the optional physical cleanup separate from the safe
recovery actions above:

- **Move misplaced file** moves one canonical file to the exact missing path
  already recorded by Mylar. It requires an empty destination, revalidates the
  source fingerprint, and updates the Pullbox reference and rollback journal
  together. Its signed confirmation is a one-time authorization for that exact
  move even when the root is otherwise reference-only; the root's permanent
  managed-write policy and Mylar's database remain unchanged.
- **Move all verified files** applies the same checks to the complete current
  exact-identity scope. The signed preview covers the scope digest and actor;
  changed, ambiguous, occupied, or inaccessible candidates remain untouched.
- **Move duplicate to Trash** is a separate per-file choice available only for
  an extra copy that remains byte-identical to its canonical issue. It requires
  a configured Trash folder and managed-write permission. Pullbox never removes
  these copies automatically.

All actions use an actor-bound signed preview and restore the physical source
if their database update cannot commit. A reference-only Mylar root remains
non-destructive during import and does not become managed after an explicitly
confirmed cleanup move.

Every mutation requires a fresh, actor-bound signed preview. Pullbox rejects an
expired preview or any action whose row set changed after preview. Bounded,
recoverable actions use a normal confirmation; typed confirmation remains
reserved for permanent deletion. Mutations run in bounded database pages,
recompute import counters, and create import and security audit records.
Dangerous, unknown, and genuinely ambiguous outcomes remain manual-review
items.

Once cleanup is complete, **Archive results** hides the finished job from the
current history view without deleting its rows, logs, decisions, or rollback
evidence. Archived jobs can be restored later. **Clear History** never deletes
archived jobs.

## Building A Clean Pullbox Library

A completed reference-only import can be used as the reviewed source for a
separate clean managed library. Import History exposes this optional organizer
for eligible jobs. Its modal shows the exact file count, series count, and
source size, then requires the operator to choose an enabled writable library
root that does not overlap any source root.

The clean-library build creates a normal background Step 4 import. It copies
only files with an exact, current imported-file to library-file to issue
lineage. The destination root's current folder naming, file naming, CBZ
conversion, and ComicInfo policy are applied. Existing skip-existing behavior
does not prevent this explicit adoption, because each verified source reference
is being replaced by its newly managed Pullbox copy.

The operation is intentionally source-preserving:

- The Mylar database, folders, filenames, permissions, and file content remain
  unchanged.
- The destination must have sufficient capacity under the ordinary managed-copy
  preflight, including conversion workspace reserve when conversion is enabled.
- The preview token is actor-bound, expires after 15 minutes, and covers the
  exact source rows and destination root. Changed scope requires a new preview.
- Each completed placement records the prior reference and source identity.
  Rollback removes only an unchanged Pullbox-managed destination, restores the
  original referenced library record, and leaves the Mylar file in place.
- A missing or changed source, occupied old identity/path, stale database
  lineage, or modified managed destination fails closed and preserves the clean
  managed file for review.

Validate the managed library before disabling or retiring the legacy Mylar
root. Source retirement is a separate operator decision; this workflow never
deletes the legacy tree automatically.

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
- For a `COMPLETED` job, prefer the matching in-app recovery action. **Retry
  failed** remains available for ordinary import failures; **Retry source
  inspection** handles completed safety rows whose source should be checked
  again.
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

For a completed import, restart Pullbox, open that import in Follow-up, and
choose the relevant recovery action. Only the previewed scope is prepared; successful
files and series remain untouched. A file that is still missing, outside an
approved root, unreadable, or unsafe remains excluded with refreshed
diagnostics.

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
  Bulk archive-size approval does not approve these files. Moving the source to
  Trash is never automatic and is available only from completed-import cleanup
  after an explicit red warning on a writable source.
- Archive read failures remain distinct from empty archives. An unavailable
  RAR backend, corrupt archive, permissions problem, or disappearing source is
  not evidence that the archive has zero pages.
- Two or more image members pass this content heuristic, not a full image
  integrity guarantee. It deliberately avoids decoding every page or using an
  arbitrary file-size cutoff. PDF/EPUB keep their existing validation paths.

Verify the resulting review and diagnostic logs before asking the user to
confirm an import. A reported host-restart recovery is useful evidence, but
does not replace checking the current job's durable state.
