# Import update: v1.3 testing scope

This guide describes the v1.3 feature-branch testing candidate. It is not a
production release announcement. Test with a separate Pullbox database and
library, and keep a backup of the source library and any Mylar database.

The earlier fully validated checkpoint is `09f306b`. The current candidate also
includes Mylar in-place adoption, original-filename arc copies, and Comic Vine
Story Arc discovery. Live provider, Python, browser, accessibility, static,
security, and isolated Docker smoke checks passed for the completion work.
Those checks support feature testing, not production release approval or formal
large-library certification.

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
supported. Embedded ComicInfo and supported series sidecars remain
matching evidence, with conflicts kept visible for review.

MetronInfo parsing and matching are deferred to the metadata-provider release.

Mylar imports include series, issues, Story Arc evidence, and
existing arc-file references. Logical Story Arc import and optional separate
arc-file materialization are independent choices. Step 3 remains the final
review before anything is registered or created.

Mylar in-place import requires selecting an existing enabled library root in
Step 1. The Mylar database may be outside that root; mapped comic paths determine
eligibility. Missing, unsafe, changed, or outside-root files stay visible for
review instead of silently switching to a copy. Rollback detaches adopted files
without deleting the originals, and trusted Mylar imports make no provider calls.

Story Arcs use the same canonical issues as the library. They support ordered
memberships, local issue selection, monitoring, and optional separate managed
copies or links with reading-order filename prefixes. Existing user-owned arc
files remain references. Canonical files are not moved into an arc directory.

When creating an arc, choose its storage policy and an existing destination
directory inside the selected library root. Copy mode creates independent extra
files. New arc naming defaults to the canonical filename, with an optional
two-digit leading order such as `01 - Batman 001.cbz`; padding and advanced
templates remain configurable. Existing saved policies do not change.
Synchronization applies the chosen policy to later acquisitions.

## Discovering and adding Story Arcs

On **Story Arcs**, use **Find a Story Arc on Comic Vine**. Results appear inline;
already-added arcs link to the existing library entry. Preview the members,
review their order, choose the library root for any new parent series, and set
the arc's independent monitoring and optional storage choices before confirming.

Comic Vine's returned list is **not a verified reading order**. Pullbox preserves
the provider's original ordinal as evidence and lets you choose the actual
sequence, including any leading filename numbers. Its often-zero issue counter
is not treated as an empty arc: the explicit returned member list and exact
issue hydration determine whether the preview can be added. Partial or
inconsistent provider responses cannot create a partial arc.

Existing canonical series and issues retain their paths, monitoring, metadata,
and files. New parent series get their normal canonical folders in the selected
root; only the required arc issues are seeded, not the entire parent catalog.
Arc copies are additional files. Initial copies of available issues are separate
from the future-synchronization choice; failures remain visible and can be
retried without overwriting user-owned files.

**Search missing issues** is scoped to resolved missing members of that arc.
It respects explicit issue skips, existing files/downloads, pending intervention,
and the upcoming option; it does not monitor the parent series. Individual
members link to the canonical issue page for manual result selection. Automatic
search-on-add also respects the global setting. Shared matching, download,
cooldown, and post-processing behavior remains in use.

**Review provider changes** previews refreshes before confirmation. Existing
user metadata/order and storage policy remain unchanged. Newly returned members
start pending review and cannot search or synchronize until resolved; provider
removals are reported without deleting local members or files. Imported arcs
without a saved canonical root require an explicit root choice for new series.
This choice does not relocate existing series or arc files.

The guarded signed development-image workflow and isolated tester instructions
are described in [Signed development images](../development/DEVELOPMENT_IMAGES.md).
Local workflow tests are not evidence that an image has been published.

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
- Search Comic Vine for an event, review/edit its returned order, and add it.
  Confirm that existing parent series keep their monitoring and paths, and new
  parent series use the selected canonical root. Repeat a provider search and
  check that the arc is shown as already added.
- Add an arc with copy mode and future synchronization disabled. Confirm that
  available issues still receive initial copies and source hashes stay unchanged.
  Test a destination collision: it must report a failure, not overwrite a file.
- Review a provider refresh. New members must remain pending until approved;
  existing order, manually skipped members, and provider-removed members stay
  intact. Search missing issues and verify that unrelated parent-series issues
  are not acquired.
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
