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
confirmation. Mixed series IDs cannot use this shortcut, and conflicting issue
evidence remains in review after the series has been added.

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

## Remaining Scope

### GCD Exact-Identity Extension

GCD-primary `MetronInfo.xml` archives now use the same local exact-identity path.
Both `GCD` and `Grand Comics Database` source names are accepted by the existing
XML reader. The file must supply its own native series and issue identities,
and both must agree with verified links already saved in Pullbox. ComicVine IDs
are not required. Matching and rechecking do not contact a provider.

Step 3 labels these rows **GCD identity**. For an unknown series, **Add or link
GCD series** opens the existing Add Series page in a separate tab. After adding
or correcting the linked catalog, return and use **Recheck linked metadata**.
Rechecking does not select or import files. The inline exact GCD preview/add
shortcut is not part of this checkpoint.

Integration coverage exercises numeric and lettered issue numbers, absent
Number with exact identity proof, conflicting and stale links, already-owned
files, final placement revalidation, source preservation, and rollback. Browser
coverage checks GCD labels and same-job rechecking in all three themes. Live
GCD import acceptance testing remains separate from the completed Metron
browser checks above.

This is not the complete provider-aware importer. Inline GCD preview/add,
automatic split-folder regrouping, and provider-neutral issue
reassignment/recovery remain separate milestones.
This checkpoint targets CBZ folder imports, not additional archive formats.
It does not qualify PostgreSQL or change Anna's verification work.

Regression coverage is in `tests/integration/test_metron_folder_import.py`,
`tests/integration/test_gcd_folder_import.py`,
`tests/integration/test_metron_import_recheck.py`,
`tests/api/test_source_series_add_api.py`, `tests/e2e/test_metron_import_review.py`,
`tests/unit/test_import_scan_materialization.py`, and
`tests/ui/test_import_review_v2.py`, alongside existing import lifecycle,
matching, paging, recheck, and archive-safety tests.
