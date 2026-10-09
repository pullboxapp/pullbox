# Cached LOCG Series Enrichment

The existing source-aware series Refresh Metadata transaction can fill missing
publisher, start year and volume from the public What's New release cache. This
is passive release context, not a new executable provider, settings entry,
credential, crawler or source-priority choice.

## Admission

The series must already own a verified LOCG series identity, established by exact
evidence or explicit selection. Titles never establish this link. Only the newest
current-week cache and the unfiltered upcoming cache are read, with the existing
six-hour freshness limit; future timestamps are ineligible. Each payload and the
combined release-ID provenance are bounded to 10,000 entries.

Non-null exact provider series IDs must be valid and agree with verified or
observed provider evidence. Disagreement stops refresh with a review/recheck
message; priority cannot decide it. Crosswalks are checked, not attached.
Variant release IDs are retained as many-to-one discovery provenance, never
converted into canonical issue identities or duplicate issues. Inconsistent
descriptive facts leave the affected gap empty rather than guessing.

## Canonical Values

Provider assembly runs first. Release context fills only compatible blanks and
cannot overwrite existing values, deliberate user clears, embedded provenance or
archive disagreement. A later explicit managed refresh may replace a passive
value with stronger exact-provider metadata; user edits remain protected.

`FieldOrigin.passive_release` records the confirmed series ID, distinct release
IDs and oldest contributing cache fetch timestamp. It is separate from
`MetadataSource`, requires the same verified series identity in the snapshot, and
cannot also claim provider, override, derived or embedded origin. Existing
version-one snapshots remain readable; no database migration is required.

After provider I/O, the short caller-owned write transaction locks and rechecks
the contributing cache facts and freshness alongside the existing library,
identity and source-policy read set. Changed or expired evidence requires retry
before canonical mutation. No extra query occurs for an unlinked series.

## Outputs And Limits

Refresh changes canonical database metadata, not archives or sidecars. Existing
explicit paired-XML and series.json workflows consume the saved snapshot and
retain their admission, preview and file-safety guarantees. Page rendering remains
read-only. This slice does not fill issue dates/titles, price/currency, URLs or
covers, promote Watch automatically, or alter import/recovery decisions.

Qualification lives in `test_locg_series_enrichment.py` (actual refresh on SQLite
and disposable PostgreSQL), `test_metadata_locg_enrichment.py` (provenance and
protected values), and `test_series_sidecar.py` (existing output reuse).
