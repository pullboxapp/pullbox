# Embedded comic reader

Pullbox can open an owned issue directly from its issue-details page. The reader is a private,
full-viewport, single-page experience intended both for ordinary reading and for quickly checking
that a download is the correct comic.

## Supported files

The reader supports CBZ, CBR, CB7, CBT, and PDF. The production image includes the official UnRAR
backend for CBR, py7zr/7-Zip support for CB7, TAR support for CBT, and Poppler for PDF rendering.
The file signature must match the format recorded in Pullbox. EPUB is not supported.

Archive pages are naturally ordered, unsafe members are ignored or rejected, and extraction,
decoded pixels, rendering time, worker concurrency, and generated cache storage are bounded. Large
images are resized to the configured rendition ceiling before delivery to keep browser memory use
predictable. Source comic files are never rewritten by the reader.

## Controls

- Use the visible previous/next buttons, the one-based page field, the outer tap zones, or a
  horizontal touch swipe while the page is fitted.
- Fit page is the default. Fit width, fit height, actual size, and stepped zoom are available.
- LTR/RTL changes arrow, tap-zone, and swipe meaning without changing canonical page numbers.
- Close returns to the same issue URL, scroll position, page state, and Read button focus.
- `?` opens the complete keyboard shortcut reference. The main shortcuts are arrow keys,
  Page Up/Down, Space, Home/End, `G`, `W`, `H`, `0`, `+`/`-`, `R`, `F`, and Escape.

Pullbox saves only the last page that remained visible after decoding for the settle interval.
Progress is private to the signed-in user. Prefetching and opening directly on the final page do not
mark completion; deliberately navigating to and viewing the final page does.

## Reading state and workspace

Reading state belongs to the signed-in user and the canonical Pullbox issue. It has three
independent dimensions:

- resume position records the last settled page for the current file revision and page count;
- Read/Unread records deliberate completion intent; and
- Want to Read maintains a private reading queue.

**Want to Read is not the acquisition status Wanted.** The reading queue does not trigger a search
or download, and acquisition state does not mark a comic read. Marking an issue Read removes it from
Want to Read but preserves its last known page. Marking it Unread clears completion without erasing
resume history.

The **Reading** workspace at `/reading` presents bounded Continue, Want to Read, and Read tabs. The
dashboard shows the eight most recently opened incomplete issues. Issue details expose the current
reading state and explicit actions; series details show per-issue state and a read aggregate; the
series registry reports readable/completed totals without opening comic archives. Unavailable
queued or read issues remain visible but cannot be opened until a supported owned file is restored.

## Moving between issues

Previous issue and Next issue are separate from page navigation. They consider only owned issues in
the same series with a registered CBZ, CBR, CB7, CBT, or PDF file. Pullbox waits for the active
issue's progress write before requesting the next manifest. A failed save, target manifest, or first
target page leaves the current issue open and reports an in-reader recovery message.

Only one adjacent page in the active issue may be prefetched; another issue's pages are not fetched
until the user explicitly switches. Reader fit, direction, and zoom preferences carry across the
switch. Reaching the final page shows a completion panel with **Read next issue**, **Mark unread**,
or a caught-up message as appropriate.

## File identity and continuity

Private state is keyed by `(user_id, issue_id)`, never a path or `LibraryFile` identifier. File
operations therefore follow these rules:

| Operation | Result |
|---|---|
| Rename or relocate | Position, completion, queue, clocks, and state version remain exact. |
| Convert to CBZ with the same page count | The saved ordinal position and issue-level intent remain. |
| Replace or re-import with the same page count | The saved ordinal position and issue-level intent remain. |
| Replace or re-import with a different page count | The next open starts on page 1; completion and queue intent remain. |
| Remove the registered file | Continue hides the issue; Want to Read/Read preserve it as unavailable. |
| Re-import against the same Pullbox issue | Readability returns using the existing private state row. |
| Delete the canonical Pullbox issue | Its private reading state is deleted by database cascade. |

The manifest is side-effect free: page-count reconciliation controls the initial page returned to
the client but does not rewrite saved state merely because a file was inspected.

## Errors and recovery

The reader keeps failures inside the full-viewport shell and never exposes library paths, archive
member names, renderer output, or stack traces. A page-level failure can be retried without closing
the reader, and the original Download action remains available. Missing, mismatched, corrupt, empty,
oversized, or temporarily busy sources return stable private errors.

If PDF or CBR support is unexpectedly unavailable, verify that the running image is the supported
Pullbox production image rather than a locally reduced Python environment. The authenticated
`GET /api/v1/reader/capabilities` diagnostic reports readiness for all five formats plus path-free
cache and worker limits.

## Cache and operations

Generated pages live under the Pullbox data directory in `reader-cache`; losing them affects only
latency because they are rebuilt from source comics. The authenticated, CSRF-protected
`DELETE /api/v1/reader/cache` operation clears only generated reader files and does not follow
symlinks or touch the comic library. Normal deployments use a 512 MiB quota, two expensive workers,
and a short bounded worker wait.

Archive reads retain hard entry, expanded-size, page-size, page-count, path, pixel, and concurrency
budgets. The 250:1 compression-ratio guard applies only to readable page entries that expand to at
least 4 MiB, so small solid-color pages are not mistaken for archive bombs. Operators can adjust the
floor with `PULLBOX_READER_COMPRESSION_RATIO_MIN_MB`; large high-ratio pages still fail before
extraction, with path-free structured diagnostics.

For an immediate feature rollback, set `PULLBOX_READER_ENABLED=false` in the Pullbox service
environment and recreate/restart that service. This hides Read and makes the private reader routes
return not found while preserving source comics, generated cache files, and private resume state.
Set the value back to `true` to restore the feature.

Reader state is stored in the main Pullbox database, so a full `/data` backup includes it. Generated
`reader-cache` files do not need to be backed up. Back up `/data` before any migration downgrade.
Downgrading the independent-state migration deletes queue-only, completion-only, and explicit-unread
rows that the old progress-only schema cannot represent; downgrading past the original reader-state
migration deletes all reader state. Disabling the feature gate is the preferred rollback because it
is non-destructive.

## Performance contract and acceptance snapshot

The reader indexes a source once per revision, reads or renders only the requested page, prefetches
only the adjacent page, and keeps generated pages in the bounded disk cache. Archive decoding and
PDF rendering never run on the event loop. The initial acceptance objectives are a cached manifest
or page under 150 ms, a cold common CBZ page under one second, and a cold PDF page under two seconds
on documented hardware. These are engineering objectives, not a network-inclusive release SLA.

The 2026-08-03 development acceptance run used the production ARM64 container on an Apple M4 Pro
MacBook Pro with 24 GB of memory. Each synthetic format fixture contained the same two JPEG pages;
the CBR fixture used RAR5. Times are single-run server measurements and RSS deltas are retained
process memory after garbage collection, so they are deliberately reported as observations rather
than statistically significant benchmarks.

| Format | Manifest | Cold first page | Repeated cache hit | RSS delta | FD/child leak |
|---|---:|---:|---:|---:|---:|
| CBZ | 5.5 ms | 10.6 ms | 0.13 ms | 2.4 MiB | 0 / 0 |
| CBR | 6.2 ms | 11.6 ms | 0.14 ms | 8.1 MiB | 0 / 0 |
| CB7 | 39.0 ms | 35.2 ms | 0.11 ms | 3.4 MiB | 0 / 0 |
| CBT | 3.4 ms | 4.1 ms | 0.17 ms | 1.6 MiB | 0 / 0 |
| PDF | 9.5 ms | 356.6 ms | 0.15 ms | 10.5 MiB | 0 / 0 |

A real 74.3 MB, 52-page CBZ from the mounted development library produced its manifest in 5.9 ms,
its cover in 129.5 ms, its next page in 4.0 ms, and a repeated page in 0.14 ms. The two generated
pages occupied 1.78 MiB on disk; retained process RSS grew by 52.4 MiB after decoding the large
source artwork. Cancellation, single-flight generation, worker saturation, cache quota/clear, and
source-preservation behavior have dedicated regression coverage.

The 2026-08-25 reader-enhancement acceptance run on an Apple M5 Pro with 48 GB of memory seeded
10,000 issues across 500 series and 5,000 overlapping state rows per user in SQLite. The warmed
Continue query used exactly two SQL statements, returned eight rows, and improved from roughly
2,154 ms to 3 ms after adding the measured `library_files.issue_id` join index. A 50-issue manifest
run retained three bounded page sources, added no file descriptors or child processes, and retained
about 1.2 MiB RSS. These are local development observations, not release SLAs.

## Future OPDS and mobile clients

`IssueReaderState` plus the adapter-neutral reader state and bounded query services are the source
of truth for future OPDS and mobile integrations. Those adapters must call the same commands and
projections rather than creating another state table or deriving state from file paths. Web URLs
remain a web-adapter concern and are not stored in domain projections.
