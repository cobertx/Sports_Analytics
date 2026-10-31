# Local dashboard performance

The application keeps its existing Dash router, forecast models, statistical definitions,
saved simulation formats, and data freshness policies. No new dependencies or services
are required.

## What changed

- Market ladders initially build six selections for each of the first two sections.
  Other headers and their historical-data links appear immediately; opening a section
  loads its rows and distribution locally. **Show more** loads batches of 50, preserving
  every selection, its ordering and probability. The shared exchange formatter retains
  live price patches and updates the server-side source for unopened rows.
- A bounded server registry holds row renderers for 32 views. Scoped view IDs prevent a
  response for an old game/snapshot/method from replacing a new view. Old inactive views
  can expire; reopening the category or rerunning Quick Filtering restores them.
- The shared decoded-dataset cache holds at most 16 frames and 128 MiB of estimated
  retained DataFrame/payload memory per process. Large frames still load but are not
  retained. Identical simultaneous reads share a decode; unrelated reads can overlap.
  Returned datasets are independent copies. SQLite/WAL revisions, exact payload bytes,
  existing TTLs, corruption handling and stale-source fallbacks remain effective.
- WHP/UO/OAM/PGE forecast input identities use the existing content-fingerprint logic
  instead of serializing entire tables as JSON. Keys retain schema, row/index order,
  timestamp precision and parameters, with a JSON fallback for unhashable objects.
  The identity format is versioned; old derived forecasts may need one rebuild.
  Archived simulation reports remain accessible.
- FanDuel and Kalshi Quick Filtering submit to the existing bounded background queue.
  Two filter jobs can advance concurrently; their method computations share two worker
  threads. Existing backtest process limits and single-threaded child BLAS settings
  remain in place. NCAAF preparation keeps its serial scan path.
- Browser scan state contains request/result references, not snapshots or fitted models.
  Each job retains its snapshot, methods, criteria and prompt overrides on the server.
  A bounded, per-key single-flight forecast cache reuses fits across threshold changes.
  Keys include inputs, implementation revisions, model parameters, source revisions,
  season weighting and availability freshness. Explicit rescans can retry failed jobs.
- The top three qualifying results remain visible; remaining results load in batches of
  50. Full rankings and saved Filtered Results still include every qualifying selection.

## Repeatable offline benchmark

From the repository root, using the existing environment:

```powershell
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
.\.venv\Scripts\python.exe scripts/benchmark_dashboard.py --repeats 5 --concurrency
```

This reads existing NBA market CSVs and saved historical inputs. It blocks remote HTTP,
uses temporary folders for fixtures, checks history equivalence and the 75% payload
reduction target, and prints raw timings as JSON. The optional concurrency checks use
actual isolated backtest workers and verify their results against sequential runs.
They do not start simulations against the user's saved reports.

Observed on this Windows workspace, five samples per timed operation:

| Operation | Reference | Updated |
| --- | ---: | ---: |
| Initial NBA Combos payload, 1,589 selections | 4,663,296 bytes | 146,707 bytes |
| Build + serialize that loading-state view, median | 402.9 ms | 14.5 ms |
| Peak Python allocations during one rendering | 26.5 MB | 1.8 MB |
| Repeated saved player/team history loading, median | 1,082.2 ms | 121.3 ms |
| Forecast input identification, median | 459.9 ms | 87.6 ms |

The payload reduction was **96.9%**. The updated history cache retained nine datasets,
about 48 MB including payloads, and reused all 45 decodes across five warm repetitions.
The reference four-entry cache had no hits on that working set.

In a controlled offline HTTP fixture, Quick Filtering returned a 375-byte request state
in 28 ms while its forecast was blocked. Two threshold selections reused one game fit.
Five concurrency/numerical-equivalence regression fixtures passed.

These are server-side samples, not overall application speed guarantees. The reference
market renderer builds all rows; the history reference uses the former four-entry
policy. Cache-cold timings are not OS-file-cache-cold timings. The single cold history
sample was 900 ms for the reference and 1,077 ms with memory accounting; warm reuse is
the intended improvement. Python allocation measurements exclude native/browser memory
and are collected separately from elapsed timings. Five samples do not support a stable
p95 estimate. No real-browser latency or visual smoothness improvement is claimed.

## Regression checks

```powershell
.\.venv\Scripts\python.exe scripts/check_dashboard_efficiency.py
```

The focused checks cover cache limits, single-flight reads, failure recovery, copy safety,
content identities, lazy pagination, expired scopes, actual HTTP job submission, stale
game suppression, threshold reuse, concurrent jobs, and the shared method-worker limit.
Existing market, forecast, availability, season-weighting, backtesting, saved-result,
concurrency, and navigation suites remain the authority for their established behavior.
The complete Python workflow passed 38 suites (460 unit tests), alongside 254 JavaScript
checks and dependency validation. Actual browser automation was unavailable in this session.
The local measurement and validation records are in `data/benchmarks/` (ignored by Git).
