# Premium dark presentation

The application retains its native Dash components, hash routes, mounted shell,
localized loading, lazy markets, and existing calculation services. This update
does not add API requests, callbacks, dependencies, remote fonts, model fits, or
initially mounted graphs.

## Shared styles

- `sports_analytics/assets/style.css` defines the palette, spacing, radius and font
  tokens. Existing page styles use those tokens for common surfaces and text.
- `sports_analytics/assets/terminal.css` applies compact shell, panel, control,
  market, responsive and accessibility conventions under `.app-shell`. Routing
  visibility stays in the existing navigation styles and callbacks.
- `sports_analytics/presentation.py` supplies the shared Plotly template and fresh
  table style dictionaries. Its palette is checked against the CSS. The template
  retains Plotly's native trace defaults and changes visual properties only.
- Player PMF/CDF charts read CSS tokens when an opened market renders them; their
  fitted arrays, axes, push handling, tails and lazy observer behavior are unchanged.

The desktop sidebar is 196px, narrows to 168px below 1100px, and becomes grouped
navigation above the content below 768px. Sport and category tabs keep horizontal
scrolling instead of becoming full-width stacked tabs. Wide quote tables retain
horizontal scrolling. Mobile controls have 44px targets. Court/field dimensions,
chart intervals, observed values, saved-report formats and exports are unchanged.

The palette distinguishes actions, probabilities, comparisons, positive/negative
values and warnings. Signed edge values retain their signs; color supplements the
number. Missing/zero edges stay neutral. Shared text-token contrast is at least
7.51:1 on the three main surfaces. This token check is not a complete rendered-page
accessibility audit.

## Repeatable offline verification

Use the installed project environment; no application dependency installation is
needed. From PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts/check_presentation.py
.\.venv\Scripts\python.exe scripts/benchmark_presentation.py --output data/benchmarks/presentation.json --native-reference
.\.venv\Scripts\python.exe scripts/benchmark_dashboard.py --repeats 5
```

For before/after work, capture the presentation benchmark with `--output` before
editing, then use `--compare <before.json>` afterward. It verifies identical
callback registrations, mounted graph count, analytical chart signatures and table
contents/definitions. Chart signatures discard visual properties, retaining data,
axis semantics, hover values, intervals, interaction identity and court/field
geometry. The optional interleaved native-theme reference isolates theme overhead
on the current code under similar CPU conditions. It is not a baseline of an older
application version.

The dashboard benchmark measures the largest locally saved NBA category and blocks
network requests. Its timings measure Python component construction and JSON
serialization, not browser rendering or click-to-content latency. The 1,589-row
Combos fixture was captured before this update; measurements and regression-suite
results are recorded below.

Measured locally on the existing Windows environment (network blocked):

| Measurement | Before styling | After styling |
| --- | ---: | ---: |
| Registered callbacks | 167 | 167, identical registration signature |
| Initially mounted graphs | 8 | 8 |
| Initial layout JSON | 259,868 bytes | 262,242 bytes (+0.91%) |
| Large category initial JSON | 146,707 bytes | 146,971 bytes (+0.18%) |
| Large category build + JSON median, 5 samples | 14.251 ms | 15.015 ms |
| Large category Python allocation peak | 1,777,375 bytes | 1,764,405 bytes |
| Synthetic game backtest build + JSON median | 98.220 ms (15 samples) | 109.983 ms (21 samples) |

Separate timing runs vary with local CPU conditions; these results do not show a
speed improvement. An interleaved same-run comparison of native versus terminal
theme on current code measured 114.625 ms versus 116.399 ms (21 samples per theme,
about 1.5% difference). Market payload growth remains below the 5% budget, and the
prior lazy-loading reduction versus eager rendering remains 96.87%.

All five analytical chart/table fixture signatures matched their pre-change
baselines, including court/field geometry. All 40 Python check suites passed
(473 unit tests plus the project checks), as did 295 direct JavaScript checks and
the offline navigation dispatch benchmark. The Python suites include numerical,
interval coverage, ranking, export, concurrency, history, loading and navigation
regressions. Raw records are in the ignored `data/benchmarks/presentation*.json`
files; no paid or other external API requests were needed.

## Browser acceptance still required

The computer-use connection is unavailable in this environment. No screenshots,
browser latency improvements, keyboard interaction results, mobile rendering or
200% zoom results are claimed. Offline CSS checks validate token parity, contrast
and basic structure; they do not replace a browser CSS/layout audit.

When browser access is restored, review at 1440px, 1024px, 768px, 390px and 200%
zoom. Check each sport/provider, source-column comparisons, player history,
research, performance summaries, news, API usage and P&L. Include long names,
negative odds/edges, missing quotes, selected bets, open dropdowns/date pickers,
empty/partial/error states, pending loaders, the progress monitor and bet slip.
Use keyboard-only access and reduced motion; confirm focused controls and table
scrolling remain reachable and overlays do not cover the current action.

Technical references: [Dash Graph](https://dash.plotly.com/dash-core-components/graph),
[Plotly templates](https://plotly.com/python/templates/), and
[WCAG contrast](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html).
