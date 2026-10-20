# Local college modeling history

School-scoped `team_games_YEAR_SCHOOL_HASH.csv` and
`player_games_YEAR_SCHOOL_HASH.csv` files live here. `raw/` retains reusable
CFBD responses; `transfers/` retains exact-ID prior-school player histories;
`forecasts/` freezes estimates by input/code/config/cutoff hashes. Generated
data and credentials are not committed to Git.

Choosing a school in Historical Data prepares its selected season and two
prior seasons. Market pages reuse these files and prepare missing matchup
schools. Player tabs request only needed players' prior-school boxes.

UO/OAM/PGE player tabs also save `player_team_games_YEAR_SCHOOL_HASH.csv`
with official CFBD passing/rushing totals for both sides of each scoped game.
These denominators are reused across the three methods. See
[college player methods](../../../docs/NCAAF_PLAYER_METHODS.md).

```powershell
python -m sports_analytics.ncaaf_model_data --teams LSU Auburn --start 2026-10-24T23:00:00Z
```

Use `--refresh` to refresh public history for those schools. Ordinary browsing
reuses saved sources. See [data and model details](../../../docs/NCAAF_FORECASTS.md).
