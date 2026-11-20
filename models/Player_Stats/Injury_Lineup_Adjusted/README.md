# Injury & Lineup Adjustment (ILA)

Choose **Injury & Lineup Adjustment (ILA)** in NBA player forecasts, Quick
Filtering, Backtesting → Player Stats, or Strategy & Research. ILA supports
points, rebounds, assists, made threes, steals, blocks, turnovers, and supported
combinations. It reports minutes and participation separately from Over, Under,
and Push probabilities conditional on playing.

ILA reads the latest saved NBA injury packet available by the quote cutoff.
The default retrieval-age limit is 60 minutes. Confirmed absences are removed
from each applicable team rotation. Questionable and other uncertain labels
identify players whose presence can be relevant to historical role estimates.
Without supplied scenario weights, their participation comes from the saved
rotation patterns.

Each simulated team uses 240 regulation minutes. Player arrays preserve the
same scenario across both teams and across prop statistics. ILA estimates role
and shot conversion from matching combinations of teammate presence, with
recency and season weighting. Matching shots borrow a pooled shooting prior,
and role samples blend with the player's broader history. When the exact
combination is sparse, the model can use one teammate with sufficient evidence
in both states. It prioritizes absent players with larger historical roles and
uses pooled rates when neither cohort has sufficient coverage. Rebounding, assists, and other event pools use
saved box-score proxies.

## Optional lineup scenarios

Add a `lineup_scenarios` object to a saved snapshot or save the object under
`data/modeling/player_stats/injury_lineup_adjusted/lineups/<game_id>.json`.
Use the ID from the saved quote or replay. Omit the `espn:` prefix from the file
name. A file may contain an array of dated versions. The most recent eligible
version at the cutoff is the only version used.

```json
{
  "available_at": "2026-11-19T20:30:00Z",
  "scenarios": [
    {"label": "Plays with a limit", "probability": 0.35,
     "teams": {"BOS": {"active": ["Example Player"], "minutes_caps": {"Example Player": 22}}}},
    {"label": "Sits", "probability": 0.65,
     "teams": {"BOS": {"out": ["Example Player"]}}}
  ]
}
```

Use exact player names that resolve to the current team in saved boxes. An
`active` player is required to participate. A `minutes_caps` value only limits
minutes, so pair it with `active` for a required limited appearance. Use
`out` for 0 minutes. Probabilities are supplied pregame inputs for combined
lineup scenarios and must sum to one across the matchup. A confirmed absence
at the cutoff takes precedence. An infeasible scenario prevents estimates for
the affected team.

## Run and replay

```powershell
python -m models.Player_Stats.Injury_Lineup_Adjusted saved_snapshot.json
```

Use `--prepare` to prepare missing public player and team box scores. Live
forecasts and chronological replays both read timestamped injury archives
and scenario versions. A replay saves the source timestamps, projected
minutes, participation, and matched role weight. It evaluates recorded
appearances. The regulation model uses participation inferred from complete
game boxes and does not reconstruct five-player stints or fit probabilities
from official status labels.
