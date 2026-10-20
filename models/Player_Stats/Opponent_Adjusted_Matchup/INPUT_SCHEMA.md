# Opponent-adjusted matchup input contract

OAM reads the same saved team/player history as UO, with the additional requirement
that league-wide team rows identify their actual opponents. Required identity
fields are `game_id, entity_id, entity_name, team, opponent, event_date`; explicit
`available_at` is preferred. Game/team and game/player identities must be unique.
All data used by a forecast must be strictly available before its cutoff.

## NBA team/player channels

Team rows require `fga, fgm, fg3a, fg3m, fta, ftm, oreb, dreb, ast, stl, blk, tov`.
Team and quoted-player inputs also need `min` for the shared opportunity engine.
Counts remain integer observations; minutes can be fractional. Possessions are
estimated from actual box-score counts, not supplied as a fabricated tracking field.

NBA matchup features separately model attempt counts per estimated possession,
made counts per attempt, and additional count channels per possession. A current
club's actual team-game totals must align with player appearances by game ID.

## NFL team/player channels

Team rows require `attempts, carries, sacks_suffered, targets, receptions,
receiving_yards, rushing_yards, completions, interceptions`. The existing adapter
maps `passing_interceptions` to the canonical interceptions field when needed.
Quoted players need the same UO workload and efficiency channels. Net yardage can
be negative, but event counts must be nonnegative and physically consistent.

Optional observed `position` labels enable WR/TE/RB/QB cohorts; FB/HB map to RB.
A cohort is used only when all recorded members have complete required fields.
No position, route, snap, alignment or coverage label is inferred from a name.

## Saved quotes and scenarios

Snapshot CSVs/JSON use the app's standard schema: sport, game_id, home_team,
away_team, pulled_at, commence_time, rows. Each row has group, market_key, subject,
selection, line, odds and updated. OAM supports NBA and NFL only. Whole-number
lines can push. TD/order markets do not get an unrelated probability proxy.

Optional scenario JSON is a list of `offense, available_at, probability, factors`.
Offense is a canonical club code; factors map exact component keys to positive
relative effects. Probabilities sum to one per offense. The availability time
must include a timezone and precede the cutoff. These are supplied scenarios,
not an automatically verified injury feed. Effects are relative to the existing
defensive baseline. Use positional component names when adjusting a fitted
positional efficiency, e.g. `nfl:catch:WR`.

## Evaluation and artifacts

Actuals CSV uses `game_id,subject,market,value`, with one finite integer outcome
per identity. Replay snapshots must be genuinely pregame and sport-consistent.
Artifacts record neutral projection, adjusted mean, net multiplier, component
factor diagnostics, full empirical mass, win/loss/push probabilities, fair odds,
Edge and EV. Defense artifacts additionally store chronological actual/expected
labels and frozen historical factors. Keep source quote CSVs immutable.
