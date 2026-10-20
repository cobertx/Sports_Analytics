# College player forecasts: UO, OAM and PGE

In a saved NCAAF FanDuel game, **Player Forecast Method** offers WHP (default),
Usage & Opportunity (UO), Opponent-Adjusted Matchup (OAM), and Pace & Game
Environment (PGE). The selection controls player probability, fair odds, Edge,
criteria highlighting, and latest historical performance date. Game Outcome
Forecast Method independently controls PPP/EPA/ELO for spreads and totals.

## Inputs and saved-data reuse

`NcaafModelData.prepare_player_inputs` requests CFBD `/games/teams` only for
the two matchup schools, the current football season and two prior seasons.
It reuses saved `/games` schedules and writes paired, official totals to
`data/modeling/ncaaf/player_team_games_YEAR_SCHOOL_HASH.csv`. These contain
attempts, completions, net passing yards, rushing attempts/yards, interceptions,
scores, opponent IDs and timestamps. It does not reconstruct a team total by
summing players quoted by the sportsbook. Missing source fields remain missing.

Player inputs come from the existing observed `/games/players` CSVs. The
market page prepares missing school/player CSVs and denominators on first use;
subsequent views read the saved inputs. No Odds API call is made by forecasting.
**Refresh from source** on a previously prepared school refreshes its player-team
totals too. The three method CLIs also support explicit preparation/refresh:

```powershell
python -m models.Player_Stats.Usage_Opportunity --sport NCAAF --prepare --teams "LSU Tigers" "Auburn Tigers" --start 2026-10-24T23:00:00Z
```

Replace `Usage_Opportunity` with `Opponent_Adjusted_Matchup` or
`Pace_Game_Environment` for the other entry points. Add `--refresh` to update
source history. `CFBD_API_KEY` is read server-side from the local environment.
CFBD quota is separate from Odds API credits.

## College simulation

The college engine is `models/Player_Stats/Usage_Opportunity/ncaaf_model.py`.
Each existing strategy service dispatches college snapshots to this engine,
with a different context for UO, OAM or PGE. NBA/NFL continue through their
existing engines.

1. Set the cutoff to the earlier of snapshot and kickoff; exclude games not
   strictly started and available before it. Availability uses the existing
   game-start + eight-hour proxy, not an archived publication feed.
2. Match the quoted name to an unambiguous saved player ID and latest observed
   matchup school. Opportunity ratios use **current-school appearances only**.
   A receiver does not need quarterback fields, and absent off-role counts
   are not replaced by zeros.
3. Draw recent team workloads and aligned player/team role shares. Define NCAA
   plays as passing attempts + rushing attempts. NCAA sacks already count in
   rushing attempts; adding NFL sack counts would double-count them.
4. Split plays into pass attempts and carries. Draw team completions with a
   recency-weighted beta-binomial conversion model (Jeffreys prior 0.5/0.5).
5. Allocate attempts among observed QBs, carries among observed rushers, and
   completions among observed receivers. Each pool has an explicit **Other**
   bucket for unquoted/unmodeled participants. Conflicting sampled shares are
   normalized and every simulated pool conserves its integer team count.
6. CFBD standard player boxes lack targets. Receiving workload therefore uses
   reception share of team completions, not fabricated targets or catch rates.
   Draw receiving/rushing yards from weighted yards per event and empirical
   game residuals scaled by the square root of projected event count. Negative
   yardage is allowed; zero events imply zero yards.
7. QB completions are a hypergeometric allocation of the common receiver catch
   pool, bounded by each QB's attempts. QB passing yards share the common
   receiver yardage pool by completions; rounding residual goes to Other.
   This uses team/receiver efficiency rather than a separate QB yardage model.
   Observed QB interceptions per incompletion generate interception samples
   when enough explicit observations exist.

One set of 12,000 integer simulations supplies every line for a given player
statistic. Over/Under settle separately with exact integer push mass. Fair
probability is `win / (win + loss)`; fair odds use that probability. Edge is
fair model probability minus raw market implied probability. A missing channel
remains unavailable without hiding valid estimates for other players/channels.

## Lookbacks, new seasons and transfers

| Input | College default |
| --- | --- |
| Player role window / half-life | 12 complete appearances / 3 games |
| Efficiency window / half-life | 24 complete appearances / 6 games |
| Team mix window | 16 complete games |
| Minimum complete observations per channel | 3 |
| Source season range | Matchup season and two previous seasons |
| Latest-player staleness limit | 450 days |

Football season lookup changes in March, retaining January/February in the
preceding season. Windows cross seasons when the player remains at the same
school. Early-season transfers need three complete current-school role games;
old-school shares are not assumed to transfer. WHP remains available as a
separate baseline that can use exact-ID prior-school performance. No invented
rookie role or minimum-sample override is used.

## What distinguishes the methods

**UO:** Bootstrap team workloads and observed role shares with recency weights,
then apply conversion/yardage uncertainty. Shared bootstrap quantiles link the
two teams' volume scenarios as a declared positive-dependence assumption.

**OAM:** Reuse `ScheduleAdjustedDefense` and its chronological actual/expected
fits for college pass share, completion rate, receiving yards per completion,
rushing yards per carry, and interceptions per incompletion. Offense and cohort
priors use only results available before each historical game. Neutralize
historical component rates before applying the upcoming defense's uncertain,
shrunk factor, so the matchup is applied once. Use Bayesian bootstrap defensive
games, a 16-game defense window, four-game half-life, 10-game defensive prior,
six-game offense prior, at least 16 prior feature rows, and at least three
usable defense games. Sparse individual components remain neutral and appear
as absent profiles. Both matchup defenses must have a usable profile. The
reference cohort is the accumulated selected schools and their paired opponents,
not the entire FBS. College positional, conference, lineup and scheme effects
are not inferred.

**PGE:** Reuse the lagged, partially pooled ridge `PaceModel` on paired full-game
NCAA plays and scoring margins. It needs at least 24 earlier games for warmup
and 24 later chronological error games. A single school's three years may be
insufficient; both matchup schools usually expand the cohort. The current
defaults use 256 cohort games, 64-game scenario half-life, pooling prior five,
ridge penalty 10, and 25% recency-only smoothing in the score-margin role kernel.
Paired held-forward residual scenarios feed both teams' volumes and score
scripts; role draws are conditioned on those scripts before allocation.
College overtime is included in the source full-game volume, **not separately
estimated as regulation plus overtime**. PGE does not also apply OAM efficiency
factors. Too little paired history produces a specific preparation message.

## Persistence and limits

Each method stores histograms under its existing ignored
`data/modeling/player_stats/<method>/forecasts/ncaaf` namespace. Identity includes
snapshot, all settings, eligible player/team inputs and implementation hashes.
Source/config/code changes invalidate affected forecasts. Future-only input
additions do not affect an earlier forecast. Saved quotes remain reproducible
until inputs or code change; source corrections are not historical as-of data.

Supported observed channels include passing yards/attempts/completions/
interceptions, rushing yards/attempts, receiving yards and receptions. These
three opportunity models do not invent red-zone or first/last-scorer inputs.
Touchdown/scorer markets therefore retain their market prices and an unavailable
reason. Live injuries, tracking and clock/scheme models are not connected.
UO/OAM/PGE Monte Carlo intervals are research estimates; they do not have a
fitted college betting-probability recalibrator. PGE's forward pace errors and
OAM's chronological fits do not establish profitable prop predictions.

Local checks cover actual-source field mapping, scoped CSV reuse, missing
channels, NCAA sack conventions, pool conservation, all three method prices,
integer pushes, future exclusion and disk cache restores. Browser checks use
saved college quotes without external requests.

Source contract: [CFBD schedules and team/player game boxes](https://api.collegefootballdata.com/api/games).
