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

When every selection removes an override and both canonical XML documents
already match the preview byte for byte, Apply changes only the selected fields'
provider-control settings. The comic, file registration, and import rollback
history stay untouched. The utility item records that result in the same database
transaction so a restarted worker cannot release a newer override. If the preview
contains an actual XML change, the normal reviewed archive write still applies.

### Manual Import

When **Import File** finds a descriptive disagreement, the default choices
still apply only to the new file. Choose **Edit issue fields** to save
corrections to that issue as well. In editor mode only the current library
value is selectable for series conflicts. Change shared catalog values through
the series editor, not by editing a single issue.
**Update preview** is read-only. **Import reviewed file** starts the normal
background import; for an owned issue, **Replace File** is a separate decision.
Declining it keeps the review open. A changed source, binding, or reviewed
replacement invalidates approval. Pullbox checks the replacement's content digest
before moving it aside. The selected source file is never the replacement target.

## Current Field Inventory

| Field | Type / validation | ComicInfo | MetronInfo | Behavior |
| --- | --- | --- | --- | --- |
| Issue title | Bounded text | Title | Stories/Story | Editable |
| Summary | Bounded text | Summary | Summary | Editable |
| Cover date | ISO date | Year/Month/Day | CoverDate | Editable, distinct from store date |
| Store date | ISO date | No native equivalent | StoreDate | Editable; does not overwrite the cover date |
| Catalog page count | Integer 0-1000000 | PageCount | PageCount | Editable, never changes physical pages |
| Creator credits | Up to 128 normalized Name / Role entries | Role-specific elements | Credits | Editable; resource IDs/unsupported content block destructive changes |
| Age rating | Unknown, Everyone, Teen, or explicit clear | AgeRating | AgeRating | Editable even when both old values agree; a manual correction is saved with the issue |
| Tags | Up to 128 distinct labels, one per line, maximum 255 characters each | Tags, comma-separated | Tags/Tag | Editable; retained labels keep their resource IDs, added labels have no ID |
| Genres, characters, teams, locations | Same limits as tags, separately per list | Genre, Characters, Teams, Locations | Genres/Genre, Characters/Character, Teams/Team, Locations/Location | Editable; IDs are preserved within the selected list only |
| Issue number, native provider IDs | Identity | Number/Web | Number/IDs | Protected; use validated provider linking |
| Cover artwork URL | Artwork | Existing renderer projection | Existing renderer projection | Protected; artwork workflow |
| Series title, sort title, publisher, summary, dates, type, archive format, status, count, volume, language, artwork | Series-wide catalog values | Existing series projection | Existing series projection | Read-only; no changes to siblings |
| Issue imprint | Single-line name, up to 255 characters | Imprint | Publisher/Imprint | Editable for this issue only. A nonempty imprint requires a named publisher; retain its ID only for the same imprint. |
| Embedded story arcs | Up to 128 unique arc names and optional positive positions | StoryArc and StoryArcNumber | Arcs/Arc/Name and Number | Edit names and positions together. Retained names keep their IDs. Does not create tracked arcs, change memberships, or reorder a reading list. |

ComicInfo uses one comma-separated list of arc names and one of positions. If
any position is unknown, Pullbox writes the names but omits the ComicInfo
position list; MetronInfo retains each known position on the correct arc.
Ambiguous or malformed source values cannot be chosen as corrections. Review
the other XML source, enter explicit pairs, or choose **Clear value**.

Arbitrary extension nodes, resource identifiers and page annotations remain under
the existing preservation contract, not editable raw XML. See the separate
[series editor](SERIES_METADATA_EDITOR.md) and [member-file propagation](SERIES_METADATA_PROPAGATION.md)
workflows. Other XML-only fields and bulk editing remain outstanding.

The age rating selector offers only identical labels from the
[ComicInfo 2.0 schema](https://raw.githubusercontent.com/anansi-project/comicinfo/main/schema/v2.0/ComicInfo.xsd)
and the bundled MetronInfo 1.1 schema. A format-specific label is shown as
evidence, not translated into a guessed audience. Explicitly clearing a rating
removes both elements; selecting Unknown writes both elements. No provider is
queried for a rating. A provider refresh preserves a manual correction.

Genres, characters, teams, locations, and tags preserve the selected order.
A comma inside one Metron name is not two ComicInfo entries: review the
original and explicitly replace or clear it. Within each field, an
exact retained label keeps its Metron resource ID, a renamed label starts
without one, and ambiguous duplicate IDs are never reassigned. A saved list
override does not authorize overwriting later external changes.

## Safety Boundary

The issue-page editor requires one independently bound, managed CBZ/ZIP. Missing,
referenced/read-only, actively importing and ambiguous files retain the existing
writer's rejection. Native formats require the separate explicit conversion
workflow. There is no implicit conversion, rename, move, download or sidecar write.

In **Intervention → Recovery**, **Review metadata** still opens the usual
conflict choices. If those choices cannot represent a fixable field disagreement,
the modal can open the field editor directly. Nothing is approved automatically.
**Edit issue fields** is available when the recovered issue
has its original verified managed CBZ or ZIP. Choose corrections under
**All fields**, review any separate series conflicts, then **Update preview**
and **Finish recovery**. Neither merely opening the editor nor updating its
preview writes the file. The approved job uses the same retained download
and the same publication journal for registered files. If the registration is missing
and both paired writers are enabled, **Restore and finish recovery** creates a new
managed copy with the approved XML pair and saves the corrections to the issue.
The retained source is not modified or downloaded again. A changed source or occupied
destination stops the restore. A replacement copy, a registered file needing native
conversion, or an incomplete issue keeps its existing recovery path.
When only removing an override, the library update and queue resolution commit
together without rewriting the comic.

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
