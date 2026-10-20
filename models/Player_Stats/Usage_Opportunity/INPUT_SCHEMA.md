# Usage & Opportunity input contract

All rows need `game_id`, `entity_id`, `entity_name`, `team`, `event_date`,
and preferably an explicit `available_at`. IDs remain strings. Dates must be
valid and earlier than the intended prediction cutoff. Exact player/game
identities must be unique. Optional `season` and `season_type` record source scope.

Player data comes from the existing saved player repository; NBA/NFL team data
comes from saved actual team datasets in `DataService`. Public preparation is
available through this package's CLI. No data download occurs while forecasting.

## NBA

Both team and player rows require `min`, `fga`, `fg3a`, `fgm`, `fg3m`, `fta`,
`ftm`, `oreb`, `dreb`, `ast`, `stl`, `blk`, `tov`. Team `min` is total player
minutes, at least 240; player `min` may be fractional. Counts are observed event
totals, not percentages. Two-point attempts/makes are derived by subtraction.

Canonical team codes match the application's NBA codes, e.g. BOS and DET.
Generated targets are `pts`, `reb`, `ast`, `fg3m`, `stl`, `blk`, `tov` and the
supported point/rebound/assist combinations. Tracking chances and potential
assists are not accepted as silently inferred fields.

## NFL

Team rows require `attempts`, `carries`, `sacks_suffered`, `targets`, `receptions`,
`receiving_yards`, `rushing_yards`. Player rows require `attempts`, `carries`,
`targets`, `receptions`, `receiving_yards`, `rushing_yards`, `completions`,
`interceptions`. The shared nflverse adapter maps `passing_interceptions` when
the canonical interception column is absent. Recorded zero stats are valid;
unobserved event families must not be replaced with invented zeros in imports.

Team aliases WAS/WSH, LA/LAR and JAC/JAX are normalized using existing rules.
Counts must satisfy player <= aligned team opportunities, receptions <= targets,
and interceptions <= incomplete attempts. Negative net yardage is allowed.
Zero-event yardage must be zero. TD/red-zone columns are not connected in this
version, so UO does not offer a proxy scoring-TD forecast.

## Snapshot and outputs

Use an app-saved market CSV or canonical snapshot JSON with sport, game_id,
home_team, away_team, pulled_at, commence_time and rows. Each row contains group,
market_key, subject, selection, line, odds and updated. NBA/NFL names must match
the existing canonical mappings. Historical replay additionally requires
pregame snapshot timestamps and actuals `game_id,subject,market,value`.

Forecast histograms carry mean/median/SD/interval summaries and opportunity/
efficiency diagnostics. Quote evaluations contain win/loss/push, fair-price
probability, fair odds, raw and optional no-vig market probability, Edge and
expected net return per dollar. Keep these outputs separate from the immutable
market quote CSV and the provider's usage ledger.
