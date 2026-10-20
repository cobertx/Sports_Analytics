# Backtesting dashboards

Each NBA, NFL and NCAAF methodology page exposes the actual model’s historical
replay through `sports_analytics/backtesting_service.py`. Configuration and model
documentation remain under the collapsible methodology section.

Select a team (or all teams) for a Game Outcome model. Select one player and one
statistic for Player Stats to keep error units comparable. Optional dates restrict
evaluation targets by inclusive EDT calendar days, not their earlier training history. The game limit chooses
the most recent 5/10/20/50/100 completed saved targets before replay; insufficient
history can cause some of these targets to be skipped. The cards and detailed
skip list distinguish attempted and successfully evaluated games. Defaults and
minimum-sample rules are not relaxed to manufacture results. Larger replays,
especially models with nested calibration, can take several minutes.

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
| 90% interval coverage | Fraction of actual outcomes inside the lower/upper predictive bounds |
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
1.64485362695 predictive SD. Player empirical simulations use their 5th/95th
percentiles; WHP uses inversion of its actual integer CDF, including count and
empirical families. These describe predictive outcomes, not confidence intervals
for the mean. Integer distributions can yield coverage above the nominal 90%.

The game benchmark is the earlier available cohort’s mean home margin or total.
The player benchmark is the mean of its preceding five observed target games.
These baselines never use the evaluated outcome.

## Graphs and persisted runs

Predicted/actual timelines include the predictive band. The agreement scatter
shows a perfect-forecast diagonal and colors points by error. The residual
histogram shows over/underprediction and outliers. Rolling MAE uses up to five
successfully evaluated games and compares the same prior-data benchmark.
Calibration groups settled event forecasts in ten probability bins, comparing
mean forecast probability with observed event frequency and exposing bin counts.
Small or highly selected samples need careful interpretation.

Reports persist in `data/backtesting/<sport>/<group>/<model>`. Cache identity
includes model settings/code, inputs/source file revision metadata, selected
target IDs, scope, dates, statistic, reference line and tolerance. Newly
available evaluation games change the key. Identical runs restore their saved
report; rerunning recomputes after source/config/code changes. Old reports can
be viewed with their saved timestamp and applied scope, even if current controls
or source files have changed. Changing the chart target recalculates point-error
cards without refitting the already stored margin/total forecasts.

The detailed table shows actual/predicted values, signed errors, bounds,
event probabilities and latest training dates. CSV export follows the displayed
margin/total target and includes the underlying paired forecast fields. The app
rejects displaying/exporting a report from another sport or methodology.
