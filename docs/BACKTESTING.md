# Backtesting dashboards

Each NBA, NFL and NCAAF methodology page exposes the actual model’s historical
replay through `sports_analytics/backtesting_service.py`. Configuration and model
documentation remain under the collapsible methodology section.

Select a team (or all teams) for a Game Outcome model. Select one player and one
statistic for Player Stats to keep error units comparable. Optional dates restrict
evaluation targets by inclusive EDT calendar days, not their earlier training history. The game limit chooses
the most recent eligible completed saved targets before replay (1–10,000); insufficient
history can cause some of these targets to be skipped. The cards and detailed
skip list distinguish attempted and successfully evaluated games. Defaults and
minimum-sample rules are not relaxed to manufacture results. Larger replays,
especially models with nested calibration, can take several minutes.

### Evaluation and estimation controls

Changing sport or team/player scope resets the editable **Backtest Data Games**
field to these defaults. Single-player evaluations use the single-team defaults.

| Sport | Single team / player | All teams |
| --- | ---: | ---: |
| NBA | 82 | 1,000 |
| NFL | 34 | 250 |
| NCAAF | 24 | 1,000 |

The separate **Evaluation start** and **Evaluation end** calendar/date fields
accept MM/DD/YYYY. Either can be blank. Both bounds include the selected EDT
calendar day. The newest-game limit applies after date, scope and season filters;
it is a maximum requested sample, not a promise of that many saved/valid forecasts.

**Include playoff / postseason games** defaults to **No**. No uses only verified
regular-season rows for both evaluation and estimation/calibration. Yes includes
regular season and postseason (including NBA play-in games when available and
college bowl games); preseason and unknown phases remain excluded. Older CSVs
recover phase from saved season metadata, filename, NFL schedules, college raw
game archives or standard NBA game IDs. Unknown phases are not guessed regular.

The field also displays how many saved games match the team/player, statistic,
dates and playoff selection. This counts eligible evaluation targets before
model fitting, not guaranteed successful forecasts. Training/calibration checks
may skip some targets. The count does not depend on the requested evaluation cap.

**Lookback Training Data Games (optional)** is an optional maximum number of earlier available
appearances **per team/player**, independent of the number of games evaluated.
The field initially shows its actual default with **(Model Default)** appended.
WHP shows 20/16/12 appearances for NBA/NFL/NCAAF. Opportunity models display
their role / efficiency / team windows; the note explains that order. Game
Outcome models display **All available (Model Default)** because they have no
fixed last-N cutoff. Model defaults keep their usual older calibration/league
prior histories. Blank also preserves the default. Replace the displayed text
with a whole number to impose a maximum input cap. A value of 30 bounds each
forecast's input pool to the newest 30 eligible prior appearances per identity.
The bound rolls forward for each evaluated game and can cross seasons. Game
Outcome models retain only complete pairs for which both sides survive their
individual limits, so a team can have fewer than 30 usable appearances. Player
opportunity models cap players by stable player ID and team histories by team.
The same bounded input pool supplies calibration, league priors and benchmark
forecasts; held-out results never enter it. Model-specific rolling windows may
use fewer games, and original minimum training/calibration checks still apply.
Small lookbacks can therefore produce explicit skipped games or no estimable
forecasts. This control does not tune or relax the underlying model parameters.

Saved reports record the applied playoff setting and estimation cap in the
report caption and cache identity. Editing controls changes the next run; it
does not relabel a previously saved result as if it used the new settings.

## Models and input boundaries

Game Outcome calls the same PPP, EPA or ELO model classes and configurations
used by live markets. NBA PPP means **Points Per Possession**; football PPP means
**Points Per Play**. The EPA pages are named **Expected Points Added (EPA)**;
NBA uses its event-based EPV implementation and NCAAF its provider-PPA bridge.
Final home/away scores are read separately after forecasting. Each forecast
cutoff is the target game’s source event timestamp. Models apply their existing
strict availability filters and fit/calibrate solely on earlier inputs.

Sources are prepared team-game CSVs under `data/modeling/<sport>`. NBA EPA also
requires its saved reconciled games and per-game state-statistics NPZ arrays;
the model filters those state labels to its eligible training games. No bulk
source preparation or HTTP request occurs in backtesting. Sparse college
school/opponent cohorts can legitimately evaluate few games.

Player input discovery reads cached player datasets, college player-game CSVs,
and supported canonical imports. Identity selection uses stable player IDs.
WHP forecasts directly from that player’s earlier target/workload observations.
UO/OAM/PGE call their actual forecast services on historical matchup identities,
with a cutoff immediately before the game. Observed target-game points, yards,
attempts, receptions and outcomes are never passed as forecast features. These
methods additionally require their saved team/opponent/pace inputs. Their
historical result rows carry **no invented sportsbook price**. The optional
reference line is user-specified, fixed across the selected games, and distinct
from an archived market-line backtest.

Missing histories, incomplete matchup pairs, unsupported channels or inadequate
calibration produce explicit skipped-game reasons. Previously prepared college
transfer performance may support WHP; opportunity strategies retain their
current-school role rules. Sources are revised present-day archives with
availability proxies, not a complete historical as-of data warehouse. Some
sources provide only a date rather than an exact tipoff/kickoff time.

## Metrics

One point forecast is scored per evaluated game/target, avoiding duplicate
weight from alternate market lines. Error is **predicted minus actual**.

| Card | Calculation |
| --- | --- |
| Evaluated games | Successful forecasts, with attempted and skipped counts |
| Event accuracy | `P(event) >= 0.5` compared with the binary outcome |
| MAE | Mean absolute error |
| RMSE | Square root of mean squared error |
| Bias | Mean signed error; positive indicates overprediction |
| Error variance / SD | Sample variance (`ddof=1`) and its square root; undefined for one observation |
| Within tolerance | Fraction with absolute error at or below the applied tolerance |
| 80% interval coverage | Fraction of actual outcomes inside the lower/upper predictive bounds |
| Brier score | Mean squared binary probability error |
| Log loss | Mean negative log probability assigned to the realized event, clipped only for numerical stability |
| Baseline MAE | Earlier-data benchmark scored on exactly the evaluated games |
| Pushes / ties | Refunded/reference-equal targets excluded from binary event metrics |

Game event accuracy and probability scores always evaluate **home-team win**,
even when the chart target is total points. NFL ties are excluded from these
binary scores. Player event scores evaluate **Over the applied reference line**;
equal-to-line outcomes are excluded. Without a player reference line, event
metrics/calibration remain unavailable while forecast-error metrics still work.
Fair probabilities condition on non-push settlement, consistent with the models.

Accuracy displays a 95% Wilson interval with its settled sample size. This is a
descriptive binomial interval; repeated teams, players and related games can be
dependent. It is not evidence of independent trials or betting profitability.
No ROI, stake returns or market-performance claim is inferred without archived
prices and the appropriate settlement/selection policy.

Game predictive intervals use the model’s normal margin/total mean plus/minus
1.281551565545 predictive SD (the central 80% interval). Player empirical simulations use their 10th/90th
percentiles; WHP uses inversion of its actual integer CDF, including count and
empirical families. These describe predictive outcomes, not confidence intervals
for the mean. Integer distributions can yield coverage above the nominal 80%.
Old player reports only retained their 90% bounds; these keep their correct
90% label until rerun, rather than estimating unknown 80% quantiles from two
endpoints. Old Game Outcome reports can recalculate 80% bounds from saved SDs.
The separate descriptive Wilson confidence interval for event accuracy stays 95%.

The game benchmark is the earlier available cohort’s mean home margin or total.
The player benchmark is the mean of its preceding five observed target games.
These baselines never use the evaluated outcome.

## Graphs and persisted runs

Predicted/actual timelines show actual outcomes as dots only: blue circles
inside the inclusive predictive bounds and red crosses outside, using compact
4-pixel markers. The prediction line and 80% band remain. A percentage and
inside/total count sit beside the plot within its panel (above it on smaller
screens). The agreement scatter
shows a perfect-forecast diagonal and colors points by error. The residual
histogram uses 20–100 explicit equal-width bins; counts sum to evaluated games.
Its smooth Gaussian-kernel overlay describes the overall error distribution,
rather than interpolating bar heights. Bandwidth uses the smaller of sample SD
and IQR/1.349 when positive, scaled by `0.9*N**(-1/5)`, with a floor of two bin
widths for stability. Single/constant samples also use that floor. The curve
uses 384 grid points and scales density by `N*bin_width`, keeping it comparable
with histogram counts. It is descriptive smoothing, not a forecast distribution.
Rolling MAE uses up to five successfully evaluated games and compares the same
prior-data benchmark. Every evaluated game has its own position, even when
several games share a date. At most seven date/game ticks keep the axis readable;
hover identifies every game. Spline rendering smooths the connecting lines;
it does not add simulated observations or change rolling estimates.
Calibration uses 50 two-percentage-point bins, comparing
mean forecast probability with observed event frequency and exposing bin counts.
Only the binned calibration curve and perfect-calibration diagonal are drawn;
individual binary outcomes are hidden to avoid bands of overlapping dots at
0% and 100%. Those outcomes still contribute to bin frequencies and metrics.
Empty bins are omitted.
Small or highly selected samples need careful interpretation.

The last graph before the prediction table scores **80% interval accuracy**:
an actual outcome at or between the lower/upper bounds is one success, and an
outcome outside is a failure. This applies to the displayed margin/total or
player statistic, independently of event probability, reference lines and
push/tie classification. This graph is distinct from the Event Accuracy card.
It shows four series: the mean across games on each EDT date, plus trailing
10-, 20- and 50-game means. The rolling series advance per chronologically
evaluated game across all dates; early windows use however many games exist.
Daily means are placed at the final evaluated game of each date, with daily
sample counts on hover. Cumulative accuracy and individual success/failure dots
are omitted. Invalid or older 90%-only intervals cannot be scored as 80%; an
older player report requests a rerun rather than relabeling its saved bounds.

## Progress, reusable runs and exports

Run backtest starts a background worker. Dashboard replays run in separate
Python processes, so CPU-heavy fitting does not share the web server's Python
interpreter with Quick Filtering. Both Game Outcome and Player Stats runs use
the same worker slots: two to four simulations can advance concurrently,
depending on CPU count; additional runs wait with an explicit progress message.
Each worker uses one numerical-library thread to prevent competing fits from
oversubscribing the CPU. Model settings, simulation counts, random seeds and
chronological input boundaries are unchanged. Workers use saved local inputs
and the selected cache database; they never import or start the dashboard.
Closing the server terminates its active workers.
Intermediate player-forecast caches are scoped to the run ID, so overlapping
replays do not compete to replace the same files. The parent server alone
updates each method's latest-result marker; completed reports remain reusable.

The progress bar to the right of Export
results updates from completed/attempted games, including skipped targets, and
reaches 100% after results are saved. Preparing inputs remains at 0%; saving is
capped at 99%. A single slow model fit can leave the percentage unchanged until
that game completes. Polling does not rerun the model. Change the controls and
use **Run another backtest** to submit another job, including on the same player
method page. Identical concurrent requests share a job. Progress
is local to the server process; finished reports persist across app restarts.

A **Backtests** monitor stays fixed at the bottom-left, below the sidebar, on
every page and sport tab. It lists all ongoing jobs in the app's server process,
with sport/model, team/player scope, requested game count, current phase and a
percentage bar per run. Jobs waiting for a worker slot show 0% until they can
start. A lightweight one-second poll reads in-memory summaries; it never loads
historical data or starts simulations. Completed/failed jobs leave this active
list, while their existing result/error handling remains on the model page.
Click its header to minimize or expand it. The preference persists within the
browser session, and polling continues while minimized or after navigating away
from the originating model page. The monitor moves above an open parlay drawer;
on mobile it also reserves bottom space so final content remains reachable.

Reports persist in `data/backtesting/<sport>/<group>/<model>`. Cache identity
includes model settings/code, inputs/source file revision metadata, selected
target IDs, scope, dates, statistic, reference line and tolerance. Newly
available evaluation games change the key. Identical runs restore their saved
report; rerunning recomputes after source/config/code changes. Old reports can
be viewed with their saved timestamp and applied scope, even if current controls
or source files have changed. Changing the chart target recalculates point-error
cards without refitting the already stored margin/total forecasts.

Each new successful run additionally writes a readable JSON report and CSVs to
`data/backtesting/<sport>/<group>/<model>/runs/<scope-prefix>_<run-hash>/`.
Game Outcome saves separate margin and total CSVs; Player Stats saves its selected
statistic. The full parameter/source hash continues to identify the canonical
cached report. Repeating identical inputs and parameters loads that report,
marks completion as a saved run and avoids another simulation. Different
lookbacks, dates, lines, tolerances, source revisions or model code get their
own run identity. A failed run does not become a successful cached report.

Both saved and downloaded CSV filenames include sport, strategy, abbreviated
team/player scope and target, Backtest Data Games, numeric lookback or
`model_default`, `reg`/`post`, UTC run timestamp and a run-hash suffix. Names and
folder prefixes stay short for Windows compatibility; JSON retains the full
scope and parameter values. The Export button downloads the displayed target
using the same name, without fitting again.

The detailed table shows actual/predicted values, signed errors, bounds,
event probabilities and latest training dates. CSV export follows the displayed
margin/total target and includes the underlying paired forecast fields. The app
rejects displaying/exporting a report from another sport or methodology.
