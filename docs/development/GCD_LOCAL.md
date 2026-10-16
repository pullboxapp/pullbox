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

## Story Arcs

The existing Add Story Arc source selector now supports GCD Local discovery,
reading-order review, Add, and catalog refresh. This uses the optional official
`gcd_story_arc` / `gcd_story_story_arc` / `gcd_story` tables, not an API request
per issue. Dumps without these tables retain series/issue functionality and
report Story Arc search as unsupported. Present malformed tables fail safely.

Membership means distinct active issue records associated with active stories,
under public comic series. Multiple stories in one issue are deduplicated.
Deleted stories/issues/series are excluded. Broken member/parent references and
same-series variants stop the preview; Pullbox does not replace an explicit
variant with a guessed base issue or silently drop it to claim completeness.
Cross-series representatives retain their exact native identity, as in existing
GCD series reads. Ambiguous issue designations also retain the shared catalog
saver's existing rejection policy.

The initial order follows GCD's publication sorting (key date, sale date, series
sort name, issue sort code), with native issue ID as a stable final tie-breaker.
It is **not a curated reading order**. The preview says so and notes that story
associations can include reprints. Users can reorder or skip members using the
existing controls before Add. Related/sub-arcs and translations are not
automatically combined into a larger arc.

Search is bounded to 100 results per source page / 100 pages; member reads are
bounded to 100 issues per page and 5,000 distinct issues. The shared Add/refresh
command still requires a complete snapshot and applies its smaller 2,000-member
and 200-parent limits. The existing arc-membership FK index drives the join;
structured credits remain batched per member page. Arc descriptions/notes are
bounded in SQL before materialization. There are no new indexes or migrations.

Search cache keys include the validated dump generation. Signature checks run
before and after reads and before/after cached discovery; a changed or unreadable
dump cannot supply a stale result as current evidence. Healthy remote-provider
results remain available when GCD Local fails.

Native GCD arc/series/issue identities use the shared server-owned snapshot and
atomic catalog saver. Add is repeat-safe, refresh preserves reviewed order and
removed members, and new members require review. Existing file defaults, roots,
monitoring/search-on-add behavior, owned files, import recovery, and placement
safety remain unchanged. This activates **GCD Local**, not GCD API v2 Story Arcs.

Verification includes `test_gcd_local_arcs.py` in the unit, API,
metadata-identity integration, and browser suites: native namespace isolation,
multi-story deduplication, cross-series members, paging, invalid/changed dumps,
fixed indexed query counts, bounded text, caller rollback, reviewed refresh,
and the existing keyboard/reorder/skip controls in light, dark, and mobile views.
PostgreSQL parity uses the existing disposable test database; no live provider
is required by CI. GCD Local still supplies no cover URL or inferred crosswalk,
and this is not a complete mapping of every GCD descriptive field.
