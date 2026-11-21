# Native Provider Folder Import Checkpoint

## Available Behavior

Folder imports can match a CBZ's primary Metron series and issue IDs to verified
identities already saved in Pullbox. The local series and issue do not need
ComicVine IDs. Matching uses saved identities, not title similarity or a live
provider request, and does not create or attach provider links.

Step 3 shows the series under Ready with a Metron identity label. Since this is
an existing series, use its ready-file selection controls to add missing issues.
Already-owned issues remain excluded. An unverified identity stays in review.
Use **Review identity** to preview and add its exact Metron series without leaving
Step 3, or correct an existing provider link and choose **Recheck linked
metadata**. A successful Add queues that same local recheck. Files are never
selected or imported by either action; skip remains available.

The preview reuses the regular source-aware Add Series dialog and API, including
source availability, catalog review, managed-root checks, and final identity
validation. Only an explicit preview/add contacts Metron; scanning, rendering,
polling, and rechecking use saved evidence and local verified identities.

Adding the series is independent of the import. Cancelling or rolling back the
import does not delete that added library series. The dialog states this before
confirmation. Competing series IDs within a single file cannot use this shortcut,
and conflicting issue evidence remains in review after the series has been added.

## User Test

Use an isolated development library and copies of import sources.

1. Add a series through Metron, or verify its Metron link using the existing
   series metadata workflow. Its issue catalog must be present locally.
2. Choose an unowned issue and a CBZ containing `MetronInfo.xml` whose primary
   Metron ID and `Series/@id` identify that exact issue and series. A ComicVine
   identity and `ComicInfo.xml` are not required.
3. Start a **Scan folder** import. In Step 3, open **Ready**. Check that the series
   is labeled **Metron identity**, and select its ready file.
4. Complete the import. Check the issue page and library file. The original
   import-source bytes must remain unchanged.
5. Scan the same source again. The owned issue should not be selected or imported
   a second time.
6. With a separate source copy, use an unknown issue ID or a conflicting issue
   number. It must stay out of the ready import, even if its filename matches.
   Correct the provider link and use **Recheck linked metadata** in the same job,
   or skip the file/series. If the source file itself changes, use the existing
   source recheck/rescan workflow rather than trusting its old saved evidence.
7. Roll back the successful test import. The test copy should be removed and
   the issue should return to its recorded pre-import status, unless a later
   choice or surviving library file means that status must be preserved.
8. For a series not yet in the library, open **Review identity**, then **Preview
   Metron series**. Confirm the title and managed library root before adding it.
   After the recheck, select the ready file explicitly. Cancelling the preview
   must add nothing, and a late response must not reopen the dialog.

Do not invent provider IDs in a real library. The automated test fixture uses
synthetic identities only in a disposable database.

## Safety And Compatibility

- Metadata members are read through the existing bounded ZIP inspection, without
  extracting archive-controlled paths or opening a second archive session.
- Every automatically matched file needs its own exact identity. A tagged file
  does not authorize untagged neighbors in a mixed folder.
- Competing exact identities and issue-number disagreements block the match.
  Unknown descriptive XML fields remain diagnostic data rather than erasing a
  valid identity. Malformed or unsafe XML does not become a trusted match.
- Lettered issue numbers remain distinct, including when their numeric portions
  are equal. A missing Number can match only through verified exact issue IDs.
- Execution rechecks provider links. Existing source-signature, archive safety,
  ownership, collision, transfer, and rollback handling still applies.
- Mylar matching and ComicVine-primary paired archives keep their existing path.
- There is no database migration or new production default/feature flag.

## Native Issue Choices

Known Metron/GCD series now offer **Choose issue** for unresolved files in Step 3.
The existing inline picker searches the saved, verified local catalog with a
10-row viewport and 25-issue pages. Lettered numbers remain distinct. Opening,
searching, paging, and applying a choice make no metadata-provider requests.

This is useful after correcting a provider link or refreshing a catalog while
an import is already in review: the user can confirm the now-proven issue for
one file without rematching its neighbors. It is not permission to override
contradictory XML or guess the identity of an untagged file. Each eligible
choice must agree with the file's exact identities and embedded issue number;
other choices stay disabled with an explanation. **Review linked metadata**,
**Recheck linked metadata**, and **Skip this file** remain available.

Choices are actor/file/issue scoped and expire. Changed source evidence, series
links, issue links, or displayed issue identity invalidates the preview. Execution
revalidates links and source signatures again. Owned files are not replaced, and
two staged files targeting one unowned issue become an explicit copy conflict.
The old ComicVine-only assignment path cannot bypass native identity checks.

Local acceptance import #51 used the existing generated Metron and GCD archives.
Only their test issue links were briefly marked stale during scan, then restored
before opening the picker. Both choices imported successfully and rollback
restored Skipped. All three fixture hashes and all 1,088 pre-existing library-file
records remained unchanged. No production service was deployed.

Validation passed `make validate` (16,611 passed, 1,778 skipped, one expected
failure) and 92 focused native/legacy import regressions. Browser checks cover
both native catalogs in all three themes, including pagination, search,
selection, accessibility, and preservation of the review shell. The combined
Chromium run passed all 15 checks. Firefox passed all 15 checks on rerun; an earlier run during concurrent
validation hit an intermittent collapsed-row failure in the existing GCD
preview test. Its cause is not established. PostgreSQL remains unqualified by
these local results.

## Explicit Library Selection

An unmatched folder group without native series evidence now offers **Choose
library series** next to the existing ComicVine search. The dialog searches
already-added series with verified Metron or GCD links, in pages of 25. It does
not contact a provider or add a series. Stale links and ComicVine-only series
are not presented as native choices.

Choosing a library series leaves its files unresolved and unselected. Expand
the row and use **Choose issue** to confirm each file against the saved catalog.
This supplies missing identity only: conflicting issue numbers, exact provider
IDs, or malformed identity metadata still block the choice. Selecting a series
does not rewrite the archive or turn the user's decision into embedded XML
evidence. Ready rows say **Library selection (Metron)** or **Library selection
(GCD)** rather than claiming an automatic identity match.

The saved file decision binds the source evidence, issue, and verified links.
Rematching retains a still-valid explicit decision but does not guess unchosen
neighbors, even when their issue number agrees. Execution revalidates the
decision through the existing ownership, collision, safety, and rollback paths.
Mixed-file issue reassignment across series remains outside this checkpoint.

The series-choice dialog stays open with confirmation disabled until the review
refresh finishes, so a slower response cannot expose the old row controls. A browser
regression observes the dialog while that response is pending. The shared
review shell remains mounted. Other file-action dialogs use the same refresh
ordering; inline issue choices remain inline.

Coverage includes Metron/GCD catalogs, untagged and ComicInfo-only archives,
lettered issues, stale/contradictory identities, CSRF, actor-bound previews,
rematching, unchosen neighbors, source-preserving import and rollback, paged
library search, all three themes, and automated accessibility checks. Local
browser acceptance for the generated untagged archive is staged as import #52;
it is not yet recorded as completed. PostgreSQL and broader provider-aware
import/recovery qualification remain separate release requirements.

Validation for this checkpoint passed `make validate`: 16,627 tests passed,
1,778 were skipped, and one was an expected failure. The focused selection
suite passed 15 cases. The browser suite includes native preview/add, explicit
library selection, native issue choice, and shared review-workspace regressions;
all 29 cases passed in each of Chromium and Firefox. The suites ran serially
after the broader validation run.

### Find A Missing Library Series

**Choose library series** now includes **Search Metron / GCD**. This is an
explicit search using the shared Add Series discovery, configured source
priority, cache, availability and backoff behavior. Opening the dialog does
not contact providers. ComicVine search remains on its existing review path.

Existing verified results offer **Use series**, with the same actor-bound
review token and explicit per-file choices. Missing results offer **Preview &
add**, using the standard catalog preview and Add Series API. The dialog
states that this creates an independent library record: cancelling or rolling
back the import will not remove it, and adding does not select or import files.
**Choose added series** opens only that exact saved record for confirmation,
not a title search that could select a different same-name series.

Search results are paginated, both chooser bodies stay within the viewport,
and cancelled searches cannot reopen the dialog with a late response. Cancelling
preview by button or Escape returns focus to its result after the search panel
becomes visible, including in Firefox. On mobile, the title input occupies its
own full-width row instead of shrinking beside the source selector.
The review shell remains mounted, and
ready counts remain unchanged until a file receives an explicit issue decision.
Source and issue proofs are still revalidated through the existing import and
rollback services; no archive-writing or recovery rules changed.

Qualification includes real catalog adoption through the shared API followed by
explicit selection, import and rollback, source-byte preservation, stale review
rejection before and after provider I/O, search source restrictions, exact-ID
return, cancellation, all three themes and browser accessibility checks.
Live provider acceptance on local 8585 remains separate from these deterministic
tests. This checkpoint does not complete broader Mylar/native, sidecar or
PostgreSQL qualification.

Checkpoint validation passed `make validate`: 16,634 passed, 1,778 skipped and
one expected failure. The combined search/add, library selection, native issue
choice, shared review workspace and Add Series browser suite passed 57 checks
in each of Chromium and Firefox, including accessibility and mobile layout.
Browsers ran separately and serially with the broader validation gate. Provider
fixture IDs do not pre-create catalogs expected to be missing by other tests.

The earlier dropdown contract violation is corrected. An initial reader
RSS-bound failure did not recur in either subsequent full validation run; its
limit was not changed, and no reader fix is claimed. Live provider acceptance
for this dialog and PostgreSQL qualification remain pending.

## Compiled Series Sidecars

Folder scans now read the version-1 compiled block produced by Pullbox's
`series.json` writer. Its Metron/GCD series claims must resolve to existing,
verified local identities, including every additional exact series identity
in the sidecar or file. Copied links, provenance, and observed identities do not
create verified library links. Reads are bounded to 1 MiB; malformed, oversized,
unsupported, or contradictory evidence remains in review with rescan/skip guidance.

A sidecar identifies the series, not every issue in its folder. A file with
its own exact issue identity may match automatically, including an issue linked
through another provider when local verified records prove the same target.
The legacy ComicVine issue column is supported. Conflicting embedded issue
numbers remain blocked; absent numbers require exact identity evidence.

Files without a proven issue offer **Choose issue** against the saved native
catalog. Only an explicit choice makes that file ready. Unchosen neighbors stay
unresolved even if their filenames resemble an issue. Ready rows identify
`series.json (Metron)` or `series.json (GCD)` as the series evidence. Rechecks and
issue choices make no provider requests; execution revalidates the reviewed
evidence and verified links before the usual file-safety and ownership checks.

This extends filesystem imports, not the trusted Mylar path. The compatibility
suite retains Mylar in-place, split-series, source-provenance and legacy metadata
tests. Qualification covers both native series providers, cross-provider exact
issue IDs, lettered/missing numbers, mixed-folder conflicts, stale links before
and after confirmation, owned-file rescans, preserved sources and rollback.
PostgreSQL parity and broader Mylar/native recovery remain separate requirements.

The local lab has three separate synthetic scenarios under `sources/compiled-sidecar`:
series-only Metron evidence needing per-file choice, exact GCD issue evidence
with its series supplied by the sidecar, and a conflicting issue number. They
do not alter the existing review job. Finish or cancel that review before
starting a new scenario, and roll back successful synthetic imports afterward.

The combined review, native search/add and series-choice browser suite passed
55 checks in each of Chromium and Firefox, including all three themes and
accessibility. The tab color fade briefly dropped label contrast below 4.5:1;
review tabs now switch foreground and background together. A controlled pending
review refresh also preserved an expanded row. Older browser tests now wait for
the actual lane response before operating its controls, rather than racing the
previous lane's DOM. The focused sidecar safety suite passed 54 cases.

The first broad validation run passed 16,687 tests but failed the existing reader
RSS limit (41 MiB growth against the unchanged 32 MiB bound). Its other resource
bounds held; the complete reader-content suite then passed 11 checks alone with
144 KiB RSS growth. This intermittent full-suite failure remains unresolved;
neither a raised limit nor a reader fix is part of this checkpoint.
The subsequent unchanged `make validate` run passed CSS build, lint, formatting,
strict typing and 16,688 tests, with 1,778 skipped and one expected failure.
Both runs are retained in the local evidence directory; this does not qualify
PostgreSQL or replace live acceptance of the staged fixtures.

## Rollback Status

New file-registration journal entries record the issue's status and manual-skip
choice immediately before registration. After safely reversing registration,
rollback restores that status only if the issue still has the import's Owned
state, its manual-skip choice is unchanged, and no library file remains. A later
user choice or surviving replacement file is not overwritten. Edited-file
preservation and existing source/ownership checks still run first.

For a series created by the import, its rollback guard recognizes this recorded
status restoration without treating it as a later user edit. All other series
ownership and activity checks remain in place. Historical journal entries with
no previous-status evidence do not guess the original status.

## Local Browser Checkpoint

On November 20, 2026, the signed-in development instance on port 8585 completed
the following checks using generated two-page CBZs and the existing verified
Metron-only identity for Devil's Reign #1:

- Conflicting ComicInfo/MetronInfo issue numbers remained blocked in Step 3.
- The valid file displayed its Metron identity, could be selected, and imported.
- A rescan recognized the owned issue and excluded it from another import.
- Rollback removed the generated library copy and restored Skipped without a
  separate status repair. Both original fixture hashes stayed unchanged.
- The 1,088 pre-existing library-file records were unchanged after rollback.

Fixtures and run evidence are recorded in the local
`pullbox-metron-folder-import-lab` directory. This is a local acceptance check,
not a production deployment or PostgreSQL qualification.

The Step 4 counter correction separates files from newly added series: Files
imported, Files failed, Series added, and Series failed use their respective
durable totals. The footer uses file totals too. The old inferred Remaining /
Follow-up count is removed; Step 5 continues to report actual follow-up outcomes.
Import execution, progress calculation, and recovery decisions are unchanged.

Browser import #47 added one generated file to the existing Metron-only series.
Step 4 and its footer showed one file imported, zero failed files, and zero added
or failed series, including after refresh. Step 5 agreed. Rollback reset the
file totals to zero and restored Skipped; source hashes and all 1,088 original
library-file records remained unchanged. Browser regressions also cover new
series, mixed file outcomes, partial live updates, polling, and rehydration.

Browser import #48 exercised the new in-review Add path against Metron's real
Superman: Red Son (2003) catalog (series 13131, issue 152712), using another
clearly labeled generated archive. The series was initially absent. Preview/Add
created the series and its three issues, rechecked the same job, and left the
file unselected. Explicit selection imported one file. Step 4 and Step 5 agreed
on one imported file and zero series added by import execution. Rollback removed
the generated library copy, restored Skipped, and retained the independently
added series. The source hash and every original library-file row were unchanged.

Chromium and Firefox regressions cover cancellation, late preview responses,
exact add payloads, in-place review updates, no implicit selection, and light,
dark, and Tron themes. The modal also passes the automated accessibility check.

## GCD Exact-Identity Extension

GCD-primary `MetronInfo.xml` archives now use the same local exact-identity path.
Both `GCD` and `Grand Comics Database` source names are accepted by the existing
XML reader. The file must supply its own native series and issue identities,
and both must agree with verified links already saved in Pullbox. ComicVine IDs
are not required. Matching and rechecking do not contact a provider.

Step 3 labels these rows **GCD identity**. For an unknown series, **Preview GCD
series** opens the shared Add Series dialog without leaving review. It selects
an enabled, configured GCD source in core-metadata priority order; GCD API remains
excluded unless its feature flag is enabled. Preparing the preview context does
not contact a provider. The shared preview and final Add retain catalog and
revision validation. If no GCD source is configured, the dialog directs the user
to Metadata settings. Adding queues the same-job local recheck; neither action
selects or imports files. Conflicting identities cannot be resolved by priority.

Integration coverage exercises numeric and lettered issue numbers, absent
Number with exact identity proof, conflicting and stale links, already-owned
files, final placement revalidation, source preservation, and rollback. Browser
coverage checks GCD labels, cancellation, late responses, explicit Add and
same-job rechecking in all three themes.

Local browser import #49 used a generated two-page archive with only GCD series
15002 / issue 258869, Superman: Secret Identity (2004) #1. The official local GCD
dump supplied the preview and all four canonical issues. Add created local series
296 with no ComicVine ID, then rechecked the same job without selecting files.
Explicit selection imported one file. Step 4 and Step 5 agreed on one imported
file and zero series added by import execution. Rollback removed the generated
library copy, restored Skipped, and retained the separately added series and its
four issues. The source SHA-256 and all 1,088 pre-existing library-file rows were
unchanged. GCD API and automatic acquisition remained disabled.

## Remaining Scope

This is not the complete provider-aware importer. Compiled sidecar import
identity, broader provider-neutral mixed-folder recovery, and issue
reassignment/recovery remain separate milestones.
This checkpoint targets CBZ folder imports, not additional archive formats.
It does not qualify PostgreSQL or change Anna's verification work.

Regression coverage is in `tests/integration/test_metron_folder_import.py`,
`tests/integration/test_gcd_folder_import.py`,
`tests/integration/test_native_mixed_folder_import.py`,
`tests/api/test_gcd_import_preview.py`,
`tests/integration/test_metron_import_recheck.py`,
`tests/api/test_source_series_add_api.py`, `tests/e2e/test_metron_import_review.py`,
`tests/unit/test_import_scan_materialization.py`, and
`tests/ui/test_import_review_v2.py`, alongside existing import lifecycle,
matching, paging, recheck, and archive-safety tests.

## Native Mixed-Folder Review

Folder scans now separate native identity groups within a physical folder before
matching. Each group retains all of its own series identities, and each file
still needs its own verified issue identity. Unknown native groups can use the
existing in-review Preview/Add workflow independently. Untagged neighbors stay
under **No series matched**, with the existing series search and Skip available;
another file's tag cannot authorize their import. Conflicting cross-identities stay separate
from clean files rather than blocking every file in the folder.

This uses already-inspected XML evidence, not another archive read or provider
request. File paths, source signatures, source ordinals, folder cohorts, safety
blocks, and explicit layout-review requirements are retained. A mixed folder's
aggregate ComicVine hint does not identify its native subgroups; their own
embedded cross-identities must still agree with the local verified links.
Mylar and folders without native identity evidence keep their existing paths.
Scan totals count the resulting review groups across incremental batches.

Local browser import #50 tested a single folder containing generated Metron
Superman: Red Son #1 and GCD Superman: Secret Identity #1 archives plus an
untagged neighbor. The two native groups appeared independently under Ready,
unselected. Explicit selection imported two files, with zero newly added series
and one file left in Follow-up. Rollback restored both issues to Skipped,
removed only the generated library copies, and left all three source hashes and
the 1,088 pre-existing library-file records unchanged.

A review-only rescan verified the untagged row's **No series matched** label and
Skip action. Skipping it completed one decision without selecting or removing
either Ready group. Cancelling that test left no active import.

Validation for this checkpoint passed `make validate` (16,597 passed, 1,778
skipped, one expected failure), the focused native/Mylar/split-series regressions,
and nine existing provider-review browser tests in each of Chromium and Firefox.
The skipped PostgreSQL cases are not PostgreSQL qualification evidence.
