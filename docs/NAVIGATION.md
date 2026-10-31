# Navigation and loading

The local `index.py` workflow keeps its existing hash router and mounted page
shell. Existing market locations, session selections, saved results and back/forward
URLs remain compatible. No dependencies or external services were added.

## Changes

- Historical figures include a small route/sport/team-or-player identity. A browser-side gate
  shows a static placeholder while the previous player's results are being replaced.
  Late results for a different route/sport remain hidden. Empty and failed loads
  can finish normally and retain their source/error messages.
- Dataset-summary loading covers that summary alone. Charts, game logs, news and
  weather have their own indicators. Graph loaders observe `figure`, table loaders
  observe `data`, and other loaders observe `children`. Existing 150 ms spinner
  delay avoids flashes on fast responses; it does not delay the calculation.
- Returning to linked player history waits for the visible performance panel
  before scrolling. Returning to a market still opens the original prompt.
  Restoration stops when the user scrolls, touches, types or interacts.
- DOM notifications during restoration are batched into one animation-frame check.
  Settled, unrelated updates do not keep scanning or repositioning the page.
- Sidebar links receive current-page semantics locally. A keyboard-accessible
  skip control focuses the shared content region without changing the route.
  Route focus uses `preventScroll`, leaving prompt restoration in control of scrolling.
- Historical-chart zoom survives a rolling-window refresh of the same analytical
  selection. Changing player, statistic, season, phase, sport or linked prompt
  changes `uirevision` and resets that view. Calculations, hit rates and exports
  are unchanged.

The sidebar's 180 ms color transitions honor reduced-motion preferences. No
full-page fades, reloads, continuously animated skeletons or routing migration
were introduced.

## Validation

The first navigation pass passed 221 Python unit tests and 285 JavaScript checks.
The follow-up dispatch improvements and current validation are described below.
The affected checks can be repeated with the project's existing environment:

```powershell
.\.venv\Scripts\python.exe scripts/check_market_transitions.py
.\.venv\Scripts\python.exe scripts/check_performance.py
.\.venv\Scripts\python.exe scripts/check_responsiveness.py
.\.venv\Scripts\python.exe scripts/check_loading_speed.py
.\.venv\Scripts\python.exe scripts/check_page_speed.py
node scripts/check_market_navigation.js
node scripts/check_market_transitions.js
```

The deferred-DOM harness covers rapid route changes, delayed content, active links,
focus cancellation, skip navigation, market restoration and mutation batching. The
screen-selection harness tests rejection of previous-player and previous-sport
results and immediate pending states on native URL changes. Python fixtures verify
unchanged chart observations and exported rows, scoped zoom persistence, empty/error
identities, localized loaders, retained state and nonblocking background work.

In a controlled mocked-DOM comparison against the previous navigation asset,
100 mutation notifications while waiting for a missing market section caused
100 layout reads before the change and one scheduled layout read afterward.
This measures JavaScript work in that fixture; it is not a browser timing or an
application-wide latency result. Raw local records are saved under the Git-ignored
`data/benchmarks/navigation_validation.json` and `navigation_mutations.json`.
No paid API calls were made for validation.

Browser automation was unavailable (no browser/native connection). Visual smoothness,
real click-to-content time, keyboard announcements, mobile layouts and slow-network
painting still need browser verification. Restart the application and refresh its
browser tab, then check sport → game → player history → Back; rapid provider changes;
back/forward; a failed request; keyboard-only navigation; and reduced-motion mode.
Record navigation time, callback time and visual transition duration separately.

API references checked against the installed Dash 4.4.1 / Plotly 6.9.0 environment:
[Dash routing](https://dash.plotly.com/urls),
[localized Loading targets and delays](https://dash.plotly.com/dash-core-components/loading),
[callback lifecycle](https://dash.plotly.com/advanced-callbacks), and
[Plotly uirevision](https://plotly.com/python/reference/layout/#layout-uirevision).

## Faster market revisits

URL-to-page routing now has one registered clientside owner. The native hash
bridge only paints the destination; it no longer writes sport/page controls a
second time. Shared headings, sidebar links, research visibility, provider
visibility and refresh-timer policies run locally. Finite presentation tables
are generated from the existing Python descriptions and checked for equivalence.

Data callbacks receive one small versioned workspace signal. Route-aware callbacks
wake when that workspace's route changes; catalog callbacks wake on activation or
their existing controls/timers, rather than every game URL. Returning to a workspace
increments its activation identity so existing freshness checks still run. Inactive
workspaces retain their last signal and selections. The server independently checks
the current page, sport and route before executing scoped work. These stores contain
only navigation metadata; they contain no quotes, predictions, datasets or credentials.

The screen selector consumes compact readiness metadata. Snapshot/category/method
changes cannot reveal another selection's probabilities. Equal exchange quote
payloads and status messages return `no_update`; changed prices, timestamps and
notices still propagate under the original freshness policies.

Lazy rows and probability charts observe the active market subtree. Unrelated
progress updates and Plotly internals do not cause market scans. Relevant section
updates are batched into an animation-frame pass. Inactive backtest/comparison/
performance view timers are stopped with `max_intervals`, while global progress
monitoring and simulation workers continue. Expensive result rendering is scoped
to the active backtesting workspace and resumes when it is revisited.

No model formulas, worker limits, historical cutoffs, season weighting, eligibility,
injury adjustments, seeds, draw counts, cache identity policies or report formats changed
in this follow-up. There are no speculative requests or forecast preloads.

### Repeatable dispatch benchmark

```powershell
.\.venv\Scripts\python.exe scripts/check_navigation_pipeline.py
.\.venv\Scripts\python.exe scripts/benchmark_navigation.py --repeats 5
```

The benchmark executes the registered client route/context functions for 11 warmed
provider/game/history/category events. It replays the resulting callbacks through
real Dash HTTP endpoints with no-op data handlers, against the captured pre-change
callback graph in `scripts/fixtures/navigation_before.json`. It isolates scheduling,
control-metadata serialization and framework dispatch. It does **not** measure model
execution, real quote payloads, total browser requests or click-to-content latency.
Five samples on this Windows workspace produced:

| Measurement, complete controlled sequence | Before | After |
| --- | ---: | ---: |
| Callback invocations | 565 | 119 |
| Request bytes | 286,666 | 97,525 |
| Response bytes | 15,820 | 3,332 |
| Framework dispatch time, median | 150.3 ms | 34.4 ms |
| Direct URL subscribers | 39 | 1 |
| Direct page-selection subscribers | 42 | 2 |

Callback invocations decreased **78.9%**, exceeding the 60% fixture target. Remaining
global subscribers manage lightweight shared UI, including market-structure closing.
The benchmark enforces the invocation reduction; it does not impose machine-dependent
timing thresholds. Raw results are in `data/benchmarks/navigation_dispatch.json`.

All 39 Python check suites passed after adapting legacy HTTP fixtures to the scoped
callback interface (468 unit tests plus project checks). Affected suites were rerun
after the final polling/render guards. Four JavaScript harnesses passed 292 checks;
the registered client presentations also matched 480 reference cases. Dependency,
syntax and whitespace checks passed. Validation includes numerical replay, seeds,
concurrency, saved results, coverage, ranking, exports, current-season weighting,
stale/failed contexts, unchanged quote suppression and manual refresh behavior.

Browser access was still unavailable: no browser was exposed and the native
connection could not be opened. No real-browser speed or visual-smoothness result
is claimed. Restart `index.py` and refresh the browser tab to load the new callbacks
and assets; use the browser scenarios above for visual verification.
