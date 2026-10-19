# Sports Analytics

A Python Dash workspace for analyzing historical **NBA, NFL, and NCAA Football (NCAAF)** team statistics, NBA and NFL player statistics, and developing forecasts of future performance. NCAAF is team-only.

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

Select NBA, NFL, or NCAAF, then choose a season. NBA and NFL offer team/player statistics; NCAAF offers teams only and automatically resets the mode to Teams when selected. The selected scope loads automatically when you open the page or change a filter, populating **Team or player** and **Statistic** from the actual observations. You can also click **Load historical data** to retry. Selectors are disabled with a source-status message when no data can be loaded. The game log supports filtering, sorting, and pagination. **Export loaded dataset** downloads all loaded rows and numeric statistics as CSV.

Player statistics are filtered by the selected player's position and available observations. Football kickers see kicking stats, defenders see defensive stats, and offensive players see their relevant stat families. Relevant zero values remain selectable. NBA positions share the same basketball box-score metrics. If a historical position label is unavailable, football options use only that player's recorded stat families. An incompatible statistic is replaced automatically when you switch players. Existing player caches without position metadata refresh automatically on their next load.

In player mode, the first analysis filter is **Team**, followed by **Player**. Choose a team to narrow the player list and the selected player's current-season games, or select **All teams** to include every loaded team. The career graph always covers the selected player's full available history across teams. **Statistic type** selects a category, and **Statistic** then lists its detailed measures—for example **Passing → Passing yards / Passing touchdowns / Completions**. Categories also respect position relevance. Basketball uses categories such as Scoring, Shooting, Rebounding, Playmaking, and Defense. Changing a team, player, or category preserves compatible choices and replaces incompatible selections automatically. These analysis filters use the loaded dataset without additional source requests.

Starting the Python server does not download data until you open the page. Opening the dashboard loads its default scope, using the local cache when available. No sample statistics are presented as real observations.

**Performance over time** labels games with their date and **away @ home** matchup, for example `2025-11-01 · GSW @ TOR`. Longer game logs show a readable subset of axis labels; every point's hover shows its date and matchup. The next-appearance baseline has no fabricated future date or opponent.

Selecting **Players** for NBA or NFL adds a **Player career** graph directly below the performance graph. It loads every published season available from the selected sport's summary source, matches the player by ID across all teams, and follows the selected statistic and regular/postseason phase. It ignores the season and week filters so earlier seasons and transfers remain visible. Counting statistics use **season totals**; rates keep their source units. Gaps remain missing rather than becoming zero. The caption reports source coverage and failed seasons. The first load can take longer; summaries are shared across players and saved in `data/cache/career.sqlite3` for 24 hours.

## Data sources

| Sport | Source and access | Imported data |
| --- | --- | --- |
| NBA | [NBA Stats](https://www.nba.com/stats), with historical box scores from [SportsDataverse's NBA Stats archive](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nba_stats_team_boxscores). No API key. | Team or player game logs: points, rebounds, assists, shooting, turnovers, and other box-score statistics. |
| NFL | [nflverse-data](https://github.com/nflverse/nflverse-data), using the same weekly CSV releases as [`nflreadr::load_stats`](https://github.com/nflverse/nflreadr/blob/main/R/load_stats.R). No API key. | Weekly team or player statistics: passing, rushing, receiving, defense, kicking, and available derived metrics. Uses actual team datasets rather than sums of player stats. |
| NCAAF | [CollegeFootballData](https://collegefootballdata.com), using its documented [`/games` and `/games/teams`](https://apinext.collegefootballdata.com/api/games) endpoints. API key required. | Completed FBS team game box scores, including scores and numeric team statistics. |

### College football setup

Request an API key at <https://collegefootballdata.com/key>. Add it to the ignored `.env` file:

```dotenv
CFBD_API_KEY=your-key-here
```

Alternatively set `$env:CFBD_API_KEY` in PowerShell. The server reads the key; it is not stored in the browser or cache. The college scope starts with `Michigan`; change it to another school or clear it and choose a week. These box-score endpoints require a team, week, or conference filter; the app supports team and week. A school filter returns only that school's observations. A week filter without a school returns teams across the requested FBS games. Each NCAAF import makes two requests: game metadata and team box scores. Historical availability, quotas, and endpoint access depend on the provider and your key.

### Seasons and source limitations

- NBA seasons use their **starting year**: `2024` means `2024–25`. Football uses the season year even for postseason games played in the next calendar year.
- Regular season and postseason are imported separately. Current seasons can be incomplete. The default is the prior season.
- NFL weekly releases are requested for 1999 onward; some statistics have shorter coverage. Game dates come from the [nflverse schedule](https://github.com/nflverse/nfldata/blob/master/data/games.csv). If schedule metadata is unavailable, labels show season/week rather than an invented date. Historical franchises retain the provider's team abbreviations. Unassigned events without a player ID are excluded from player histories; team totals come from the separate team release.
- NCAAF imports support 2001 onward where the provider has box scores; earlier or incomplete records may be unavailable. Non-numeric fields such as possession time are omitted. Recognized compound counts such as completions/attempts and third-down conversions/attempts are split into numeric fields.
- NBA historical seasons from 1996 onward first use SportsDataverse's published NBA Stats box scores and schedules (files use the season's ending year). If the archive is unavailable, the app tries NBA Stats directly. The selected source is recorded in the cache and displayed on the page. Player DNP records are excluded from appearance histories.
- Metrics retain their source units. NBA shooting fractions, football percentages, and similarly named categories are not automatically harmonized across providers. Missing values remain missing.

## Cache and historical imports

Normalized datasets are saved atomically in **`data/cache/sports.sqlite3`**, ignored by Git. A cache entry records the query, source, UTC retrieval time, and compressed dataset. Entries are reused for 24 hours. **Refresh from source** bypasses the age check. If a download fails and an older copy exists, the dashboard explicitly reports that it is using stale data. Failed or empty downloads do not replace valid cached data. Override the location with `SPORTS_CACHE_PATH` in `.env` or the environment.

Import several seasons without opening the UI:

```powershell
.\.venv\Scripts\python.exe -m sports_analytics.ingest NBA --seasons 2022 2023 2024 --entity player
.\.venv\Scripts\python.exe -m sports_analytics.ingest NFL --seasons 2023 2024 --entity team
.\.venv\Scripts\python.exe -m sports_analytics.ingest NCAAF --seasons 2023 2024 --entity team --team Michigan
.\.venv\Scripts\python.exe -m sports_analytics.ingest NFL --seasons 2024 --entity player --season-type postseason --refresh
```

The command exits nonzero for failed imports or stale fallback data. CLI and dashboard share the cache. The dashboard explores one imported season at a time; use the data/service modules to assemble multiple seasons for model training.

Career summaries use [SportsDataverse NBA Stats season summaries](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nba_stats_player_season_stats) from 1996 onward and nflverse player season summaries from 1999 onward. Published seasons are discovered from the providers' releases. Seasons before source coverage, unavailable metrics, and future seasons are not manufactured.

You can verify one player's career import directly:

```powershell
.\.venv\Scripts\python.exe -m sports_analytics.career NBA 201939 --name "Stephen Curry" --metric pts
```

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
  career.py                    Career season summaries and shared cache
  forecast.py                  Rolling baseline and chronological evaluation
  ingest.py                    Multi-season command line importer
tests/test_history.py          Career, date/matchup, and dashboard integration checks
```

Run the offline checks:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use representative API-shaped fixtures and never consume API quotas. Live access is a separate check: run an import for a known historical season. Before redistributing downloaded data, review each source's current terms and licensing.
