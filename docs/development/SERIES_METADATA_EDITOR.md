# Series Metadata Editor

This milestone edits one series and its eligible `series.json` outputs. Comic-file
propagation and bulk editing remain separate. Anna verification is parked.

## User Workflow

Open **Edit metadata** from series details. **Differences** shows saved-value
disagreements; **All fields** also exposes unchanged, empty and protected fields.
Select fields and choose a saved value, enter a correction, intentionally clear
an optional value, or remove a manual override. Preview before applying.

The default destination is **Library and eligible series.json files**. **Library only** is an
explicit choice, including for reference-only libraries. Preview lists actual
sidecar destinations and blocked locations. No remote provider is queried, and
the editor does not claim to have fetched new source values.

Manual corrections and intentional clears survive provider refresh. Removing an
override keeps the current value until configured refresh supplies a replacement.
Missing source values cannot silently erase data. Required values cannot be
cleared. User choices remain available after validation errors.

## Field Inventory

| Field | Scope and output |
| --- | --- |
| Series title and sorting title | Library and canonical sidecar; never renames folders or comics |
| Publisher and description | Library and canonical sidecar; does not edit issue summaries |
| Start/end year, status and series type | Validated series values and canonical sidecar |
| Archive format | Publication format, distinct from Pullbox's internal series type |
| Volume and language | Canonical baseline and sidecar; no member comic propagation |
| Issue count | Protected catalog value; editing metadata cannot create catalog rows |
| Provider IDs and cover artwork | Protected; use validated provider linking or artwork workflow |
| Aliases and safe custom JSON | Preserved; not a raw JSON editor |

## Safety Boundary

The editor changes no issue records, archive members, reading progress, import
history, ownership, filenames or folder names. It does not convert, download,
delete or move comics. Mixed folders, referenced files, read-only roots and
active import/archive mutations retain their existing write protections.

One reviewed series snapshot supplies the canonical metadata and sidecar output.
Revisions, identity and destination evidence are rechecked before publication.
Each sidecar uses the existing atomic publisher; multiple files and the database
are not one atomic transaction. Results must distinguish a saved library record
from completed, blocked or still-pending sidecar writes. A retry must preserve
completed output and never silently broaden the approved destinations.

The library update and retained sidecar job are committed together. Sidecar work
uses the existing background utility queue. Closing the dialog does not cancel
it; cancelling stops unfinished sidecars, not the already-saved library update.
**Retry unfinished sidecars** rechecks the original destinations without applying
the library correction again. An output published before an interrupted receipt
is recognized by its exact contents and is not rewritten. Changed library values,
folder ownership or unexpected sidecar contents require a new preview.

Safe unknown JSON content is retained. Invalid or identity-conflicting sidecars
are not overwritten. Library-only editing is not permission to repair or replace
those files. ComicInfo.xml and MetronInfo.xml remain unchanged by this editor.
Explicit shared-field propagation is a separate action documented in
[Series Metadata Propagation](SERIES_METADATA_PROPAGATION.md).

Explicitly edited descriptive fields also update their recognized legacy JSON
aliases, including `summary`/`description`. Unselected aliases and unrelated custom
JSON are preserved. The ordinary sidecar exporter keeps its existing behavior.

## Focused Qualification

Qualify selected edits, explicit clears and resets, stale previews, partial
sidecar publication, retry/cancellation, split managed folders, reference/mixed
root exclusions and unchanged comic bytes on SQLite and PostgreSQL. Browser
checks cover modal bounds, standard controls, focus, all themes, progress and
unsaved-value retention. No PR or full CI is part of this private deployment.
