# Series-to-Comic Metadata Propagation

The series detail Metadata card has a separate **Update comic metadata** action.
Saving a series or series.json does not trigger comic-file changes.

## Operator Workflow

1. Select series fields explicitly. No fields are selected for a new preview.
2. Preview comic files in the background. No files or metadata records are changed.
3. Review the per-file differences. Select all ready files or individual issues.
4. Apply the selected files. Progress uses the shared utility operation component.
5. Review individual failures through **Review issue**, or retry unfinished writes.

Preview and write results are paginated inside the scrollable standard modal.
The modal may be closed while work continues in Background activity and Utilities.
Cancellation stops unfinished work; a published file finishes journal accounting.
Retry retains the original operation IDs and approvals. Already-published files
recover from their journals rather than being rewritten. Changed files or metadata
require a new preview; retry does not silently approve new content.

## Boundaries

- Fields: series name, publisher, language, publication format, sort name, start year,
  and volume. Start year and volume use the existing Metron series mappings, not
  issue dates or issue numbering.
- Only registered managed CBZ/ZIP files with verified issue/series bindings and a
  reconciled XML pair qualify. Missing pairs link to the issue's existing metadata
  review workflow; the batch does not invent unreviewed issue metadata.
- Reference-only files, read-only roots, native archives requiring conversion,
  import rollback protection, identity disagreements, unsupported XML and protected
  issue-level corrections remain unchanged and receive per-file explanations.
- Issue titles, summaries, dates, IDs, credits, reading state and other unselected
  metadata are not refreshed. Series description never becomes issue summary.
- No folder scan, provider fetch, download, conversion, rename, move, library-wide
  utility, story arc copy synchronization or series.json write is performed.
- The bounded preview supports up to 10,000 registered issues in one series.

## Safety Contract

The existing FILE_METADATA executor, staged paired writer, mutation admission lock,
source fingerprints, verified archive manifest and publication journal are reused.
The optional journal propagation mode contains selected fields and exact canonical
snapshots. It checks the staged pair against both the original source projection
and its own idempotent projection before publication. It cannot carry import-owner
authority or change canonical values. Finalization updates only library-file
accounting, not shared series/issue metadata or baselines, so sibling approvals
remain valid. Historical journals without this mode retain their existing behavior.

Tests cover selected scope, stale approvals, explicit selection, CSRF, protected
files, page preservation, no-op previews, journal recovery without rewriting,
SQLite/PostgreSQL parity, theme/mobile modal bounds and visible progress.

## Planet Atmos Testing

On the October 20 home-server snapshot, issues #5 and #6 qualify. Issues #1 through
#4 remain protected by import rollback history. Do not detach that history merely
to make a propagation test pass. A private copy verified selected changes to #5/#6
and preserved all page/other archive entries, series.json and canonical records.
