# NFL modeling data

Run `python models/Game_Outcome/PPP/PPP.py --seasons 2025 2026` from the project root to prepare team-game observations from public schedules, team box scores, play-by-play and player snap counts. Add `--refresh` to update the cached sources.

- `raw/`: original source CSVs and retrieval/hash metadata.
- `team_games_<season>.csv`: validated team-game observations for modeling.
- Signed offensive/defensive EPA sums and counts are included, with passing/rushing components for `models/Game_Outcome/EPA/EPA.py`.
- `team_games_<season>.manifest.json`: source provenance, coverage and data definitions.
- `forecasts/`: archived PPP parameters and chronological validation residuals.
- EPA forecasts use a separate `epa_` filename prefix; PPP archives retain `ppp_`.

Generated datasets are ignored by Git. See [the model documentation](../../../models/README.md) for formulas, usage and limitations.
