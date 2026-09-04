# Import Performance

## Runtime Contract

Mylar and folder imports share bounded, read-only archive inspection. A worker
receives paths and immutable safety settings, never an AsyncSession. Completion
and diagnostic updates flow through one coordinator. Review file rows are
inserted in batches of at most 500 in the existing transaction; source pages
retain their durable checkpoints. A singleton issue group cannot contain a
duplicate or conflict, so it no longer loads files and commits just to check.
Archive page-name matching runs off the event loop and uses a bounded local
cache for repeated page-title parsing. It still counts every page toward
consensus and does not reuse safety decisions between scans.

`PULLBOX_IMPORT_SCAN_WORKER_COUNT=0` selects automatic inspection concurrency,
up to four workers. CPU affinity, cgroup v2 CPU quotas and parent limits,
cgroup memory headroom, and OS available memory cap the budget. Common cgroup
v1 mounts are supported too. Missing resource information falls back
conservatively. Explicit values 1-16 are also capped. The budget is reevaluated
between batches; this is not a throughput-learning autotuner or a guarantee
against other host workloads consuming resources.

The resource ceiling reserves at least one CPU (25% on larger machines) and
512 MiB of available memory, with an additional 512 MiB allowance per inspector.
Automatic mode deliberately does not saturate high-core machines: local ZIP
header parsing did not improve with more than 2-4 workers. Docker Desktop's
VM resources are the relevant limits, not the Mac's advertised RAM.

Step 4 keeps `PULLBOX_IMPORT_FILE_WORKER_COUNT=2` and its existing temporary-space
preflight, target-collision serialization, per-worker sessions, and rollback
journal. It now bounds submitted tasks as well as active workers, rather than
creating one waiting task per file. Exiting or canceling either worker pool
drains active work before the job can transition; no orphan filesystem work
may continue after cancellation is reported complete.

## Progress and Evidence

Unknown inventory totals are indeterminate. Completed series report 100% for
the current item, independent of overall phase weights. The browser must not
invent an ETA when the backend reports an unknown estimate. Matching emits
lightweight completion updates between durable checkpoints without adding a
database commit per item.

`import_archive_inspection_batch` reports actual workers, effective CPUs,
available memory, files checked, and elapsed milliseconds. Mylar and folder
batch events separately report inspection and persistence durations; existing
Step 2 timing events retain discovery/matching/total durations. These metrics
are observations, not substitutes for transaction-wait or storage profiling.
Inspection batch wall time includes policy reads, reconciliation, and progress
callbacks. Persistence timing covers row materialization/flush, not the later
checkpoint commit. Do not interpret either value as exclusive disk I/O time.

## Reproducible Benchmarks

```bash
.venv/bin/python scripts/benchmark_import_scan.py \
  --series-count 100 --files-per-series 12 --trusted-comicinfo \
  --archive-pages 32 --inspection-workers 4
```

The benchmark creates an isolated temporary source tree and database and makes
no external provider calls. Repeat with workers 1, 2, 4, 8, and 16; use medians
and retain the effective worker count, not only the requested count. Compare
identical matched, blocked, conflict, and missing-file outcomes before speed.

`--inspection-delay-ms 10` adds controlled per-archive latency to evaluate I/O
overlap. It is a simulation, not a NAS measurement. Archive fixtures exercise
member indexes and bounded ComicInfo reads, not full image decoding or physical
multi-gigabyte payloads. Run representative CBR/conversion workloads and real
storage samples before claiming a user's end-to-end speedup.

Do not add persistent cross-scan safety caches, speculative provider concurrency,
or process pools without evidence and new invalidation/recovery tests. Existing
compact archive metadata reuse remains intact; safety is freshly evaluated.
