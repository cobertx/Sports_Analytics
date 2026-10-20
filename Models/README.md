# NFL forecasting models

The NFL saved-market view also supports **EPA** through its **Forecast method** dropdown. PPP remains selected by default. See the EPA section below for its independent efficiency model and scoring bridge.

`PPP.py` implements the supplied *Football Betting Arbitrage & Forecasting: Offensive/Defensive Points per Play* document, principally sections 1–5. It is independent of Dash and can be imported as `Models.PPP` or run directly.

```powershell
.\.venv\Scripts\python.exe Models\PPP.py --seasons 2025 2026
.\.venv\Scripts\python.exe Models\PPP.py --seasons 2025 2026 --home SF --away WSH --as-of 2026-10-19T23:35:23+00:00
```

The first command prepares public NFL observations. The second also fits a forecast and saves its parameters, uncertainty estimates, training cutoff and chronological validation residuals under `data/modeling/nfl/forecasts`. Add `--refresh` to refresh public source data. Use team abbreviations such as `LAR`, `WSH`, `SF`, `CHI`, or `NE`.

## Data

`sports_analytics/nfl_model_data.py` joins these public sources:

- [nflverse schedules and final scores](https://github.com/nflverse/nfldata/blob/master/data/games.csv), also used by the historical-data adapter.
- [nflverse team box scores](https://nflreadr.nflverse.com/reference/load_team_stats.html), the existing team-statistics source. Numeric box-score fields are retained with a `box_` prefix.
- [nflfastR play-by-play](https://nflreadr.nflverse.com/reference/load_pbp.html), used for offensive/defensive plays, drives, and scoring attribution.
- [PFR player snap counts distributed by nflverse](https://nflreadr.nflverse.com/reference/load_snap_counts.html), joined as participation coverage and maximum-player-snap checks. Individual player snaps are not summed or presented as exact team totals.

Raw files, `team_games_<season>.csv`, and a source manifest are saved in `data/modeling/nfl`. Manifests include source URLs, retrieval timestamps, SHA-256 hashes, coverage counts and scoring/play definitions. These generated files are ignored by Git. Downloads are cached for 24 hours and consume no Odds API credits. Available seasons with saved data can still be used when another requested season is unavailable; coverage notices are shown with the forecast.

One observation is a team in a completed regular-season or postseason game. IDs are preserved as strings; franchise abbreviations are normalized. Duplicate identifiers, incomplete possession fields, scoring mismatches and incomplete final-score coverage are rejected. Each game has both teams. A final score in PBP must agree with the completed schedule.

Offensive plays include passes (including sacks), runs, kneels and spikes. Kicks, two-point tries, penalties classified as no-play, and administrative records are excluded. Offensive points include offensive touchdowns, their conversions and made field goals. Defensive/special-teams touchdowns and their conversions, safeties and other residual scoring are handled separately. Thus final scoreboard points are not incorrectly divided by offensive plays as offensive PPP.

## Calculations

- **2.2:** `matchup_ppp(O, D, league)` exposes the paper's multiplicative baseline, `O * D / league`.
- **2.3:** The default estimate jointly fits offensive strength, opposing defensive vulnerability and a home-field term with a log link and offensive-play exposure. A regularized Poisson mean objective accepts zero scoring observations; no Poisson variance assumption is used for market probabilities. Team effects are centered for identification. Time weights have a 180-day half-life; the default ridge coefficient is 20. These are explicit baseline configuration choices, not values optimized on a held-out test set.
- **3:** Both teams share an expected-drive forecast estimated from their own/opposing historical drives. Matchup plays per drive are estimated separately. Expected points equal expected plays times matchup PPP plus a shrunk estimate of non-offensive scoring. Neutral venues do not receive the learned home-field term.
- **4.1:** Entire weeks are held out in chronological order. Each held-out game's prediction uses only earlier available observations. At least 50 training games, four prior appearances per selected team, and 40 held-out games are required. The last 250 held-out score-error pairs estimate covariance and mean forecast bias. Margin variance is `Var(home) + Var(away) - 2*Cov`; total variance uses `+ 2*Cov`.
- **5:** Raw market break-even probabilities use the paper's American-odds formula. Whole-point lines use integer continuity corrections to estimate wins, losses and pushes. Displayed PPP probability is `P(win)/(P(win)+P(loss))`; its fair American price therefore accounts for refunded pushes. Hover values show all three unconditional settlement probabilities. Market probabilities retain the bookmaker margin; they are not described as no-vig probabilities.

This implements the document's **normal baseline in section 4.1**, rather than claiming to implement its preferred drive-by-drive simulator in section 4.2. Football key-number masses and ties are approximated by integer bins. Weather, injuries, roster changes and full possession/overtime dynamics are not modeled. Final-score training observations include actual overtime. The results apply to full-game markets; regulation-only markets would need separate settlement logic.

## Saved market integration

The NFL Game Lines view shows market odds, market-implied probability, PPP probability and PPP fair odds for every saved alternate spread/total selection, including selections under Show more. Player-prop layouts are unchanged. Four charts below the markets separately compare the home team's handicap, the away team's handicap, Over, and Under. Each chart contains a teal PPP probability line and a blue market-implied probability line, with transparent areas extending to zero. Both series use the same saved market lines; unavailable quotes remain gaps. Spread labels include the team and handicap, such as `Baltimore - 3.5`. The charts appear in a two-column grid on wide screens and stack on smaller screens. These are line-probability curves, not a claim that probabilities across alternate lines sum to one.

The rightmost **Edge** column is PPP probability minus raw market-implied probability, displayed in signed percentage points. Above the matchup, **Estimated Probability (%)** defaults to 75 and **Edge (%)** defaults to 5. Click **Apply Criteria** to highlight rows strictly exceeding both thresholds. Editing an input does not change the applied criteria until the button is clicked. Qualifying market sections and Show more groups expand automatically. Missing estimates never qualify, and invalid inputs preserve the previous applied criteria. Applying criteria uses saved quotes and cached model estimates without pulling live odds.

`PPPMarketService` freezes each estimate using its game identity, model version/configuration and the earlier of snapshot time and kickoff. Forecasts are reused after a restart. Only earlier completed game dates enter training, with conservative next-day availability. Post-kickoff snapshots are explicitly labeled as comparisons with a pregame estimate. The actual target result never enters training.

Source archives retrieved today can contain later statistical corrections. The cutoff filtering prevents future-game leakage, but does not reconstruct the exact vintage of every statistic known at an old snapshot timestamp. Forecast manifests preserve which prepared inputs were used. Numerical formula, scoring, push, cutoff and dashboard tests establish implementation correctness; predictive calibration still requires evaluation on untouched future games.

## NFL EPA model

`EPA.py` implements the NFL baseline from the supplied *EPA Sports Forecasting: NFL, NBA, College Football* research note. Run it independently with the same interface:

```powershell
.\.venv\Scripts\python.exe Models\EPA.py --seasons 2025 2026 --home BAL --away CIN --as-of 2026-10-19T23:35:23+00:00
```

The prepared team-game CSVs retain signed nflfastR EPA sums and valid-play counts for offense and opposing defense, with passing and rushing components kept separate. Spikes are grouped with passing and kneels with rushing. The selection of plays matches the existing PPP play definition. Missing EPA is recorded as missing and incomplete coverage prevents an EPA estimate; it is never replaced with zero. Provider-defined EPA preserves the provider's scoring, turnover and period-boundary conventions. `expected_points_added()` exposes the paper's signed-reward identity for formula verification.

The calculation follows these sections:

- **2–3:** Separate passing/rushing weighted ridge models estimate an intercept, offensive team effects, opposing defensive vulnerability and home-field effect. EPA remains signed: negative EPA allowed represents better defense. Both sets of team effects are centered. Weighting each team-game/play-type mean by its valid play count gives the same ridge normal equations as the corresponding per-play fit with these constant team/context predictors. Recency weights use the configured 180-day half-life.
- **3.3 and 4.1:** Pregame passing share blends the offense's earlier pass share with the opposing defense's earlier pass share. Matchup passing/rushing EPA advantages are combined using that predicted mix. Both teams share the drive-volume projection; plays per drive are matched separately. The target game's actual mix or play count is never used to predict it.
- **4.2:** The trained offensive-scoring bridge is `drives * baseline_points_per_drive + conversion * expected_plays * EPA_advantage`. Its neutral baseline and nonnegative conversion coefficient are fitted jointly against historical offensive points. EPA advantage excludes the rating intercept, keeping the league baseline out of the efficiency adjustment. Defensive/special-teams scoring and other non-offensive points are estimated separately. `scoring_bridge()` exposes this formula.
- **7–8 and 10:** EPA's own chronological held-out score residuals supply the bias adjustment and joint covariance, from which total and margin standard deviations are calculated. The common spread/total pricing interface handles integer pushes, conditional win probabilities, fair American odds and Edge. None of the paper's illustrative score means or standard deviations are used as live estimates.

The EPA configuration uses a rating ridge penalty of 200 and a conversion-coefficient penalty of 25. These are explicit initial configuration choices, not hyperparameters claimed to have been optimized on untouched validation games. The training and held-out-game minimums match PPP. EPA uses the shared historical cutoff, pace, validation and settlement code while overriding efficiency estimation and the fitted scoring bridge.

EPA forecasts are archived separately as `data/modeling/nfl/forecasts/epa_<identity>.json`, using the same saved-snapshot cutoff and restart behavior as PPP. Selecting EPA changes the probability/fair-odds columns, Edge calculation, all four comparison charts and criteria highlighting. Existing applied thresholds are evaluated against the selected model. An unavailable EPA estimate displays an EPA error alongside the saved quotes; it does not substitute a PPP estimate. Switching models makes no live-odds request.

This implements the paper's **first-stage NFL scoring bridge and normal-distribution baseline**. Starting field position has a league-level baseline; weather, projected quarterbacks, injuries, drive-state transitions and rule-specific overtime simulation are not added as unavailable inputs. Key-number/push masses remain approximate. NBA and college football require their own separately fitted efficiency, pace and settlement models and are not enabled by this NFL selector.
