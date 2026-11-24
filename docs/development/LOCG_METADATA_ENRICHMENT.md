# Cached LOCG Metadata Enrichment

Series **Refresh Metadata** can fill a missing publisher, start year, volume or
proven issue store date from the public What's New release cache. This is
passive release context, not a new executable provider, settings entry,
credential, crawler or source-priority choice.

## Adding From What's New

For a published release, **Add Series** uses the configured managed default
and normal search-on-add policy. It requires fresh, agreeing public release
evidence and a configured source for at least one exact series ID. Rendering
the table does not search providers. On click, one complete provider catalog
must corroborate all supplied IDs; Pullbox rechecks the release and destination
before saving. Repeat clicks do not create duplicate series. The selected
release ID is discovery evidence, never a canonical issue ID.

**Find & Add** remains available when evidence is sparse or changes. An
active Watch retains its own destination and confirmation workflow.

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

An issue store date needs a confirmed local series, a verified local issue
identity and consistent evidence for the issue. Without an exact provider issue
crosswalk, the normalized issue number and date must corroborate the local
issue. Disputed crosswalks do not fall back to a number match. Variants that
disagree about the date cannot fill the gap.

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
read-only. Enrichment does not create or promote Watches, attach identities,
or alter import/recovery decisions. Watch promotion is a separate workflow.

### Public Feed Field Audit

On November 23, 2026, the public [current-week feed](https://api.pullbox.app/api/v1/releases)
returned 514 rows and the [upcoming feed](https://api.pullbox.app/api/v1/releases?upcoming=true)
returned 3,089. None supplied a ComicVine, Metron or GCD issue or series ID.
This is a sample of the public responses, not a guarantee that the optional
fields are always absent. It does mean these releases alone cannot justify
adding a new provider series without user confirmation.

| Release evidence | Canonical use or boundary |
| --- | --- |
| Series publisher, start year, volume; issue store date | Supported where present and corroborated. The release-week grouping date is not an issue date. |
| Series title, release title, display title, issue number | Identification and display context. A listing such as "Absolute Flash #21" is not evidence of a story title. |
| Price and currency | 491 current rows had a price. The Metron price field requires a country. Currency does not establish it. |
| Community score | 367 current and zero upcoming rows had scores. These are timestamped What's New observations, not canonical ratings, age ratings or XML values. Keep manually edited archive scores separate. |
| LOCG URLs, cover URLs | Links belong to the release or edition. Provider URLs are protected identity references, not editable descriptive web links. A variant jacket is not necessarily the owned issue's cover. |
| Publication state and date, variant count, community activity | Discovery and Watch context. They do not establish series completion, issue inventory or personal reading state. |

Qualification lives in `test_locg_series_enrichment.py` (actual refresh on SQLite
and disposable PostgreSQL), `test_locg_issue_enrichment.py` (store dates,
variant ambiguity and stale evidence), `test_metadata_locg_enrichment.py` (provenance and
protected values), and `test_series_sidecar.py` (existing output reuse).
