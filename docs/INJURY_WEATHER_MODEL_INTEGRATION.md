# Injury and weather inputs for existing forecasts

Research and code audit: October 27, 2026. This is the original implementation design. The dashboard now implements a narrower, evidence-gated [historical availability overlay](AVAILABILITY_ADJUSTMENTS.md); that document describes the current behavior and limitations. Weather and the remaining integration proposals below are not enabled by this document.

## What the application already has

- `sports_analytics/information.py` caches attributed ESPN headlines and NWS hourly forecasts. Headline keyword matches are not structured participation reports. The location presets are city centers, not stadium coordinates, and the weather source does not determine roof status.
- `models/Player_Stats/Opponent_Adjusted_Matchup/lineup_adjustment.py` accepts timestamped, user-supplied scenario probabilities and component factors. `OpponentAdjustedMarketService(..., scenarios=...)` applies those scenarios. There is no automatic link from the news pages to this input.
- UO and PGE simulate team volume, player roles and finite opportunity allocations. These are natural places to account for unavailable players and replacement workloads while preserving the accounting identities.
- WHP `_point(..., opportunity_override=...)` supports an external workload projection, but its default `beta=1` assigns all production weight to the direct historical estimate. Supplying a minutes projection alone would therefore not change the default point forecast. Workload blending and its uncertainty would need explicit calibration.
- NFL PPP/EPA/ELO forecast scoring from historical efficiency and projected drives/plays. Current conditions need to enter that forecasting path, rather than editing the displayed probability independently.

The local file `data/modeling/nfl/raw/games.csv` already contains `roof`, `temp`, `wind`, `stadium`, and both teams' quarterback IDs/names. The cached 2025 play-by-play also contains `weather`, `roof`, `temp`, and `wind`. `nfl_model_data.py` currently drops these fields when preparing model observations.

Local schedule coverage at the audit:

| Season | Completed games | Nonmissing temperature | Nonmissing wind |
| --- | ---: | ---: | ---: |
| 2024 | 285 | 182 | 182 |
| 2025 | 285 | 190 | 190 |
| 2026 | 106 | 45 | 45 |

These are counts in the saved file, not a claim of live completeness. Indoor games can legitimately lack outdoor weather. Missing outdoor observations must remain distinguishable from indoor conditions.

## Sources and connections

| Input | Primary source | Connection and limitations |
| --- | --- | --- |
| NBA availability | [NBA official injury reports](https://official.nba.com/nba-injury-report-2026-27-season/) | Public timestamped PDF reports linked from the season page. Build a parser with source-document hashes, canonical player IDs, game/team validation and parse-failure checks. The league updates reports throughout the day. Confirm reuse terms and practical request limits before production ingestion. |
| NFL availability | [NFL official injury reports](https://www.nfl.com/injuries/) and official club reports | Public game-status and practice reports. Preserve report date/week and game identity. Practice participation and game status are different fields. These public pages do not establish a documented, guaranteed free JSON API. |
| NFL historical injuries | [nflverse injury data](https://nflreadr.nflverse.com/reference/load_injuries.html) | CSV/parquet downloads with player IDs, practice/report status and modification time. Documentation describes historical coverage since 2009. Verify each requested season's actual release, coverage and revision history; a current final season file is not automatically a pregame snapshot archive. |
| NFL forecast weather | [National Weather Service API](https://www.weather.gov/documentation/services-web-api) | Free public hourly data with an identifying User-Agent; no current paid API subscription is needed. Reuse the existing client/cache, fetch the actual venue point and select the kickoff/game window. U.S. coverage only. |
| NFL historical weather and roof | [nflverse schedules](https://nflreadr.nflverse.com/reference/load_schedules.html) | Reuse saved schedule and play-by-play fields. Observed historical conditions can estimate associations; they are not evidence of what a forecast said before kickoff. |

ESPN headlines remain useful context and source links. An article mentioning a knee injury does not establish that a player is out for the selected game. An undocumented secondary endpoint should not be the only availability source.

## NBA injury integration

1. Resolve official report entries to stable player IDs, current team and the precise scheduled game. Preserve the original status, reason, publication time, retrieval time and source URL. An absent entry is not evidence that the entire roster is healthy.
2. Represent confirmed absences, uncertain participation and documented minutes restrictions separately. Do not assign an arbitrary universal probability to Questionable or Doubtful. Learn participation probabilities from previously archived report/status histories, or show separate explicit scenarios until those probabilities can be calibrated.
3. Estimate missing rotation minutes and replacement roles from earlier observed appearances, lineups and workloads. Model minutes and opportunity shares jointly, with an Other bucket. Team minutes must reconcile with regulation/overtime duration, and player opportunities cannot exceed their team pools.
4. Estimate team offense/defense changes using opponent-adjusted, regularized historical evidence. Simple averages of wins or scoring with/without a star are confounded by opponent strength, rest, roster changes and other absences. Shrink uncertain effects toward no adjustment.
5. Use the change from the injury availability already represented in weighted historical data. If a player has missed all recent games, do not deduct the player's full impact again merely because today's report still lists Out.

Apply these inputs to UO/PGE roles and opportunities, OAM's component-specific scenarios and team outcome efficiency/pace. For WHP, explicitly fit and validate workload blending or condition historical donor weights on availability; changing its configured benchmark silently would make the method's documented meaning incorrect.

## NFL injury integration

Start with starting-quarterback availability, then supported receiver/running-back/tight-end roles, followed by offensive-line and defensive-unit coverage as identity and snap data allow.

- Predict quarterback participation and replacement identity from information available before kickoff. A replacement quarterback needs their own observed efficiency/usage estimate and appropriate shrinkage; an injured starter's established efficiency cannot simply be reused.
- Let receiver and backfield absences change routes/targets/carries and the distribution of team opportunities. Use observed snap/workload evidence; missing target or route measurements are not zero.
- Estimate offensive-line and defensive effects jointly with opponent and personnel controls. Avoid fixed points-per-injury penalties and treating all injuries as equally important.
- Feed game-outcome and player forecasts from the same availability scenario. The same absence must not receive both a full external score deduction and a second full efficiency deduction inside the model.

For both sports, a player's nonparticipation can void a prop under its settlement rules. Keep participation/void probability separate from the conditional probability that an active player's prop wins; do not turn every Out listing into a guaranteed Under.

## NFL weather integration

1. Add verified venue coordinates, country, venue identity and roof information to the scheduled-game context. Neutral/international fixtures must use the actual venue. A home-team city lookup is insufficient.
2. Use NWS hours covering kickoff and the expected game window. Keep sustained wind, gusts where actually supplied, temperature, precipitation probability and conditions distinct. Precipitation probability is not rainfall intensity. Do not invent gusts from the current hourly response.
3. Apply outdoor effects only for confirmed open/outdoor conditions. Dome/closed-roof games should not receive an outdoor wind penalty. Unknown retractable-roof state needs an explicit uncertainty/override path.
4. Fit regularized, sport-specific relationships for pass/run mix, passing efficiency, pace and supported scoring components using earlier observations and available context. Preserve the current-season weighting policy, with older seasons supporting estimation where current data is sparse.
5. Normalize historical weather before projecting today's conditions. Otherwise historical team efficiency already depressed by wind can receive another full wind deduction.
6. Include forecast uncertainty in simulated scenarios. Weather should change the underlying distribution of opportunities and outcomes; recompute spreads, totals and prop settlement probabilities from that distribution.

Historical realized weather can support training. A backtest using the target game's realized wind as though it were a pregame forecast must be labeled a hindsight sensitivity experiment, not a realistic predictive backtest. Start archiving issued forecast snapshots now for credible forward evaluation.

## Shared context, provenance and speed

Introduce one immutable, sport/game-scoped forecast context shared by the game and player services. Suggested storage:

```text
data/information_variable/availability/{sport}/{date}/{source_timestamp}.json
data/information_variable/weather/nfl/{venue_id}/{retrieved_timestamp}.json
data/modeling/context/{sport}/{game_id}/{context_hash}.json
```

Each context needs source URLs/hashes, canonical identities, report/issue/valid/retrieval times, cutoff, roof decision, scenario assumptions, transformation version and coverage status. Use explicit states such as applied, unavailable, stale, conflicting and insufficient historical support. A failed download must not produce a healthy-roster or calm-weather assumption.

Use the existing background refresh queue and local snapshots. Refresh only affected games when context changes. Forecast/model/page/filter caches must include the context hash and adjustment implementation version. Selecting different methods should reuse a single context rather than fetch the same reports repeatedly. Current information must not be fetched while replaying an old game.

For historical predictions, a report must have been available before that prediction's cutoff. Today's retrieval of an old article is not proof that the current revised text was available then. Preserve revision history and distinguish true archived pregame coverage from retrospective reconstruction.

For a calibrated scenario mixture, combine probabilities with `P(win) = sum(P(scenario) * P(win | scenario))`. Predictive variance must include between-scenario disagreement as well as uncertainty within each scenario. Preserve push/void rules and avoid independent scenario draws for correlated player/team channels.

## Validation and rollout

Recommended implementation order:

1. Preserve NFL weather/roof fields, archive venue-specific forecast snapshots and ingest structured injury reports. Show context coverage and timestamps next to forecasts before claiming adjusted accuracy.
2. Add NBA/NFL unavailable-player handling and independently visible availability scenarios. Use the existing OAM scenario hook for explicitly specified, auditable assumptions; this does not by itself establish their empirical effects.
3. Fit and evaluate NBA role/minutes and NFL quarterback/skill-player scenarios, then weather-conditioned NFL pace/efficiency. Connect each adjustment inside the corresponding statistical model or simulation.
4. Compare baseline versus adjusted forecasts on the same chronological cohort. Evaluate MAE/RMSE, bias, probability Brier/log loss, calibration and interval coverage, broken down by news/condition coverage. Select settings on earlier folds and retain a final untouched period.
5. Enable automatic numerical effects only when supported by sufficient historical coverage and out-of-sample validation. With insufficient support, show the report/context and retain an explicitly labeled baseline estimate.

Regression checks should cover delayed reports, game/date mismatches, traded players, confirmed absences, minutes restrictions, uncertain participation, DNP voids, missing news, closed roofs, international games, weather unit conversion, forecast-valid windows, stale/conflicting sources, context cache invalidation, finite opportunity conservation and unchanged predictions when no valid context applies.

The existing models and their current-season weighting remain the starting baseline. No universal injury penalty, fixed wind multiplier or unvalidated status-to-probability mapping is proposed as a production default.
