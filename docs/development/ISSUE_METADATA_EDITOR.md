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

If the browser loses the response to **Apply selected changes**, use **Check
this edit**. It reads the saved job for that operator, issue and submission,
not the latest job on the issue. A queued job is not a completed file write.
If no job is recorded yet, check again or choose **Review changes again**.
The draft is retained, and the preview is read-only. Submitting the same reviewed
edit again with its receipt cannot create a second job, even if the first one
finished in the meantime. The receipt does not authorize file access or edits.

**Saved source** identifies the origin recorded for the current library value.
For example, a cover date can come from GCD Local and a store date from LOCG
release context. A manual correction keeps its own label. A value found in
the incoming comic is a choice, not proof of where the library value came from.

With fields checked, **Use ComicInfo.xml** or **Use MetronInfo.xml** chooses
that document for the checked fields where it has a usable value. Missing or
incompatible values leave the existing draft choice intact. The shortcut does
not check more fields, resolve series or identity conflicts, or write anything.
Use **Update preview** before applying. Manual import and recovery use these
same controls when **Edit issue fields** is open.

**Select all eligible** leaves fields without a usable library value set to
**Leave unchanged**. They receive no new override, so a provider can still fill
a gap. Choose a saved XML value or enter a correction to adopt a value instead.
Only selected issue fields with a value action acquire correction intent. Clear is explicit and
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
| Issue title | Bounded text | Title | Single Stories/Story when not separately reviewed | Editing the issue title does not replace an anthology's contents. |
| Story titles | Up to 128 distinct titles, one per line | No native equivalent | Stories/Story | Reorder or correct the contents without renaming the issue. Retained titles keep their IDs. New or renamed titles receive no ID. |
| Summary | Bounded text | Summary | Summary | Editable |
| Review, scan information | Separate multiline text fields, up to 20,000 characters each | Review, ScanInformation | No native equivalent | Choose a library or ComicInfo value, enter text, or explicitly clear. Neither field changes the summary, Notes, or MetronInfo. |
| Main character or team | One printable name, up to 255 characters | MainCharacterOrTeam | No native equivalent | A comma can be part of the name. Does not change the character/team lists or their IDs. |
| Series groups | Up to 128 distinct group names, one per line, up to 255 characters each | SeriesGroup, comma-separated | No native equivalent | Labels for this issue; order is preserved. Does not change the tracked series, file path, story arcs, or Metron collection title. |
| Alternate series, number in alternate series | Single-line text, up to 255 characters each | AlternateSeries, AlternateNumber | No native equivalent | Crossover grouping and position; neither changes the tracked series or issue. |
| Total in alternate series | Whole number from 0 to 2147483647, or -1 for unknown | AlternateCount | No native equivalent | Descriptive only; does not add issues or change the parent series count. |
| Alternative issue number | Single-line text, up to 255 characters | No native equivalent | AlternativeNumber | Kept separate from the crossover position. Both number fields preserve letters and leading zeros. |
| Collection title | One printable line, up to 4096 characters | No native equivalent | CollectionTitle | Title of the collection for this issue. Does not rename the issue, its stories, or the series. |
| Manga volume | One printable line, up to 4096 characters | No native equivalent | MangaVolume | A label such as 007-B; keeps leading zeros and letters. Does not change the series volume, tracked issue number, ComicInfo manga flag, or reading direction. |
| Reprints | Up to 128 distinct entries, one per line, up to 4096 characters each | No native equivalent | Reprints/Reprint | Commas are part of the label. Exact, unambiguous retained labels keep their IDs when reordered. Renamed and new entries receive no ID. Clearing removes the list, not any comic. |
| Universes | Up to 128 distinct pairs of name and optional designation, up to 4096 printable characters per value | No native equivalent | Universes/Universe/Name and Designation | Add, reorder, remove, or correct pairs. Two universes can share a name. IDs stay with exact, unambiguous retained pairs and are never copied onto a renamed or redesignated entry. |
| Black and white | Unknown, No, Yes, or clear | BlackAndWhite | No native equivalent | Clear removes the field; it does not mean No. |
| Manga | Same choices, plus Yes (right to left) | Manga | No native equivalent | Corrects the book's metadata, not the reader's settings, page order, or Metron's manga volume number. |
| Notes | Plain text, up to 4096 characters including protected markers | Notes | Notes | Choose the prose from either file, enter text, or clear it. Each document keeps its own trailing identity markers. |
| Cover date | ISO date | Year/Month/Day | CoverDate | Editable, distinct from store date |
| Cover prices | Up to 128 country and amount pairs | No native equivalent | Prices/Price and its country attribute | Add, reorder, correct, or remove entries. Country codes can repeat. Zero and decimal precision are preserved; the format has no currency field. |
| Store date | ISO date | No native equivalent | StoreDate | Editable; does not overwrite the cover date |
| Catalog page count | Integer 0-1000000 | PageCount | PageCount | Editable, never changes physical pages |
| Creator credits | Up to 128 normalized Name / Role entries | Role-specific elements | Credits | Editable; resource IDs/unsupported content block destructive changes |
| Age rating | Unknown, Everyone, Teen, or explicit clear | AgeRating | AgeRating | Editable even when both old values agree; a manual correction is saved with the issue |
| Community rating | Exact score from 0 to 5, up to two decimal places | CommunityRating | CommunityRating/AverageRating | Zero is a score; clearing removes the rating. Metron's optional RatingCount stays only with the same numeric average. |
| Tags | Up to 128 distinct labels, one per line, maximum 255 characters each | Tags, comma-separated | Tags/Tag | Editable; retained labels keep their resource IDs, added labels have no ID |
| Genres, characters, teams, locations | Same limits as tags, separately per list | Genre, Characters, Teams, Locations | Genres/Genre, Characters/Character, Teams/Team, Locations/Location | Editable; IDs are preserved within the selected list only |
| Issue number, native provider IDs | Identity | Number/Web | Number/IDs | Protected; use validated provider linking |
| Reference links | Ordered public HTTP(S) URLs, at most one primary | Web | URLs/URL with optional primary flag | Preview, reorder, correct, select from an XML document or clear. Provider links stay in their own document. |
| Edition barcodes | Check digit and format | GTIN | GTIN/ISBN, GTIN/UPC | Edit either native identifier independently; explicitly select which one to use in an empty ComicInfo field. Does not change any provider match. |
| Cover artwork URL | Artwork | Existing renderer projection | Existing renderer projection | Protected; artwork workflow |
| Series title, sort title, publisher, summary, dates, type, archive format, status, count, volume, language, artwork | Series-wide catalog values | Existing series projection | Existing series projection | Read-only; no changes to siblings |
| Issue imprint | Single-line name, up to 255 characters | Imprint | Publisher/Imprint | Editable for this issue only. A nonempty imprint requires a named publisher; retain its ID only for the same imprint. |
| Embedded story arcs | Up to 128 unique arc names and optional positive positions | StoryArc and StoryArcNumber | Arcs/Arc/Name and Number | Edit names and positions together. Retained names keep their IDs. Does not create tracked arcs, change memberships, or reorder a reading list. |

Choose **Cover prices** to correct or clear a malformed country or amount. The
editor offers this repair only if the rest of the Metron document passes its
schema. The original stays untouched until approval, and the final XML must
validate in full. Annotated price entries are not editable.

An unreviewed Metron file with a single short story still offers that story as
an issue title, including the usual review if the titles disagree. Multiple
stories, or a native story title longer than Pullbox's issue title limit, are
contents rather than an issue title. Choosing **Story titles** records that
distinction for this issue even if only one story remains or the list is
cleared. It does not approve a different ComicInfo issue title, change the
pages, or qualify a story-only extract as complete.

ComicInfo uses one comma-separated list of arc names and one of positions. If
any position is unknown, Pullbox writes the names but omits the ComicInfo
position list; MetronInfo retains each known position on the correct arc.
Ambiguous or malformed source values cannot be chosen as corrections. Review
the other XML source, enter explicit pairs, or choose **Clear value**.

Notes are not the issue summary. For example, copying a scan note from ComicInfo
to MetronInfo does not copy a trailing `[CVDB1234]` marker. Clearing Notes
removes the prose, not the markers. If a note contains ambiguous identity text,
it stays read-only; other fields can still be edited unless an earlier saved
Notes correction now conflicts with the source. Different notes alone do not
require review during an ordinary import.

Community rating is separate from age rating and from the What's New release
feed. A Metron average of 4.500 can be chosen as 4.5 without losing its votes.
An average of 4.555 cannot be copied exactly to ComicInfo; leave it unchanged
or explicitly enter the score to keep. Changing the Metron average or clearing
it also removes its old vote count, shown in the preview before approval.
Unselected ratings do not create new import conflicts.

### Remaining XML Fields

These groups are not yet editable in the single-issue review. This inventory
distinguishes missing controls from fields that need a different workflow; it
does not authorize dropping preserved data or writing to a new field on import.

| Source fields | Remaining decision or workflow |
| --- | --- |
| ComicInfo Pages | **All fields** counts the page entries recorded in the XML, not the physical pages in the book or reader progress. Bookmark text and page keys are not displayed in this summary. |
| Metron LastModified | **All fields** shows the source timestamp from the XML. This is not the release date or the file modification time. |
| Metron resource IDs | **All fields** counts entries with IDs, such as credits and tags. Series and issue provider identities are excluded. IDs cannot be assigned by position or inferred from a renamed label. |
| Metron Series/VolumeCount | Use **Total volumes** in the series editor. Updating selected comic files is a separate series action. |
| Metron Series/AlternativeNames | Series-wide descriptive names, not issue-only overrides. |

These three archive summaries are read-only. They do not appear in **Differences**,
are never selected by **Select all eligible**, and do not create import conflicts.

Arbitrary extension nodes, resource identifiers and page annotations remain under
the existing preservation contract, not editable raw XML. See the separate
[series editor](SERIES_METADATA_EDITOR.md) and [member-file propagation](SERIES_METADATA_PROPAGATION.md)
workflows. Other XML-only fields and bulk editing remain outstanding.

**Edition barcodes.** MetronInfo stores ISBN and UPC separately. ComicInfo has one
GTIN field: choose **Use Metron ISBN** or **Use Metron UPC** to fill it. Pullbox
does not pick one for an empty field. When a corrected native number already
matches the ComicInfo barcode, the preview shows the corresponding change or
clear there too. An unrelated retail number stays untouched. Conflicting
numbers of the same kind require a choice when editing those barcodes; they do
not block unrelated edits or create new import reviews.

Original ISBN punctuation, leading zeros and UPC supplements are retained in
MetronInfo. Projection uses the ISBN-13 or full UPC without the supplement
([GS1 General Specifications](https://ref.gs1.org/standards/genspecs/)).
Checksum validation cannot prove the number belongs to this edition. Barcodes
are never used to create a provider link.

**Reference links.** The [ComicInfo Web field](https://anansi-project.github.io/docs/comicinfo/documentation#web)
stores multiple URLs separated by spaces; MetronInfo also supports one optional
primary link. Choosing ComicInfo keeps the existing Metron primary if that URL
remains in the list. Moving a row does not make it primary. Clear removes only
descriptive links, never provider URLs or IDs. If a protected provider URL is
primary, it stays primary. The editor does not visit these URLs. Lists containing
credentials, ambiguous text or opaque XML are left alone rather than copied.
If an external edit makes a previously saved link override unreadable, **Remove
manual override** is still available. Used on its own, it releases the saved
override without rewriting the comic.

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

Review and scan information are optional. Import does not automatically adopt them.
Oversized text and text containing a recognized comic identifier stay read-only;
other fields can still be edited. A reviewed change cannot alter the identifiers
used to match nested comics, even when an identifier spans neighboring XML fields.
If an externally edited or removed value disagrees with a saved correction,
preview asks which ComicInfo value to keep rather than silently restoring it.

**Alternate series**, **Number in alternate series**, and **Total in alternate
series** describe the book's [ComicInfo crossover grouping](https://anansi-project.github.io/docs/comicinfo/documentation#alternateseries--alternatenumber--alternatecount).
**Alternative issue number** edits only the MetronInfo field. For example, the
same issue can be 003-b in a crossover and 0150-X in MetronInfo. Select each
field separately. Neither number changes the tracked issue, series, provider
match, filename, or reading order. Clearing the total removes the tag; -1 means
unknown and 0 is distinct from both. A malformed legacy total can be corrected
or cleared without having to replace it during unrelated edits.

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
