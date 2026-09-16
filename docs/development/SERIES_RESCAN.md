# Series Folder Rescan

The series detail **Rescan folder** action reconciles local files with the existing
issue catalog. It is separate from metadata refresh and library organization.

- Scans only the configured series folder and its subfolders, plus individually
  registered paths for that series. It never scans those other paths' parent
  folders or an entire library root.
- Inspects archives off the event loop using the shared safety, content, local
  metadata, conflict, and semantic matching helpers. No metadata-provider calls
  are needed. Exact issue-number text preserves lettered and fractional issues.
- Plans the complete candidate set before registration. Ambiguous copies, foreign
  series, weak matches, unsafe archives, one-page archives, and recently modified
  files require review instead of being silently chosen.
- Rechecks source signatures, root policy, catalog identity, current ownership,
  active imports, and downloads before writing. New files are referenced, not
  managed artifacts. Read-only mounts are supported.
- Existing valid copies win. Proven replacements for missing links preserve the
  LibraryFile ID and become references. Missing/unreadable files never downgrade
  ownership or trigger downloads. Rescans never move, rename, convert, overwrite,
  delete, or write ComicInfo into source files.

## Progress and Review

`series_rescan` uses the durable utility queue and shared background activity
projection. Initial inspection is indeterminate until the complete candidate set
is known; registration has measured progress. Each result is saved as a job item.
The series dialog shows added, repaired, unchanged, and review counts, with paged
exceptions. **Review in Import** opens an explicit single-file, keep-in-place
import for the existing matching and safety-review controls. This action does not
approve or bypass safety findings. Files still being copied or on an unavailable
mount should instead be rescanned after the source is stable.

The issue panel updates in place on completion, without replacing the page. Jobs
continue when the dialog is closed or the user navigates elsewhere. Saved reports
are also retained in utility history. A repeat click reuses the active series job.

The additive migration `o6i7j8k9l012` extends the utility job-type constraint. No
catalog or ownership rows are backfilled. Downgrade requires removal of rescan job
history first rather than silently reclassifying or deleting its records.

## Regression Coverage

`test_series_rescan.py` covers source preservation, referenced registration,
idempotence, stale ownership/link repair, duplicates, exact letter suffixes,
one-page/unsafe archives, unavailable roots, linked files in mixed folders, active
downloads, durable queue execution, report counts, and duplicate start requests.
Migration coverage preserves dependent history. Browser coverage exercises
progress, completion, saved results, keyboard focus, and stable page identity.
