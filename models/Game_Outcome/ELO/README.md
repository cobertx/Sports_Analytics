# ELO / Power Rating: implementation and methodology

This folder implements a public-data **NFL and NBA pregame research baseline**
from the supplied Elo/Power Rating note. It combines sequential Elo strength,
opponent-adjusted points-based power ratings, a separate positive scoring/pace
model for totals, and a fitted reconciliation into a normal score-error baseline.
Elo alone cannot determine total points. NFL and NBA have independent inputs,
parameters, calibration and frozen forecast archives.

Read [the implementation guide](ELO_Implementation_Guide.pdf) for a rendered
version of this manual. The unchanged [source research PDF](Elo_Power_Rating_Models_NFL_NBA_CFB.pdf)
is a reference, not a description of every implemented feature. Source SHA-256:
`2952d6f5810021bcebadbd8f96c7a7860f738bab403a3e7918ea0efabd669883`.
References below use the PDF's section/equation numbers.

## 0. Lookback and new-season behavior

### 0.1 What history is used?

The live service derives a sport season S from the earlier of saved snapshot
time and kickoff/tipoff. NFL rolls over in March: January/February are the prior
NFL season. NBA rolls over in October and its year denotes the season's starting
year. The requested history is **S-1 and S**, including regular season and
postseason, excluding preseason. A snapshot in October 2026 requests NFL
2025/2026 or NBA 2025-26/2026-27.

All valid, available requested games can enter the final fit. There is **no hard
last-four or last-ten game window**. Those are minimum appearances for each
target team, not caps. League counts are distinct games; a complete game has
two team rows but only one paired score error.

| Setting | NFL | NBA |
| --- | --- | --- |
| Minimum league training games | 50 | 100 |
| Minimum earlier appearances per target team | 4 | 10 |
| Minimum chronological calibration pairs | 40 | 100 |
| Maximum calibration pairs | Latest 250 | Latest 300 |
| Minimum non-tied games for Elo probability calibration | 40, with both home wins/losses represented | 100, with both home wins/losses represented |
| Historical holdout unit | Entire season/week | Entire UTC game date |
| Power/scoring recency half-life | 180 calendar days | 90 calendar days |
| Conservative source availability | Game UTC date +36 hours | Game UTC date +2 days |

Every observation must have `available_at < cutoff` and a UTC game date before
the cutoff's UTC calendar date. Same-day and not-yet-available results are
excluded. Missing teams, inconsistent pairs, invalid scoring, too few error
pairs or nonpositive error variance prevent a forecast; saved market quotes
still display without invented probabilities.

### 0.2 What if the season has no games yet?

Earlier-season history is retained. NFL Week 1 in 2026 can use 2025 games for
ratings, drive efficiency and chronological calibration. NBA opening night in
2026-27 can use 2025-26 box scores. A team need not have four NFL or ten NBA games
in the new season; its total eligible history across requested seasons must
meet that threshold. Available current-season results join the old cohort.
Failed current-season sources are reported, and successful requested history
can still be used. There is no automatic fetch of S-2, lower minimum, fabricated
schedule, football-to-basketball fallback or PPP/EPA fallback.

Elo has an explicit additional offseason prior (PDF equation 5):

```text
R_new = 1500 + season_carry * (R_old - 1500)
NFL / NBA / NCAAF season_carry = 0.2
```

It is applied once at each observed season transition, and at a target new
season even if no new-season game has been observed. Skipped seasons use
`season_carry ** number_of_seasons`. A final 1600 rating becomes 1520 for each sport. The initial reference is 1500; unfamiliar
teams start there in the Elo replay but still need minimum actual appearances
before a market forecast is permitted. Carry values are declared baseline
settings, not externally supplied current power rankings.

Power and scoring fits use `2^(-age_days/half_life_days) * prior_season_weight^seasons_old`, with default `prior_season_weight = 0.2`, without a hard
season reset or fixed old/new blend. NFL games 180/360 days old receive raw
recency weights 0.5/0.25 before the season discount; NBA games 90/180 days old receive 0.5/0.25 before the season discount. Offseason days
count. These weights are not normalized to make the latest game equal 1;
fixed regularization can matter more as all observations age. Sequential Elo
uses result updates and offseason carry, rather than those recency weights.

Roster turnover, trades, quarterback changes and injuries do not create a
personnel adjustment. Ratings track franchise abbreviations and observed game
history. This is a material limitation of cross-season carryover.

### 0.3 Training counts, calibration and refreshes are different

A dataset with exactly the minimum league games does not guarantee sufficient
calibration. Earlier historical predictions also needed enough earlier training
and both teams' appearances. Early games provide warmup, not error pairs.
The live request remains two seasons; CLI `--seasons` can explicitly expand a
standalone experiment. CLI defaults are also derived from `--as-of`, avoiding
an accidental mismatch between historical cutoffs and the computer's year.

Data adapters can download missing public observations; they reuse valid caches
and report source failures/stale fallbacks. NFL preparation validates nflverse
scoring, plays/drives and supporting joins. NBA uses the existing box-score
adapter and its public cache refresh policy. ELO does not require NBA EPV event
archives or an additional API key. Refreshing history is separate from pulling
paid market quotes. A frozen estimate for the same cache identity is deliberately
reused even after underlying history is refreshed; provenance hashes do not
participate in that identity.

## 1. Files, dependencies and commands

| File | Responsibility |
| --- | --- |
| ELO.py | Configurations, logistic Elo helpers, delayed-availability replay, power/scoring fits, chronological calibration, normal forecast and CLI. |
| __init__.py | Public imports, including ELOModel and ELOConfig. |
| __main__.py | Enables `python -m models.Game_Outcome.ELO`. |
| ../../../sports_analytics/elo_service.py | Separate NFL/NBA saved-market services and frozen artifacts. |
| ELO_Implementation_Guide.pdf | Implementation manual generated from this README. |

No new runtime dependencies are required beyond existing NumPy/pandas. The
optional documentation tools are in `requirements/requirements-docs.txt`. From the project root:

```powershell
# NFL, abbreviations match the existing NFL data and team mapping.
python -m models.Game_Outcome.ELO --sport NFL --seasons 2025 2026 --home BAL --away CIN --as-of 2026-10-20T02:49:50+00:00

# NBA uses starting-year seasons, and full-game box scores including overtime.
python -m models.Game_Outcome.ELO --sport NBA --seasons 2025 2026 --home DET --away BOS --as-of 2026-10-20T03:48:47+00:00

# Rebuild only this implementation guide.
python scripts/build_model_docs.py --methods ELO
```

`--refresh` refreshes public historical inputs. Direct execution with
`python models/Game_Outcome/ELO/ELO.py ...` also works. CLI output is a readable matchup JSON
under `data/modeling/<sport>/forecasts/elo_<home>_<away>.json`; this is distinct
from the hashed live-service cache described in section 7.

## 2. Mapping the PDF to this implementation

| PDF section / equation | Implementation | Scope |
| --- | --- | --- |
| 1, equations 1/13 | reconcile_scores and forecast properties | Team means, margin and total satisfy the accounting identities. |
| 2.1-2.2, equations 2/4 | elo_probability, elo_update, ELOModel._elo | Sequential neutral ratings and zero-sum result updates; tie result 0.5; MOV modifier fixed at 1. |
| 2.3, equation 5 | _elo season transition | Offseason regression toward 1500; no roster prior terms. |
| 3.1, equations 6-8 | _fit power coefficients | Weighted ridge incidence model with centered team strengths and learned point home edge. |
| 3.2 | Not implemented | Retrospective recency fit is used; no Kalman/state-space uncertainty process. |
| 3.3, 4.1, 5.1-5.2 | _fit scoring coefficients and _raw | Positive joint O/D rates, shared projected drives or possessions, separate NFL non-offensive scoring. |
| 6 | Not enabled | No CFB conference/division/roster hierarchy or saved CFB quote pipeline is claimed. |
| 7.1, equations 24-30 | _calibrate and ELOForecast / NBAELOForecast | Covariance-based normal margin/total baseline with common integer-bin/push settlement. |
| 7.2-7.3 | Not implemented | No integer joint score draws, drive alphabet, possession simulator or explicit overtime engine. |
| 7.4, equations 36-38 | Shared pricing functions / MarketProbability | Conditional no-push fair probability and American odds; Edge uses raw market break-even probability. |
| 8, equation 39 | _calibrate and predict | Calibrated Elo normal-equivalent margin blended with power margin using a fitted bounded weight, preserving the O/D total before score bias correction. |
| 10-12 | Holdout loop, diagnostics, services and tests | Chronological first-stage records, calibration fits and versioned snapshot archives; untouched-future economic validation remains separate. |

## 3. Sequential neutral Elo

`elo_probability` uses `1/(1+10^(-D/c))`, where D is home neutral rating minus
away neutral rating plus temporary home advantage. Neutral sites set that
advantage to zero. With a 100-point neutral gap and scale 400, probability is
about 64.0065%. The PDF's 1570 versus 1510 with +45 home advantage gives
64.6669%; this is a helper check, not a live matchup parameter.

For the initial settings, NFL/NBA K are both 20, scale is 400, temporary home
Elo is 50/60, and starting rating is 1500. These are configurable starting
values, not fitted production constants. With equally rated neutral teams,
a home win moves ratings from 1500/1500 to 1510/1490. The update is zero sum.
Margin of victory does not multiply the update; this follows the paper's
recommended `g=1` starting benchmark and avoids an unvalidated blowout drift rule.

`_elo` groups games by historical UTC date. Each date is predicted from frozen
ratings. An update's change is computed from its pregame expected result, then
queued until the declared availability time is strictly before the next
forecast. Earlier unpublished results cannot enter ratings. At a target cutoff,
all earlier available pending results are applied and any required offseason
regression is performed. Home advantage is never permanently added to ratings.

The raw Elo expectation is a diagnostic/prior, not the official probability in
the market table. For NFL it treats a final tie as half a result. Later logistic
calibration uses non-tied games to target a binary conditional win probability.
The final displayed probabilities come from the reconciled score law, including
its push handling, rather than mixing unrelated probability outputs per line.

## 4. Power margin and the separate totals engine

### 4.1 Ridge power ratings in points

For each earlier home game, the design has a +1 home-team coefficient and -1
away-team coefficient. A home-venue column is 1 for a non-neutral game and 0
otherwise. The target is the actual home margin. Recency-weighted squared error
plus ridge penalties is solved with `numpy.linalg.solve`, not a matrix inverse:

```text
margin_g = r_home - r_away + learned_home_points * nonneutral_g + error_g
beta = solve(X' W X + penalty, X' W observed_margin)
```

Team penalties are 10 NFL /20 NBA and the learned point home-edge penalty is 1.
Team strengths are centered to zero. Positive power points mean stronger than
league average. Ridge stabilizes schedule connections, but this is not CFB's
conference-specific hierarchical prior. Elo's abstract units and these point
ratings are not interchangeable.

### 4.2 Positive opponent-adjusted offense and defense

The scoring model has an intercept, a signed home/away term, offensive-team
indicators, and negative opposing defensive-quality indicators. It minimizes a
regularized Poisson mean objective with opportunity exposure:

```text
rate = exp(intercept + offense[team] - defense_quality[opponent] + home_effect)
E[ordinary_points] = historical_opportunities * rate
objective = sum(w * [opportunities*exp(eta) - ordinary_points*eta])
            + 0.5 * sum(penalty * coefficients^2)
```

NFL opportunities are **offensive drives**, unlike PPP's play-level efficiency.
NFL ordinary points are offensive TDs/conversions/field goals from the shared
validated data builder. NBA opportunities are full-game estimated possessions
and all points are modeled as ordinary points. The NBA box-score estimate is
the two-team mean of `FGA + 0.44*FTA - OREB + TOV`; it is not an exact event count.

Newton steps with backtracking fit the positive mean; this does not assert a
Poisson final-score distribution. NFL/NBA scoring team penalties are 20/50;
home penalties are one tenth of that and the intercept is unpenalized. Both
effect sets are centered. Positive defense quality lowers opponent scoring.

### 4.3 Project opportunities from earlier games

The shared expected count is the mean of four recency-weighted histories:
home offense count, home defense count, away offense count and away defense count.
Each side gets its own fitted scoring rate. NFL additionally shrinks each team's
non-offensive points toward the league average with two units of prior weight;
NBA's additional component is zero because its complete scores are already in
the fitted response. The preliminary total is the sum of those team scores.

NFL offensive/defensive drive counts must agree across paired team rows and
ordinary plus non-offensive points must match actual final scores. NBA inputs
must have complete consistent paired box scores and no tied final score. No
future realized pace or scores, market prices, injury reports, inferred lineups
or weather adjustments enter the target forecast.

The power margin supplies the initial margin candidate; the O/D model supplies
the total. The O/D margin is retained as a diagnostic. Before blending,
`home=(total+power_margin)/2` and `away=(total-power_margin)/2`, so there is one
set of score means. The coherent reconstruction bounds the mean margin within
plus/minus the mean total to prevent negative means. This bound is not a repair
for impossible individual samples from a continuous normal distribution.

## 5. Chronological calibration and Elo reconciliation

### 5.1 Create forward first-stage records

NFL holds out complete season/week groups; NBA holds out complete UTC dates.
At each holdout, fit Elo, power and scoring using only earlier available games.
Store the power margin, O/D total and margin, pregame raw Elo probability and
actual paired scores. Only eligible fits/teams contribute records. Computation
visits newest folds first to stop once enough recent records exist, but each
fold's training cutoff remains strictly earlier; sort and retain the latest
250 NFL /300 NBA error pairs. No future fitted coefficient is reused in an old
fold. A selected target game's actual result never enters its own forecast.

### 5.2 Fit calibration parameters on those records

The calibrator performs a declared one-pass sequence:

1. Reconstruct raw team means from each held-out O/D total and power margin.
   Compute the raw margin-error standard deviation `sigma_initial`.
2. Exclude final ties, require the sport's minimum non-tied calibration sample
   and both binary outcomes, and fit a logistic intercept/slope to raw Elo logits.
   Ridge strength is 10 toward intercept 0/slope 1; slope is constrained to be
   nonnegative. This is calibration of earlier first-stage forecasts, not a
   new independent team-strength model.
3. Convert calibrated Elo probability into an equivalent continuous-normal
   margin `elo_margin = sigma_initial * inverse_normal_cdf(p_elo_calibrated)`.
4. Fit `w` in `[0,1]` by least-squares margin error across that calibration cohort:
   `final_margin = (1-w)*power_margin + w*elo_margin`. The closed-form unconstrained
   one-parameter solution is clipped to that interval. No betting quotes/ROI
   enter fitting. A fitted zero weight is a permitted result, not an error.
5. Reconstruct score means using the same O/D total and blended margin. Their
   paired actual-minus-predicted score errors provide bias and covariance.

This is a single reconciliation pass. The initial raw sigma used in the Elo
conversion is recorded separately from the final residual sigma. There is no
hidden iterative fixed-point procedure. The final target's Elo/power/scoring
components are refitted on all eligible history, the fitted transformation and
blend are applied, and the learned score biases are added. Neutral targets use
common bias instead of a home/away differential. Negative bias-adjusted means
are floored at zero; final margin/total are recomputed from the adjusted means.

These records were held out of their respective first-stage fits. They are
then used to fit logistic calibration, blend, bias and covariance; they are
**not an untouched evaluation of that final combined model**. K, scale, home
Elo, carry, ridge strengths and half-lives are initial hyperparameters. Further
chronological tuning and a separate untouched season are needed to assess
calibration and forecast performance. No betting-profit claim follows from
the arithmetic, engineering tests or in-cohort calibration diagnostics.

## 6. Distribution, probabilities, fair odds and Edge

Let C be the sample covariance of the paired home/away score errors:

```text
mu_margin = adjusted_mu_home - adjusted_mu_away
mu_total  = adjusted_mu_home + adjusted_mu_away
variance_margin = C_hh + C_aa - 2*C_ha
variance_total  = C_hh + C_aa + 2*C_ha
```

Finite positive variance is required. The resulting normal margins/totals are
the PDF section 7.1 benchmark, not the section 7.2 production simulator.
Historical final scores and volumes include actual overtime; there is no
separate regulation/OT generator. NBA spread settlement inherits removal and
renormalization of the impossible final zero-margin bin; NFL ties remain
possible. Football key-number frequencies are approximated by normal bins.
The marginals are not a fully coherent discrete joint score simulator.

For a selected team's handicap s, cover is `team_margin+s > 0`. A total Over
requires total > line; Under reverses it. Integer line equality is a refunded
push. The shared `MarketProbability` exposes unconditional win/loss/push and:

```text
p_display = win / (win + loss)
fair_american = -100*p_display/(1-p_display) if p_display > 0.5
              +100*(1-p_display)/p_display otherwise
market_implied = abs(a)/(abs(a)+100) for a negative American quote
                 100/(a+100) for a positive American quote
Edge = p_display - market_implied
```

Endpoint model probabilities have no finite fair price. Market break-even
probability is raw and includes vig. For example, win/loss/push 0.60/0.35/0.05
gives 63.1579% displayed probability and fair odds about -171.43. Edge is a
percentage-point difference, not the paper's expected-profit formula and not
an arbitrage proof. It is not multiplied by 100 until display formatting.

## 7. FanDuel Market integration and frozen artifacts

NFL and NBA offer **PPP**, **EPA**, and **ELO / Power Rating**; PPP remains the
default. ELO changes estimated probability/fair-odds column names and values,
Edge, all four charts and criteria highlighting. Both teams' alternate spreads
and both Over/Under totals use the same fitted forecast. Player props have
prices without invented Elo probabilities.

Apply Criteria requires both probability >75% and Edge >5 percentage points by
default. Qualifying rows and their section receive yellow borders. Evaluated
sections with no qualifying rows receive a red border and `no match`; missing
estimates remain unavailable rather than being misclassified. Extra selections
stay collapsed until Show more is clicked, even after criteria are applied.
The bottom Show less control restores the six-selection view. Gold chart
segments and markers identify the same qualifying quoted lines. All these
actions read saved quotes; none pulls odds.

`ELOMarketService` / `NBAELOMarketService` inherit only the existing snapshot
cutoff/cache contract, not PPP fitted parameters. Forecasts are stored under
`data/modeling/nfl/forecasts/elo_<hash>.json` or the corresponding NBA directory.
Identity includes version (initially 1), sport-specific configuration, teams,
cutoff and neutral status. First uncached fits prepare the requested public
seasons and archive source notices, timestamp, team/rating diagnostics,
calibration records, coefficients, covariance and input-file SHA-256 hashes.
ELO caches are separate from PPP/EPA prefixes and preserve the correct sport's
forecast class when reopened. Missing ELO data shows saved prices plus an ELO
error; it never relabels PPP or EPA estimates.

The estimate uses `min(pulled_at, commence_time)`; a quote captured after start
still receives a labeled pregame comparison, not a live in-game forecast.
Reopening a frozen identity does not retrain it or refresh historical sources.
Hashes provide provenance but are not cache-key inputs. A new cutoff,
configuration or model version creates a new identity. Historical data may
contain later corrections; filtering future games does not reconstruct exact
source publication vintages. Only an explicit Pull Live Markets action requests
new betting quotes and can consume Odds API credits.

## 8. Validation and remaining limitations

Offline tests check logistic examples, zero-sum updates, season carry, score
accounting, pushes/no-tie settlement, cutoff and unpublished-result exclusion,
duplicate/insufficient input rejection, separate scoring-driven totals,
sport-correct independent cache restoration and NFL/NBA method-dependent UI,
Edge/highlights/charts. Real saved NFL and NBA snapshots were also forecast
using prepared public histories with all network requests blocked. Those checks
establish functioning integration, not predictive performance.

The optional MOV modifier, dynamic state-space ratings, roster/quarterback,
injury/weather/rest/travel effects, calibrated conditional variance, hierarchical
CFB priors, full joint integer score draws and sport/season-specific overtime
simulators are not implemented. NCAAF remains outside the saved-market forecast
selector because its separate hierarchy, input coverage and quote workflow have
not been connected. Do not substitute NFL calibration for college football.
Evaluate untouched seasons with proper probability scores, reliability plots,
margin/total tail coverage, integer push rates and source-revision sensitivity
before interpreting model differences as actionable betting information.
