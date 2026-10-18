# Sports Analytics

A Python Dash workspace for analyzing historical **NBA, NFL, and NCAA Football (NCAAF)** team and player statistics, and developing forecasts of future performance.

## Run locally

Requires Python 3.11 or newer. From this repository in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe index.py
```

Open **http://127.0.0.1:8050**. Use `--port 8051` to change the port, or `--debug` for development. The default server listens locally. Stop it with Ctrl+C.

With the virtual environment activated, you can also run `python index.py`. The original `python app.py` entry point remains available.

Running `index.py` directly also selects this project's `.venv` automatically when it exists, so an IDE Run button using system Python can still launch the app. VS Code's default interpreter is configured to use `.venv`. Install the requirements with the setup command above when creating a new environment.

Select NBA, NFL, or NCAAF, then choose a season and team/player statistics. The selected scope loads automatically when you open the page or change a filter, populating **Team or player** and **Statistic** from the actual observations. You can also click **Load historical data** to retry. Selectors are disabled with a source-status message when no data can be loaded. The game log supports filtering, sorting, and pagination. **Export loaded dataset** downloads all loaded rows and numeric statistics as CSV.

Starting the Python server does not download data until you open the page. Opening the dashboard loads its default scope, using the local cache when available. No sample statistics are presented as real observations.

## Data sources

| Sport | Source and access | Imported data |
| --- | --- | --- |
| NBA | [NBA Stats](https://www.nba.com/stats), with historical box scores from [SportsDataverse's NBA Stats archive](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nba_stats_team_boxscores). No API key. | Team or player game logs: points, rebounds, assists, shooting, turnovers, and other box-score statistics. |
| NFL | [nflverse-data](https://github.com/nflverse/nflverse-data), using the same weekly CSV releases as [`nflreadr::load_stats`](https://github.com/nflverse/nflreadr/blob/main/R/load_stats.R). No API key. | Weekly team or player statistics: passing, rushing, receiving, defense, kicking, and available derived metrics. Uses actual team datasets rather than sums of player stats. |
| NCAAF | [CollegeFootballData](https://collegefootballdata.com), using its documented [`/games`, `/games/teams`, `/games/players`](https://apinext.collegefootballdata.com/api/games) endpoints. API key required. | Completed FBS game box scores, including team scores and numeric category-specific player stats. |

### College football setup

Request an API key at <https://collegefootballdata.com/key>. Add it to the ignored `.env` file:

```dotenv
CFBD_API_KEY=your-key-here
```

Alternatively set `$env:CFBD_API_KEY` in PowerShell. The server reads the key; it is not stored in the browser or cache. The college scope starts with `Michigan`; change it to another school or clear it and choose a week. These box-score endpoints require a team, week, or conference filter; the app supports team and week. A school filter returns only that school's observations. A week filter without a school returns teams/players across the requested FBS games. Each NCAAF import makes two requests (game metadata and box scores). Historical availability, quotas, and endpoint access depend on the provider and your key.

### Seasons and source limitations

- NBA seasons use their **starting year**: `2024` means `2024–25`. Football uses the season year even for postseason games played in the next calendar year.
- Regular season and postseason are imported separately. Current seasons can be incomplete. The default is the prior season.
- NFL weekly releases are requested for 1999 onward; some statistics have shorter coverage. NFL observations use **season/week ordering**; event dates remain blank rather than guessed. Historical franchises retain the provider's team abbreviations. Unassigned events without a player ID are excluded from player histories; team totals come from the separate team release.
- NCAAF imports support 2001 onward where the provider has box scores; earlier or incomplete records may be unavailable. Non-numeric fields such as possession time are omitted. Recognized compound counts such as completions/attempts and third-down conversions/attempts are split into numeric fields.
- NBA historical seasons from 1996 onward first use SportsDataverse's published NBA Stats box scores and schedules (files use the season's ending year). If the archive is unavailable, the app tries NBA Stats directly. The selected source is recorded in the cache and displayed on the page. Player DNP records are excluded from appearance histories.
- Metrics retain their source units. NBA shooting fractions, football percentages, and similarly named categories are not automatically harmonized across providers. Missing values remain missing.

## Cache and historical imports

Normalized datasets are saved atomically in **`data/cache/sports.sqlite3`**, ignored by Git. A cache entry records the query, source, UTC retrieval time, and compressed dataset. Entries are reused for 24 hours. **Refresh from source** bypasses the age check. If a download fails and an older copy exists, the dashboard explicitly reports that it is using stale data. Failed or empty downloads do not replace valid cached data. Override the location with `SPORTS_CACHE_PATH` in `.env` or the environment.

Import several seasons without opening the UI:

```powershell
.\.venv\Scripts\python.exe -m sports_analytics.ingest NBA --seasons 2022 2023 2024 --entity player
.\.venv\Scripts\python.exe -m sports_analytics.ingest NFL --seasons 2023 2024 --entity team
.\.venv\Scripts\python.exe -m sports_analytics.ingest NCAAF --seasons 2023 2024 --entity player --team Michigan
.\.venv\Scripts\python.exe -m sports_analytics.ingest NFL --seasons 2024 --entity player --season-type postseason --refresh
```

The command exits nonzero for failed imports or stale fallback data. CLI and dashboard share the cache. The dashboard explores one imported season at a time; use the data/service modules to assemble multiple seasons for model training.

## Forecasting foundation

The initial model forecasts the **next observed appearance** as the average of the last 3, 5, or 10 recorded appearances for one team/player and statistic. A historical backtest predicts each appearance using only earlier observations (`shift(1)` before rolling), and reports mean absolute error (MAE) on a full window. No prediction is shown before enough history exists. Missing statistics are excluded rather than filled with zero.

This is a reference baseline, not a trained opponent-aware model. It does not forecast participation, future schedules, injuries, trades, or minutes, and produces no confidence intervals. The model module is isolated so future work can add features, chronological training/validation, and stronger forecasting methods.

## Structure and verification

```text
index.py                       Dashboard launcher
app.py                         App initialization, .env loading, and shared startup
sports_analytics/
  dashboard.py                 Three league sections and callbacks
  assets/style.css             Responsive dashboard styling
  data.py                      Validated queries, HTTP client, provider adapters
  service.py                   Source metadata and SQLite cache
  forecast.py                  Rolling baseline and chronological evaluation
  ingest.py                    Multi-season command line importer
tests/test_core.py             Offline provider, cache, model, and Dash integration tests
```

Run the offline checks:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use representative API-shaped fixtures and never consume API quotas. Live access is a separate check: run an import for a known historical season. Before redistributing downloaded data, review each source's current terms and licensing.
