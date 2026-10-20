# Pace & Game Environment input contract

Common fields: `game_id, entity_id, entity_name, team, opponent, event_date`.
Explicit timezone-aware `available_at` is preferred. Game/player and game/team
identities must be unique. Sources must use the same game IDs for player/team
joins. Both reciprocal team rows with matching event dates are required.

NBA team and player boxes need `min, fga, fgm, fg3a, fg3m, fta, ftm, oreb, dreb,
ast, stl, blk, tov`. Teams also need `pts` on both sides or finite `plus_minus`
values that agree as opposite margins. Team minutes are five times elapsed
game minutes; actual duration must agree between both sides. Counts are integer,
nonnegative observations; only minutes may be fractional. Conversion successes
cannot exceed attempts. Possessions are derived estimates, not tracking data.

NFL teams need `attempts, carries, sacks_suffered, targets, receptions,
receiving_yards, rushing_yards, completions, interceptions`, plus `pf` (or `pts`)
and the reciprocal opponent. Player efficiency and participation channels use
the existing UO contract. `passing_interceptions` maps to `interceptions` through
the existing canonical adapter. Yardage can be negative; counts cannot.

Optional saved nflverse PBP needs `game_id, posteam, play_type, qtr, qb_dropback,
qb_scramble, sack, pass_attempt, two_point_attempt, drive`; `play_deleted` is
respected when present. Player scramble allocation additionally uses observed
`rusher_player_id` matching the box GSIS entity ID. Internal joined team columns
are `pge_reg_plays, pge_ot_plays, pge_pass, pge_attempt, pge_scramble, pge_sack,
pge_other, pge_drives`. Player `pge_scramble` is an observed event count; an
observed covered game without a matching scramble event has zero. Missing PBP
coverage remains missing. Do not populate unknown routes/snaps with zeros.

The snapshot uses `sport, game_id, home_team, away_team, pulled_at,
commence_time, rows`. Rows use `group, market_key, subject, selection, line,
odds, updated`, and the standard app CSV reader. Market keys use the canonical
existing NBA/NFL mappings. TD/scorer-order outcomes are not supported by PGE.

Backtest actuals CSV: `game_id, subject, market, value`; market is the canonical
non-alternate player key, and value is a finite observed integer. Historical
snapshots must precede game start. Source event and availability timestamps
must precede the forecast cutoff. Late data corrections require a real as-of
archive to reproduce; today's downloads cannot establish historical publication
times. See README for all lookbacks, defaults, formulas and fallback behavior.
