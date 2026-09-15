# Import Review Redesign

Status: first implementation checkpoint, not release-ready.

## Boundary

Step 3 becomes a decision-oriented review without replacing the import engine.
The established matching, source ownership, safety, and recovery rules remain
authoritative. The design prototype is a presentation reference, not permission
to replace those rules with weaker heuristics.

This checkpoint does not migrate the database or rewrite saved jobs on page load.
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

## Still Required Before Completing The Redesign

1. General individual file exclusion for newly matched series. The existing
   `include_in_import` field defaults differently for old matched and duplicate
   rows; applying it globally would break saved imports. Add an explicit,
   backward-compatible selection contract and confirmation/Follow-up tests first.
2. Per-file reassignment for mixed-comic conflict groups, rather than treating
   every group as a choice between copies. Preserve target identity validation
   and source references throughout reassignment.
3. Live, bounded source reinspection and proven stale-reference pairing actions.
   The existing offline recheck command cannot simply be exposed as a web action:
   root validation, job-state races, signed scopes, and background progress must
   remain intact. A repair action must never silently mean skip.
4. Shared signature-based archive identification with the existing safety
   inspection path. A suffix mismatch must not bypass member checks or turn an
   unsafe archive into a trusted one.
5. Review vocabulary alignment in the existing job-specific Follow-up view,
   without replacing its recovery actions or redesigning Step 5.
6. Finish the interaction audit against the prototype, including per-file actions,
   small-screen layout, and a recovery replay against a representative copied
   diagnostic database. The saved-state projection check below is not a replay.

## Regression Gates

- Mylar and folder imports retain the same applicable identity and safety rules.
- Partial series import only matched files; conflicting, missing, unsafe, and
  unresolved files retain their evidence and remain recoverable.
- No source writes, moves, deletions, scans, or provider calls on a review GET.
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
