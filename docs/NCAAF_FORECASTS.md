# College history and FanDuel forecasts

NCAAF now supports PPP, EPA and ELO for alternate spreads and totals. The chosen
method controls probabilities, fair odds, Edge, criteria, four probability charts
and the latest historical game label. The parlay slip uses default college PPP
and WHP. Historical Data remains team-only; selecting a school also prepares
its player boxes for market forecasts.

## Scope and saved files

Selecting a school prepares the selected season and two prior seasons. Markets
reuse those CSVs and prepare only missing matchup schools. The app does not
request all past FBS seasons. One school's advanced defense supplies its
opponent's actual team side; no opponent-season request is needed for that pair.
Previously selected schools accumulate a reusable, deduplicated local cohort.

`data/modeling/ncaaf` contains:

- `team_games_YEAR_SCHOOL_HASH.csv`: paired scores, plays, actual drives where
  available, signed provider PPA, dates, availability, opponents and venue.
- `player_games_YEAR_SCHOOL_HASH.csv`: observed player/game IDs and box statistics.
- `raw/`: reusable scoped games, advanced statistics, player boxes and ID lookups.
- `transfers/`: requested players' prior-school records and lookup manifests.
- `forecasts/`: estimates keyed by method, configuration, code, inputs and cutoff.
- `schools.json`: official school/mascot/abbreviation aliases; ambiguous matches fail.

CFBD requests use the server's `CFBD_API_KEY`, never expose it to the browser,
and consume CFBD quota rather than Odds API credits. Existing source files are
reused until **Refresh from source** for the selected school or CLI `--refresh`.
Source status and forecast-preparation status are separate so a modeling-source
failure cannot hide the already loaded Historical Data.

```powershell
python -m sports_analytics.ncaaf_model_data --teams LSU Auburn --start 2026-10-24T23:00:00Z
```

Preparation uses CFBD `/games`, `/stats/game/advanced`, `/games/players` with
school/year scope. `excludeGarbageTime=false` retains the full-game basis.
When advanced data is denied, `/games/teams` can supply play volume as passing
attempts plus rushing attempts; NCAA rushing already includes sacks. Missing
PPA blocks EPA for that game, while valid volume can support PPP/ELO. Missing
production and PPA are not replaced by fabricated zeros.

## Separate college methods

`models/Game_Outcome/PPP/NCAAF_PPP.py` fits opponent-adjusted log-link total
scoring per play, with recency weights and ridge shrinkage. Projected plays
average own offensive plays and opponent defensive plays. It models scoreboard
points; it does not claim a separate offensive/return/defensive scoring split.

`models/Game_Outcome/EPA/NCAAF_EPA.py` fits signed CFBD game PPA offense/opponent
effects and a nonnegative scoring-per-play bridge. Negative PPA is preserved.
It uses provider-defined qualifying plays, not invented NFL EPA counts. Missing
PPA stays unavailable. This college PPA baseline is separate from NFL EPA and
NBA event EPV.

`models/Game_Outcome/ELO/NCAAF_ELO.py` uses college-specific Elo settings, season
carry, ridge power margins, positive scoring per play and fitted reconciliation.
An explicit internal adapter reuses the shared scoring kernel; CSV drive fields
remain actual drives rather than being overwritten with play counts.

Initial defaults: 180-day recency half-life, 10 cohort training games, 8 minimum
chronological calibration games (up to 120), and 3 prior appearances for each
current matchup school. Calibration folds require one earlier appearance per
participating team; sparse opponents receive regularized ratings. Regular and
postseason weeks are separated internally. These are research settings for a
selected-school cohort, not a full FBS conference hierarchy or tuned betting model.

The earlier of snapshot and kickoff sets the cutoff. Results must be strictly
available before it; the cutoff's UTC game date is also excluded. Availability
defaults to start plus eight hours, a conservative proxy rather than a historical
publication archive. Delayed records cannot enter earlier folds. Later source
corrections cannot be reconstructed without a true as-of archive.

Each method uses college score errors for a normal margin/total distribution
with continuity-corrected integer pushes. Full-game margin removes the tie bin
because college overtime resolves ties; this is not an explicit OT simulator.
Fair probabilities exclude pushes; Edge subtracts raw market implied probability.
Input/code/config changes invalidate affected estimates. Future-only additions
do not change an earlier snapshot's inputs.

## Player history and coverage

WHP prefers college CSVs to legacy JSON histories. Its college configuration
uses up to 12 recent appearances, at least four target observations and four
walk-forward errors. Three lookup years provide prior-season history early in
a season, without lowering uncertainty-calibration requirements.

For a quoted player in the active tab, exact saved player ID is checked against
CFBD's official team stints. Only applicable prior-school seasons are requested.
Personal transfer rows load only for IDs already observed in the matchup's
history, and every game remains subject to the cutoff. Same-name IDs are not
fuzzy-matched. Cross-school WHP is still a historical baseline, without a
new-team role adjustment.

Rookies, missing targets and insufficient calibration can remain unavailable.
Anytime/total TD needs an authoritative all-scoring `touchdowns` target; rushing
and receiving TDs cannot establish every return/defensive TD. Team-defense
scorer labels and first/last scorer order are not individual WHP outcomes.
Market prices remain visible and missing estimates include a reason on hover.
UO, OAM and PGE are now also selectable for NCAAF. Their separate college
opportunity, defensive-component and full-game pace calculations use official
team denominators saved as `player_team_games_YEAR_SCHOOL_HASH.csv`. See
[NCAAF player methods](NCAAF_PLAYER_METHODS.md) for exact settings, new-season
behavior, caching, supported markets and limitations.

## Verification and references

Checks cover paired source rows, zero scores, missing PPA, scoped CSV restart
reuse, aliases, independent methods, future/delayed-result exclusion, cache
identity and college method/history labels. A saved LSU/Auburn example used
63 unique games / 126 team rows, 32 calibration games per method and 15 player
distributions after targeted transfer preparation. Browser checks use real
saved quotes, real forecasts, four graphs, criteria, WHP passing estimates and
college parlay values with external requests blocked. These checks establish
data flow and implementation consistency, not predictive profitability.

Primary references: [CFBD advanced statistics](https://api.collegefootballdata.com/api/stats),
[player IDs and team stints](https://api.collegefootballdata.com/api/players),
[coverage and qualifying-play definitions](https://api.collegefootballdata.com/data-availability).
