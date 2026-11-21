# Native Provider Folder Import Checkpoint

## Available Behavior

Folder imports can match a comic archive's primary Metron series and issue IDs to verified
identities already saved in Pullbox. The local series and issue do not need
ComicVine IDs. Matching uses saved identities, not title similarity or a live
provider request, and does not create or attach provider links.

### Non-ZIP Archive Evidence

CBR, CB7, and CBT scans and source rechecks now retain the same normalized
Metron/GCD evidence as CBZ scans. The import content inspector uses the existing
bounded paired-XML reader after the ordinary archive safety gate. Metadata member
limits, the configured solid-archive scan budget, unsafe-member rejection, and
explicit safety review remain in force. ZIP scans reuse their existing report;
this does not add a second metadata read to ordinary CBZ imports.

Deferred ComicInfo loading preserves the saved native identity evidence instead
of discarding it. Recorded Mylar matches remain valid when native IDs are unknown
or unverified. Only disagreements proven by verified saved identities block the
file. No provider requests or new links are created by scanning or rechecking.

The native-identity correction is import-only, not an archive backend rewrite. It does not
change conversion settings, metadata-writing policy, downloads, rollback rules,
or archive safety limits. Real stored RAR, 7z, and TAR fixtures cover copy and
keep-in-place import, rollback, Mylar conflicts, fresh source rechecks, ambiguous
XML, unsafe content, and the configured read budget. Compressed RAR continues to
require the existing UnRAR runtime backend.

Qualification: 34 initial behavior assertions reproduced missing evidence before
the correction. The expanded regression set passes 439 cases, including 50
non-ZIP cases and the existing Mylar/import lifecycle suites. Focused Ruff and
strict mypy pass. This is not full CI or PostgreSQL release qualification.

### Converted Native Archive Qualification

Local browser qualification on November 21 exposed one downstream conversion
gap: a correctly matched CBT failed with `Unsupported source format: .cbt` when
the existing import policy required CBZ normalization. The shared converter now
streams regular TAR entries into a staged CBZ, preserving page and XML bytes.
It rejects unsafe paths, duplicate names, links, special files and sparse entries.
Publication uses the existing exclusive destination claim, so cancellation,
truncation and a competing destination do not publish a partial or replacement
file. No import setting, provider-link rule or rollback policy changed.

All six Metron/GCD CBR, CB7 and CBT browser cases then imported one explicitly
selected file with zero new series, no failed files and no metadata follow-up.
Both XML files reconciled without differences. Every generated copy was rolled
back through the UI. The 12 old/new fixture hashes, all 1,088 original library-file
rows, 4,600 provider identities, both roots and all 4,202 issue statuses remained
unchanged. CBR conversion was verified with the existing container UnRAR backend.

The identity-only negative fixtures remain unchanged. They intentionally lack a
required MetronInfo Series/Name field: matching can succeed, but paired writing
must still request metadata review. Separate schema-complete fixtures exercise
the successful writing path rather than weakening XML validation.

Qualification: 270 focused converter, paired-writer and native/legacy import
regressions passed, along with focused Ruff and strict mypy. This is not full CI
or PostgreSQL qualification. A transient browser-history restoration issue seen
while returning from an issue page to completed-import follow-up remains a
separate reproduction task; these successful direct import/rollback runs do not
establish that history navigation is fixed.

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
2. Choose an unowned issue and a CBZ, CBR, CB7 or CBT containing `MetronInfo.xml` whose primary
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

- ZIP metadata uses the existing bounded inspection report; non-ZIP metadata
  uses the bounded paired-XML reader after archive safety inspection. Neither
  trusts archive-controlled extraction paths.
- Every automatically matched file needs its own exact identity. A tagged file
  does not authorize untagged neighbors in a mixed folder.
- Competing exact identities and issue-number disagreements block the match.
  Unknown descriptive XML fields remain diagnostic data rather than erasing a
  valid identity. Malformed or unsafe XML does not become a trusted match.
- Lettered issue numbers remain distinct, including when their numeric portions
  are equal. A missing Number can match only through verified exact issue IDs.
- Execution rechecks provider links. Existing source-signature, archive safety,
  ownership, collision, transfer, and rollback handling still applies.
- Recorded Mylar issues keep their existing path, with the proven-conflict guard
  below also checking supplemental native IDs in ComicVine-primary paired XML.
  Unrecorded native Mylar extras use the bounded workflow below.
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

### Completed-Import Follow-up

For completed folder imports, **Find series** now opens the same saved-library
chooser used in Step 3. Select an existing verified Metron/GCD series, or use
**Search Metron / GCD** to preview and add a missing one. Provider calls happen
only on explicit search/preview/add, not when opening the library chooser.
Adding a series remains independent of import rollback.

Selecting the series opens its existing recovery dialog without reloading the
page. Choose each issue explicitly, or skip it, then **Save recovery choices**.
Selection alone does not match, select, import, or modify any comic. The group
stays in Follow-up until its files are addressed. Cancel preserves its current
decisions and returns focus to the row's Find series control.

Series choices bind the actor, source evidence, verified catalog links and import
revision. Archived, busy, rolled-back or changed groups cannot use an old choice.
Groups with existing file decisions cannot be reassigned. An exact embedded
native identity may only select its verified owner, not an unrelated search hit.
The existing signed issue choices, owned-file protection, competing-copy review,
source signature checks, and rollback behavior still apply at recovery.

Untagged groups retain **Search ComicVine**. Switching an undecided group between
a native library selection and ComicVine clears the previous selection rather
than mixing their catalogs. The transition rechecks durable state after provider
I/O and refuses a group changed by another request. Embedded native claims cannot
be overridden through the legacy ComicVine assignment API. Recorded Mylar issues
continue to use their existing ComicVine recovery route. Only separately staged
native extras use the native recovery workflow described below.

To test: finish a folder import with an unidentified group left for Follow-up,
open Find series, search/add or choose its native library series, and confirm
individual issue choices. Cancel must import nothing. Successful recovery must
preserve source bytes; rolling it back must preserve independently added series.

Checkpoint validation: 144 focused native and legacy import checks passed.
Chromium and Firefox each passed 37 browser checks with one existing
fixture-dependent skip. The new Follow-up checks cover both native catalogs,
existing-series selection, search/add, cancellation, focus, accessibility, and
recovery without a page reload. Provider responses are controlled test fixtures;
these results do not qualify live provider availability or PostgreSQL.

### Unrecorded Native Mylar Extras

Mylar imports can stage separate Metron/GCD groups for archives present in a
source folder but absent from Mylar's issue records. Each automatically matched
archive must carry its own exact issue identity. A compiled `series.json` can
supply its series identity; a sidecar without per-file issue evidence requires
an explicit **Choose issue** decision. Recorded or reconciled Mylar references
remain in the original Mylar group, as do untagged neighbors without a native
sidecar. The source database is not edited.

The native groups use the existing local identity checks, review, explicit file
selection, and import paths in both copy and keep-in-place modes. The original
Mylar folder's owner does not determine an extra archive's identity. Its original
folder-owner diagnostic is retained, but it does not override a separately proven
extra. Explicit sidecar claims must still agree with its embedded identities.
Stale links, unknown issue IDs, contradictory numbers, and already-owned issues cannot become
ready solely because a filename or neighboring file matches.

Scanning reports the resulting review-group count, which can exceed the number
of source series recorded in Mylar. A native group for an existing library series
with unresolved files remains in completed-import Follow-up with **Match issues**.
Its row shows the identified series while retaining the physical source folder
in the source column. Closing recovery makes no changes; saving still requires
eligible, explicit issue choices. Import and rollback preserve the original
comics and Mylar database.

This checkpoint does not replace recorded Mylar matching or add live provider
calls during scanning. The existing bounded sidecar reader is reused once per
source folder. Broader recorded Mylar cross-provider recovery and PostgreSQL
qualification remain separate work.

Checkpoint validation: 412 focused import/recovery and shared-shell checks
passed. Chromium and Firefox each passed 61 browser checks covering native
Mylar extras, filesystem/Mylar sidecar choices, Metron/GCD, all three themes,
accessibility, cancellation, recovery, and activity/sidebar behavior. The tests
use synthetic archives and local identities, not live provider availability.

Local browser acceptance imported a recorded Mylar issue alongside separate
Metron and GCD extras into three existing series. A second run used a compiled
sidecar and an explicit issue choice, importing one file while leaving its
neighbor unresolved. Both imports rolled back through the UI, restoring prior
issue statuses and preserving source bytes, existing library records, and all
provider links. Selection controls use the displayed matched series title,
not the original mixed folder's name.
That label correction passed 130 focused UI checks and 12 sidecar browser cases
each in Chromium and Firefox, retaining the source-name fallback for unmatched
titles and the existing selection behavior.

The local lab's nested test root required a temporary, approved disable during
these Mylar checks and was restored afterward. No overlap validation was
bypassed, root path rebound, or existing file moved. The standalone exact-GCD
sidecar and contradictory-number lab fixtures retain automated coverage but
have not yet been exercised in this live Mylar browser session.

### Recorded Mylar Identity Disagreements

A recorded Mylar file remains in its Mylar group. If an embedded Metron/GCD
series or issue ID has a verified saved owner that disagrees with its proposed
target, the file stays unresolved and unselected. This also covers supplemental
native issue IDs when ComicVine is the primary embedded provider. Unknown or
stale native links remain diagnostic evidence rather than invalidating an
otherwise valid Mylar match. No provider links are created and no provider
requests are made by these checks.

Expand the **Some files need an issue match** row to review the specific saved
records, correct linked metadata or the source file, and choose **Recheck
source**. Recheck reads fresh bounded archive evidence and runs the existing
local matching path. An unchanged contradiction remains unresolved; correcting
it does not move the file to another series or alter Mylar's database. **Skip
this file** is always available. The legacy issue-assignment action cannot
override a recorded conflict without a recheck. Execution checks the saved
owners again before placement, including links changed after Step 3.

Step 3 source rechecks retain existing access boundaries: a keep-in-place source
must remain under its approved reference root, and mapped sources must remain
under their saved mappings. A confirmed identity-only Mylar copy import can
recheck the exact resolved path in its complete saved scan signature, including
an explicitly corrected replacement at that same path. This grants no directory
root or permission to inspect a redirected path. Missing scan proof, symlink
redirection, sensitive paths and revoked confirmation remain blocked. A changed
extension/path still requires an approved mapping or a new scan. Completed-import
bulk recovery retains its existing explicit-root requirement; this change is
limited to the individual Step 3 Recheck action.

Tests cover agreeing, unknown, stale and proven conflicting identities, both
providers, copy/in-place import and rollback, changing links before placement,
same-job rechecks after file/link correction, and explicit skip. Browser checks
exercise recheck and skip in Light, Dark and Follow system with the review shell retained.
The zero-matched count uses a scoped contrast correction for hovered dark rows.

Checkpoint validation: 371 focused integration, route and import/recovery
regressions passed. Chromium and Firefox each passed 12 browser cases covering
both providers, those theme preferences, recheck/skip and accessibility. Scoped strict
typing, repository lint/format checks and the generated CSS build passed.
This is not full CI or PostgreSQL qualification.

The identity-only Step 3 recheck follow-up passed 385 focused import/recovery
checks and 24 browser cases in each of Chromium and Firefox, including mapped
and untranslated paths. Negative coverage keeps missing proof, symlink/path
redirection, sensitive sources, disabled confirmation and new dangerous archive
content blocked. Rechecking a corrected source does not itself approve its
identity: matching still has to confirm it.

### Step 3 Selection

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

For a series with verified links to both native providers, the issue picker
includes its verified Metron and GCD issue records even when the selected series
label names the other provider. Local acceptance exposed this with Superman:
Red Son: the library chooser used its GCD link, while its saved issues were
linked through Metron. The old namespace-only list was empty. The shared
per-file identity/number checks and stale-link revalidation still gate choices
and placement; this does not merge catalogs or create provider links.
The correction passed 96 focused import checks and 12 browser checks in each
of Chromium and Firefox, plus lint, formatting and strict typing. The browser
cases cover both cross-provider directions in all three themes. The preceding
full validation gate is recorded under Compiled Series Sidecars; it predates
this narrow picker correction.

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
browser acceptance for the generated untagged archive completed as import #52,
including the cross-catalog correction above, import and rollback. PostgreSQL
and broader provider-aware import/recovery qualification remain separate release
requirements.

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
do not alter other review jobs. Finish or cancel an active review before
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

Local 8585 acceptance then completed on `66c533e3` using the existing review
#52 and new sidecar-backed import #53. The first explicitly chose Superman:
Red Son #1 from its Metron issue catalog after selecting the library series
through GCD. The second matched Superman: Secret Identity #1 through its compiled
GCD series sidecar and embedded GCD issue ID, with no ComicVine identity required.
Both required explicit user action, imported one generated file, and displayed
one imported file and zero added series in Step 4 and results. Browser rollback
restored both issues to Skipped. No direct status repair was used.

All 1,088 pre-existing library-file rows, all series/issue provider-link rows,
and all fixture hashes remained unchanged after rollback. No import remains
active. Local 8585 is healthy; schema `i6c7d8e9f012` did not change. The home-server
and parked verification work were untouched. Live sidecar conflict/series-only
scenarios remain available for user testing; their automated coverage passed.

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

This is not the complete provider-aware importer. Compiled sidecar recognition,
unrecorded native Mylar groups, exact native Follow-up rechecks, and explicit
native issue choices in Step 3 and Follow-up are implemented. Missing-series
search/add also preserves those explicit decisions.

Recorded Mylar cross-provider reconciliation, broader source/safety retry
qualification, and additional archive formats remain separate milestones. The
current live acceptance uses synthetic CBZ files and verified local catalogs;
it does not qualify PostgreSQL or change Anna's verification work.

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

## Native Follow-up Recheck

After a folder import completes, **Follow-up > Recheck files** now recognizes
exact Metron/GCD identities using the verified library records available at the
time of the recheck. This also covers compiled sidecars with an exact per-file
issue identity. A series added or linked after the original import can therefore
be recovered without rescanning the entire source or guessing a ComicVine match.
Adding or correcting the library link is still a separate, explicit operation.

The existing signed preview, confirmation, background progress, cancellation,
and import settings are reused. Selected files enter the existing-series import
path, retaining their native match method so placement revalidates current links
and source signatures. No new provider calls, provider links, migrations, or
default settings are introduced. Original metadata evidence is retained, and
ordinary ownership, source preservation, and rollback safeguards still apply.

Native groups cannot fall through to the legacy filename or ComicVine-catalog
recovery path. Unverified identities, conflicting issue designations, malformed
metadata, untagged neighbors, and competing physical copies remain unresolved.
Already-registered sources are recognized only with matching source evidence;
an alternate copy of an owned issue remains a conflict rather than replacing it.
Explicit series/issue choices that lack automatic exact identity proof are not
silently inferred by this action. The existing Mylar recovery path is unchanged.

Manual native issue choices are a separate action described below. A failed exact
recheck is not evidence that the file should be forced into a match. PostgreSQL
and combined release qualification also remain incomplete.

The focused browser checkpoint passes six cases each in Chromium and Firefox:
Metron and GCD across light, dark, and Tron themes. It covers the authenticated
preview, cancellation, confirmation, saved execution results, and Step 4's
one imported file / zero new series counters. Accessibility checks cover the
confirmation dialog. A separate full-page check exposed an existing header
activity badge `label-content-name-mismatch`; that shared-header finding is not
fixed or claimed as passing by this checkpoint. The focused regression suite
passes 258 tests covering native imports, deferred recovery, mixed folders,
Mylar keep-in-place behavior, cancellation, ownership, and rollback. Ruff and
focused strict mypy also pass. No full CI, public release, or production
deployment is implied.

## Manual Native Follow-up Choices

For completed folder imports with a verified or explicitly selected native
library series, **Follow-up > Match issues > Choose issue** now uses that series'
saved Metron/GCD catalog. Search and 25-item pagination stay inside the recovery
dialog, with a ten-row scrolling issue list. The dialog body scrolls while its
Save and Close actions remain visible. A failed initial list request offers Retry
or Skip rather than leaving the loading message indefinitely.

Selecting **Use this** only stages the choice in the dialog. **Save recovery
choices** submits all remaining decisions to the existing background recovery
worker. Files may also all be skipped without inventing an issue match. No
provider calls, catalog changes, or metadata-link creation are performed by the
picker or the recovery action.

Choices are signed for the authenticated user and current file, series, issue,
and provider-link evidence. Recovery validates the whole decision set before
applying it. Changed or conflicting identities are rejected; source signatures
are rechecked at placement. Owned copies remain untouched, competing copies
remain conflicts, and native groups containing already-owned or duplicate-file
records do not reopen after their remaining decisions are completed. Existing
ComicVine/Mylar recovery and Step 3 selection continue through their existing
paths. Import settings, source preservation, and rollback still apply.

This does not yet add a native series-selection workflow to completed-import
groups that have no identified library series. Those groups must not be coerced
into a ComicVine match. Broader provider-aware Mylar matching, PostgreSQL
qualification, and combined v2 release qualification remain separate work.

Regression-first coverage includes signed choices, user mismatch, stale links,
changed source bytes, locked files, duplicate decisions, all-skip, owned copies,
competing copies, resolved neighbors, and rollback. The focused integration,
service, API, task, UI-route, and Mylar regression set passes 203 tests. Browser
coverage exercises Metron/GCD in light, dark, and Tron themes, including search,
pagination, retry, unsaved-choice isolation, background completion, and source
preservation. Accessibility checks cover the recovery dialog, not the unrelated
shared-header finding noted above. Chromium and Firefox each pass 17 focused
browser cases, including the existing Step 3 picker and Follow-up navigation;
each run skips one older recovery-row test because its baseline fixture has no
recovery-pending row. The new native workflow has no skipped cases. Mobile checks
keep the dialog and Save action inside the viewport. Ruff, focused strict mypy,
JavaScript syntax, and the stylesheet build pass. No full CI or production
deployment is implied.
