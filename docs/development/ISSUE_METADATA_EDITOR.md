# Single-Issue Metadata Editor

This is the first manual-editing milestone, not the completed full-field,
series/sidecar or bulk-editing scope. Anna verification is unrelated and parked.

## User Workflow

On an owned issue, choose **Edit metadata**. The default **Differences** view
highlights existing disagreements; **All fields** exposes the catalog inventory
even when no conflict exists. Select the fields, choose saved library/XML values
or enter corrections, then **Update preview** and **Apply selected changes**.
Preview does not query providers or write files. The existing background progress
and cancellation controls apply after approval.

Only selected issue fields acquire correction intent. Clear is explicit and
remains empty during refresh. Remove manual override keeps the current value
now but permits later provider refresh to replace it. Selecting an absent source
cannot erase a nonempty value; the user must choose Clear. Closing before Apply
does not change library metadata or files.

## Current Field Inventory

| Field | Type / validation | ComicInfo | MetronInfo | Behavior |
| --- | --- | --- | --- | --- |
| Issue title | Bounded text | Title | Stories/Story | Editable |
| Summary | Bounded text | Summary | Summary | Editable |
| Cover date | ISO date | Year/Month/Day | CoverDate | Editable, distinct from store date |
| Store date | ISO date | No native equivalent | StoreDate | Editable; does not overwrite the cover date |
| Catalog page count | Integer 0-1000000 | PageCount | PageCount | Editable, never changes physical pages |
| Creator credits | Up to 128 normalized Name / Role entries | Role-specific elements | Credits | Editable; resource IDs/unsupported content block destructive changes |
| Issue number, native provider IDs | Identity | Number/Web | Number/IDs | Protected; use validated provider linking |
| Cover artwork URL | Artwork | Existing renderer projection | Existing renderer projection | Protected; artwork workflow |
| Series title, sort title, publisher, summary, dates, type, archive format, status, count, volume, language, artwork | Series-wide catalog values | Existing series projection | Existing series projection | Read-only; no changes to siblings |
| Rating, genres, tags, characters, teams, locations, imprint, arcs/positions | Preserved XML-only values | Corresponding elements | Corresponding elements | Display both documents; existing Write file metadata resolves disagreements |

Arbitrary extension nodes, resource identifiers and page annotations remain under
the existing preservation contract, not editable raw XML. Full XML-only field
editing, series/series.json editing, member-file propagation and bulk editing
remain outstanding. This milestone does not claim all-schema editing completion.

## Safety Boundary

The editor currently requires one independently bound, managed CBZ/ZIP. Missing,
referenced/read-only, import-owned and ambiguous files retain the existing
writer's rejection. Native formats require the separate explicit conversion
workflow. There is no implicit conversion, rename, move, download or sidecar write.

Preview binds the exact field actions into the existing evidence key. Apply and
the worker revalidate file identity, ownership and current library state. The
existing archive publication journal publishes the reconciled pair and finalizes
canonical values, credits and provenance. Unselected disagreements still block;
select-all does not approve identity changes or shared XML conflicts.

The optional `FieldOrigin.provider_managed` flag records an explicit override
reset without inventing provider attribution. Existing snapshots remain readable.
New snapshots may contain this flag, so rollback to an older application requires
the corresponding consistent database backup, not just an old container image.
Normal refresh behavior is unchanged unless this explicit flag is present.

## Focused Qualification

Tests cover clean/empty/protected fields, selected XML/custom values, clear/reset
across refresh, stale approval, source replacement, cancellation, preserved pages,
unchanged series values, CSRF and request validation. Browser tests cover all
three themes, mobile bounds, accessible controls, unsaved-value retention,
preview/apply and real progress components. Existing paired-writing/recovery
tests remain part of the focused regression set. PostgreSQL uses the same
parametrized integration tests when its test database is configured; a skipped
PostgreSQL case is not qualification evidence.
