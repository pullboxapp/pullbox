# Import update: v1.3 testing scope

This guide describes the v1.3 feature-branch testing candidate. It is not a
production release announcement. Test with a separate Pullbox database and
library, and keep a backup of the source library and any Mylar database.

## Included behavior

Import Step 1 separates three decisions:

- **Source layout** tells Pullbox how to interpret the existing folders and
  filenames. Choose automatic detection, series folders, publisher and series
  folders, or a custom pattern, then analyze the layout.
- **Keep files in place** registers eligible files inside a configured library
  root without moving, renaming, converting, or rewriting them. A managed copy
  remains available when the source is outside an eligible root.
- **Use this layout for future files** is independent from in-place import. It
  previews and saves a naming policy for future managed files; it does not
  reorganize files that are already present.

Folder context can supply the series identity when a filename contains only an
issue number. Issue titles in filenames and publisher/series nesting are also
supported. Embedded ComicInfo or MetronInfo and supported series sidecars remain
matching evidence, with conflicts kept visible for review.

Mylar imports include series, issues, Story Arc evidence, and
existing arc-file references. Logical Story Arc import and optional separate
arc-file materialization are independent choices. Step 3 remains the final
review before anything is registered or created.

Story Arcs use the same canonical issues as the library. They support ordered
memberships, local issue selection, monitoring, and optional separate managed
copies or links with reading-order filename prefixes. Existing user-owned arc
files remain references. Canonical files are not moved into an arc directory.

## Scanner performance boundary

Filesystem discovery stores its complete inventory in a private temporary
SQLite spool rather than retaining all paths and ordering maps in memory.
Worker concurrency, archive tasks, progress delivery, and ordinary active
directory work are bounded. Temporary inventory is removed after completion,
failure, cancellation, or closing the scan early.

A single directory containing an unusually large number of comic files still
materializes as one active bucket. The scanner supports flat layouts, but its
peak memory can therefore grow with that directory's size. Strict streaming of
this case and formal 200,000-file / 50,000-series certification are deferred.

The existing database batching and matching improvements remain included.
This candidate does not claim a measured end-to-end 200K performance guarantee,
nor completion of the full PostgreSQL, Unraid, Windows, and mounted-filesystem
performance matrix.

## Placement-policy limitation

Choose an arc's managed placement policy before creating its managed files.
Changing an established managed destination or storage mode still requires a
future migration workflow. The existing preview and confirmation preparation
are read-only; execution remains unavailable and the ordinary policy update
fails closed when migration is required.

The unused operation-journal schema is not part of this v1.3 candidate. It is
preserved separately for the later executor, rollback, and recovery work.
Named/private reading lists and full Mylar `readlist` semantics remain a later
roadmap item; they are not implied by Story Arc import.

## Focused user test checklist

Start with a disposable representative sample before trying a complete library.

- Import `Absolute Batman/Issue 01.cbz` and confirm that the folder supplies the
  series, while the issue remains `1`.
- Import `Batman (2011)/Batman The Court of Owls, Part One Issue 001.cbz` and
  confirm that the title text does not create a separate series.
- Import a `publisher/series/issue.cbz` tree and a custom nested layout. Review
  the preview's ambiguity and fallback messages before continuing.
- Keep a sample in place. Compare paths, file sizes, modification times, and
  hashes before and after import; they should be unchanged. Roll back the test
  import and confirm that user-owned source files remain present.
- Repeat with managed-copy mode and confirm that source files remain untouched
  while the selected library receives managed artifacts.
- Enable future layout separately, inspect the before/after examples, and add
  one later managed file. Existing files should not be renamed.
- Import a copy of a Mylar database containing Story Arcs.
  Compare membership order, missing issues, and existing arc-file references.
  Test logical-only import and optional separate copies/links independently.
- Create a Story Arc, add local issues, reorder them through the preview and
  confirmation flow, and check the optional reading-order filename prefixes.
  Canonical issue files and referenced arc files must remain untouched.
- Test a special or large issue number and confirm that the displayed and
  searched number does not change to scientific notation.
- Cancel an import during discovery and during execution. Confirm that progress
  reaches a truthful terminal state and that the existing rollback controls
  remain usable. Retry from a new disposable test job.
- Test one genuinely unsafe archive and one reviewable warning. Confirm that
  bulk review does not bypass non-overridable safety failures.
- Increase a normally structured library sample in stages while using other
  pages. Record file/series counts, elapsed time, peak memory, and any visible
  errors. Treat huge flat-directory testing as an unvalidated scale case.

When reporting a problem, include the diagnostic package, source type, selected
layout and file-handling mode, approximate file/series counts, and the phase
where the problem occurred. Do not send credentials or an unredacted production
database in a public issue.
