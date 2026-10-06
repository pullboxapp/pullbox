# GCD Local Metadata

GCD Local reads an operator-supplied official SQLite dump in place. The active
dump is separate from Pullbox's application database: no attachment, migration,
copy, index creation, or network request occurs during discovery or enrichment.
Settings > Metadata validates a candidate before replacing the active snapshot.
Reads retain signature checks, query-only connections, cancellation, and the
existing five-second disk-operation budget.

## Structured Creator Credits

Exact issue reads and issue-list pages now preserve the optional official
`gcd_story_credit` and `gcd_issue_credit` records. These joins use
`gcd_creator_name_detail.id`, not a fabricated ComicVine creator identity.
The source's supplied descriptive name is retained.

- Story credits are restricted to GCD's core sequence types: cartoon, cover,
  cover reprint, photo story, comic story, and text story. Letters pages,
  advertisements, previews, and house columns do not become comic writers.
- Issue-level credits remain eligible, including editors and custom work labels.
  Standard role names map to canonical writer/penciller/inker/colorist/letterer/
  editor roles. Custom labels remain descriptive, not guessed standard roles.
- Cover art is distinct from interior art. A person credited for both keeps both
  roles in the canonical snapshot and in the shared XML projections.
- Deleted stories and credits are excluded. An uncertain credit, missing role,
  or missing/deleted name makes that issue's entire credit set unknown instead
  of publishing a partial set as authority. No known records is also unknown,
  never an automatic clear. Unstructured free-text creator names are not parsed.
- Earlier minimal supported dumps need not contain these optional tables. Their
  series and issue functionality is unchanged. Present enrichment tables must
  be real, nonvirtual tables with the required columns; malformed profiles fail
  safely before graph persistence.

The existing source-priority and canonical reconciliation policy selects credits.
This adapter does not merge conflicting provider lists, change priority, infer a
cross-provider identity, or override a local edit/clear. Add and refresh reuse the
existing batched Creator/IssueCreator writer and caller-owned transactions.
Credits are also available to the existing reconciled ComicInfo.xml/MetronInfo.xml
preview and writing workflows; this change grants no new file-writing permission.

## Bounded Reads

One issue page reads at most 100 canonical issues. Optional profile inspection
and two credit joins are fixed per page, not one query per issue or creator.
The combined raw-credit result has a 12,800-row ceiling. Names and role text are
bounded in SQL before materialization; oversized or malformed values are rejected,
not silently truncated into facts. Shared canonical credit limits still apply.

Official dumps lack planner statistics. The story-type predicate deliberately
avoids the low-selectivity type index so the joins use the existing issue/story
foreign-key indexes. SQLAlchemy compiles expanding IN parameters into bound
parameters; user inputs are never interpolated into query text.

## Verification

`test_gcd_local_credits.py` in the unit and metadata-identity integration suites,
plus `tests/api/test_gcd_local.py`, cover actual dump-shaped foreign keys,
core-story classification, composite/custom/cover roles, unknown/uncertain sets,
read-only hashes, bounded text, schema errors, batched indexed pages, Add,
refresh, local edits/clears, and reconciled offline-validated paired XML.
Integration cases run against SQLite and the dedicated disposable PostgreSQL
database. CI uses deterministic dump-shaped fixtures, not live GCD requests.

This is creator enrichment, not Story Arc activation or a complete mapping of
all GCD descriptive fields. GCD Local supplies no cover URL or inferred crosswalk.
