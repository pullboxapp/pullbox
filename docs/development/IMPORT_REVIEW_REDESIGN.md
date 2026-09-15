# Import Review Redesign

Status: redesign implemented for local acceptance; release gates remain below.

## Boundary

Step 3 becomes a decision-oriented review without replacing the import engine.
The established matching, source ownership, safety, and recovery rules remain
authoritative. The design prototype is a presentation reference, not permission
to replace those rules with weaker heuristics.

This redesign does not migrate the database or rewrite saved jobs on page load.
It does not change Step 4 execution, Step 5 receipts, job-specific Follow-up,
completed-import recovery, library organization, or Story Arc handling.

## Implemented

- Six mutually exclusive series lanes: Needs a decision, Review suggestions,
  Fix source, Cannot import, Ready, and Info. Secondary reasons remain attached
  to the same row; readiness is counted separately from unresolved files.
- Compact review table with labeled actions, inline candidate acceptance,
  immediate duplicate-copy decisions, issue reconciliation, signed safety
  previews, and existing individual duplicate-series file selection.
- The import gate reports selected files and remaining follow-up using canonical
  saved selection state rather than browser storage.
- Matched files in a series can be selected even when other files in that series
  remain in conflict. Existing confirmation still imports only eligible files.
- Conflict suggestions distinguish differing parsed titles or years from copies
  of the same comic. Differing identities have no recommended keeper. Older
  saved groups are interpreted without rewriting their diagnostics.
- Missing-source references are presented as information when the saved reason
  proves absence, while unreadable roots and unsafe content remain separate.
- Review updates retain the root controller, header controls, focus, expansion,
  and scroll position. Candidate rematching uses the existing completion poll.
- Source details, matched issue IDs, Mylar diagnostics, and safety codes remain
  available in expandable details.

## Completed Decision Paths

1. Ready files have individual persisted checkboxes for both new and duplicate
   series. An explicit diagnostics marker preserves legacy matched-file defaults.
   Confirmation imports only selected files. Excluded files retain target evidence
   in Follow-up, where a subsequent explicit assignment re-enables their import.
2. Mixed-comic conflicts offer Find correct issue for each file. Series search
   leads to a shared issue dropdown. The provider's issue list validates ownership;
   exact embedded identity cannot be contradicted. Reassignment changes only the
   staged file target, not source files or sibling choices. A collision creates a
   copy decision instead of silently selecting a keeper or overwriting ownership.
3. Recheck source queues one durable background inspection. It keeps the existing
   approved-root and safety rules, revalidates the saved review, and resumes across
   a restart between inspection and matching. Errors terminate progress and leave
   evidence for another decision. Unknown/dangerous content cannot be overridden.
4. A stale Mylar reference can be paired with one independently identified actual
   file in the same folder. Signed previews bind actor, file and candidate scope.
   Verification requires exact IDs, absence of the old path, an unchanged actual
   file and fresh safety checks. Resolving the redundant reference is recorded as
   verified pairing, not a guessed match or an unlabeled Skip. Sources stay intact.
5. Archive reading, conversion and safety use shared bounded header detection.
   ZIP content under a CBR filename is supported without bypassing member/path,
   payload, link, resource-limit or minimum-page checks. Detected mismatches are
   shown unobtrusively beside the file. Unknown formats retain normal parser errors.
6. Existing Follow-up actions use Find series, Match issues and Review suggestions
   labels. Completed-import retries, mixed-folder recovery, library organization,
   ownership receipts and Story Arc recovery remain the established workflows.

The prototype guides hierarchy and interaction, not weaker metadata or safety
policy. Repair actions are deliberately scoped, not an indiscriminate Fix all.
Preserved Step 5 and completed-recovery surfaces remain part of acceptance testing.

## Regression Gates

- Mylar and folder imports retain the same applicable identity and safety rules.
- Partial series import only matched files; conflicting, missing, unsafe, and
  unresolved files retain their evidence and remain recoverable.
- No source writes, moves, deletions, scans, or provider calls on review page loads.
  Explicit series/issue search previews can fetch metadata but never inspect files.
- No downgrade of exact ComicVine identity or target-summary requirements.
- Existing completed jobs, retries, mixed-folder recovery, duplicate handling,
  in-place references, and Story Arc recovery remain usable.
- Page loads use compact job-level aggregates and page-scoped file details, not
  full archive or provider inspection.
- Browser tests cover stable controls, expansion, modal focus, selection,
  pagination, and both explicit themes in Chromium and Firefox.
- Full CI and copied diagnostic-database comparison are required before release.

## Repeatable Scale Check

`tests/ui/test_import_review_projection_scale.py` creates saved review rows with
nonexistent source paths to verify that context loading is read-only and does
not inspect archives. It checks a 25-series page with 150 file details.

```sh
PULLBOX_REVIEW_BENCHMARK_SERIES=20000 .venv/bin/pytest \
  tests/ui/test_import_review_projection_scale.py -q -s --no-cov
```

Initial local measurement: 20,000 series / 120,000 files, 0.499 seconds for review
context loading. This is a synthetic saved-state benchmark, not a claim about
scan/import throughput or full browser rendering on a production database.

## Checkpoint Evidence

The initial checkpoint is preserved in commit `cfe8ee7`. The evidence in this
section predates the remaining decision paths and is not their validation record.

- Import-focused unit suite: 1,932 passed.
- Import UI, API, and integration suites: 627 passed. These include the review
  projection, signed safety approvals, Story Arc review, and completed-import
  recovery. Existing safety-path redaction is also covered in the new lanes.
- Existing review browser workflows: 34 passed across Chromium and Firefox.
- New stable-review browser scenario: passed in Chromium and Firefox, including
  scoped accessibility checks, modal focus, persisted controls, selection state,
  both explicit themes, and a 390-pixel-wide import gate. This is not an all-pages
  accessibility audit.
- Strict type checking, focused lint, JavaScript syntax, generated stylesheet
  build, and whitespace checks passed. Full CI has not been run.

A private, temporary copy of a supplied September 14 diagnostic database was
opened with SQLite `mode=ro`. The new projection classified 21,569 series and
184,190 saved file records in 1.129 seconds. Saved series/file status counts were
identical before and after, and the session had no pending changes. The original
diagnostic files were not opened for writes.

That completed/failed historical job grouped as 20,691 Info, 770 Needs a decision,
73 Ready, and 35 Fix source rows. These are display classifications of saved
records, not newly imported files or a claim that those rows can be retried
without the existing recovery checks. No archives, provider requests, recovery
actions, or import execution were exercised by this measurement.

## Completion Evidence

The remaining decision paths were developed after the no-validation checkpoint.
Regression runs and live acceptance drills include:

- Import, archive, safety and converter unit/regression suite: 1,810 passed.
- Import API/UI/integration: 633 passed.
- Chromium and Firefox import/Follow-up browser suite: 122 passed, 2 skipped.
- Strict type checking: all 691 application source files passed. Focused lint,
  formatting, JavaScript syntax and whitespace checks passed.
- An isolated real-file lab exercises persisted selection, individual reassignment,
  exact stale-reference pairing and ZIP content under a CBR name. Source verification
  must finish both its file decision and its global activity record. Normal
  rematching retains the durable work marker and the user's explicit selection.
- Eleven live previews cover all ten existing completed-recovery action types.
  Five representative recovery actions were applied successfully. Physical cleanup
  previews and moves/Trash were rerun against clones with content hashes preserved
  and stale approvals rejected.
- Browser-discovered regressions have targeted coverage: modal submission must not
  trigger boosted page navigation; ready files must not carry duplicate-exclusion
  text; rechecked archives display format evidence from their source diagnostics.
- Filesystem root resolution as well as archive inspection runs off the event loop.

The private lab, originals and primary development instance remain separate.
Its manual checklist has 214 checks, not 214 claimed passes. Full CI, full-library
acceptance runs and production-style recovery replay remain release gates.
