# Metron Folder Import Checkpoint

## Available Behavior

Folder imports can match a CBZ's primary Metron series and issue IDs to verified
identities already saved in Pullbox. The local series and issue do not need
ComicVine IDs. Matching uses saved identities, not title similarity or a live
provider request, and does not create or attach provider links.

Step 3 shows the series under Ready with a Metron identity label. Since this is
an existing series, use its ready-file selection controls to add missing issues.
Already-owned issues remain excluded. An unverified identity stays in review,
with guidance to add/link the series and scan again, or skip it for now.

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
   Correct the source/link and scan again, or skip the file/series.

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

## Remaining Scope

This is not the complete provider-aware importer. Creating a new Metron series
from the import review screen, GCD matching, automatic split-folder regrouping,
and provider-neutral issue reassignment/recovery remain separate milestones.
This checkpoint targets CBZ folder imports, not additional archive formats.
It does not qualify PostgreSQL or change Anna's verification work.

Regression coverage is in `tests/integration/test_metron_folder_import.py`,
`tests/unit/test_import_scan_materialization.py`, and
`tests/ui/test_import_review_v2.py`, alongside existing import lifecycle,
matching, paging, recheck, and archive-safety tests.
