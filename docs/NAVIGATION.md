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

## Market and player-history UX follow-up

This pass keeps the existing router, premium theme and calculation services.
Quote identity is now checked independently of model identity: switching methods
keeps matching prices visible while hiding old probabilities, fair odds, edges,
criterion highlights and distribution charts. A refresh of the same completed view
can retain its content with an Updating label. Empty in-progress quote loads still
use placeholders; empty/error responses terminate them and expose the existing
snapshot/refresh controls. Failed exchange refreshes retain old quotes only when
sport, game, category and snapshot selection match, with their original timestamp.
No paid pull is retried automatically.

Historical results carry a separate analysis identity containing route, dataset
key/revision, season, phase, week/source-team scope, entity, statistic, rolling
window and displayed team filter. The chart's existing navigation metadata remains
compatible. A local guard keeps the chart, hit-rate cards and game log hidden until
that identity agrees with the current controls. The controls remain usable. Linked
player selection hydration must settle before the result is revealed; deliberate
user selections can override the link. Career charts have a separate identity for
their actual all-season scope and independent pending/error states. Zoom remains
stable for an unchanged analytical selection.

`market-return` session metadata now uses `{v: 2, entries: {"provider:SPORT": ...}}`.
There is at most one entry for each of three providers and three sports. Entries
contain only validated routes, snapshot/category/method selections, prompt/section
identifiers and rendered row counts. Legacy FanDuel sport-keyed entries remain
readable. Provider links return to the remembered game; explicit game URLs take
precedence. Archive reads validate the filename against available saved snapshots.
Returning from history reopens the prompt and requests previously expanded rows
through the existing 50-row callback. Removed snapshots/categories/prompts fall
back to the available view/header with a short notice. Restoration is scoped to
the active provider and stops on user interaction; unrelated progress and Plotly
mutations do not drive market restoration scans.

Destination and calculation feedback uses one lightweight clientside status owner.
Repeated polling with the same phase does not repeat its live announcement. The
existing 150 ms loader delay remains; no artificial navigation delay, full-page
fade, extra initially mounted graph, dataset browser cache or dependency was added.
Placeholders reserve space for history, career and market sections.

### Follow-up measurements

Offline Windows measurements, with external requests blocked:

| Measurement | Before this UX pass | After |
| --- | ---: | ---: |
| Original 11-event server callback count | 119 | 119 |
| Control request bytes, full sequence | 97,525 | 98,173 |
| Control response bytes | 3,332 | 3,332 |
| Large category initial JSON | 146,971 bytes | 146,971 bytes |
| Initial layout JSON | 262,242 bytes | 262,739 bytes |
| Initially mounted graphs | 8 | 8 |

Five warmed dispatch samples measured a 45.818 ms median for the original sequence.
The additional six Kalshi/Polymarket game → history → Back events measured 62
server callback invocations, 53,638 request bytes, 1,736 response bytes, and a
24.242 ms dispatch median. These are framework/control-plane timings with no-op
data handlers, not browser click-to-content, real quote loading, or forecast times.
The large category render/serialization median was 15.314 ms over five samples;
its Python allocation peak was 1,764,305 bytes. No browser speed improvement is
claimed from these measurements.

The five existing chart/table analytical signatures matched their prior baselines.
Numerical, distribution, ranking, export, coverage, concurrency and cache checks
remain in the regression suite. The added offline fixtures exercise archive
returns without a live quote call, scoped quote/model failures, retained refreshes,
full history identities, career readiness, manual selections during hydration,
lazy-row restoration, and rapid/delayed navigation.

The final combined validation passed all 41 Python check suites (480 unit tests
plus project checks), 354 direct JavaScript checks, and the offline dispatch and
payload budgets. Affected suites were rerun after the final error-state handling.
Python syntax, dependency consistency and whitespace checks also passed.

```powershell
.\.venv\Scripts\python.exe scripts/check_navigation_ux.py
node scripts/check_navigation_ux.js
.\.venv\Scripts\python.exe scripts/benchmark_navigation.py --repeats 5 --exchange-history
```

Raw local reports: `data/benchmarks/navigation_ux_*.json` (Git-ignored). Browser
access remains unavailable. Real keyboard behavior, mobile/zoom layouts,
slow-network painting, focus announcements and user-perceived latency still need
verification in the browser; the controlled DOM harness does not establish them.
