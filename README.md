# Sports Analytics

A Python Dash workspace for analyzing historical **NBA, NFL, and NCAA Football (NCAAF)** team statistics, NBA and NFL player statistics, and developing forecasts of future performance. NCAAF FanDuel player links also open saved college player history using separately prepared data.

Code implementation manuals: [PPP guide](models/Game_Outcome/PPP/PPP_Implementation_Guide.pdf)
and [EPA guide](models/Game_Outcome/EPA/EPA_Implementation_Guide.pdf), plus the
[ELO / Power Rating guide](models/Game_Outcome/ELO/ELO_Implementation_Guide.pdf). They cover lookback
windows, minimum game counts, new-season carryover, formulas, calibration and
saved-model behavior. The source research PDFs are retained separately.

Each sport has three sections in the left toolbar. **Historical Data** opens nested **Team Statistics** and **Player Statistics** pages for NBA/NFL (NCAAF's standard menu has Team Statistics), replacing the team/player radio selector. These pages contain the historical filters, charts, career histories, and game log. **FanDuel Market** shows upcoming game cards; **Backtesting** opens a model overview and nested model pages. Switching pages preserves the historical controls. Historical subpages use `#historical-team` and `#historical-player`, apply to the selected sport tab, and support browser back/forward navigation.

Player prop headings show **Player name - Market**, for example **Jerry Jeudy - Alt Receiving Yds**.
Click the player name to open Historical Data with that player, sport, game season and relevant
statistic selected. NBA combination props show the exact sum of their recorded components.
History links appear only in player market headings and the game's matchup header.
Selection rows, Game Lines and Mispricing Filter tables use plain team/player names.
Matchup-header links select points scored. Missing
components remain unavailable, and missing or ambiguous names never select an unrelated player.
NCAAF player links open a saved-history player view using prepared college boxes without
downloading data; career summaries remain NBA/NFL only. First/last touchdown props show available
touchdown counts, with an explanation that scorer order is unavailable in box scores.
After following a history link, the first click on **FanDuel Market** returns to the
last saved game, snapshot and market tab. Player-header links also reopen and scroll
to that player's exact market. The return location is retained per sport for the
browser tab's session. **Back to upcoming games** still opens the schedule.

Each saved game also has a **Mispricing Filter** tab beside Game Lines and the player-prop tabs.
It contains the Game Outcome and Player Forecast method selectors, Estimated Probability and
Edge thresholds, **Apply Criteria**, and **Quick Filtering**. Quick Filtering applies the current
thresholds and evaluates game lines and player props across the selected snapshot with the chosen
methods. The first table shows up to three selections with the highest estimated edge; the next
table shows all remaining matches in descending edge order. Both thresholds must be exceeded.
Edge is measured in percentage points and uses the same push-adjusted model probabilities as
the market tabs. Duplicate selections appear once, and unavailable model estimates/prices are
excluded with a count. Changing the game, snapshot, selected methods or applied criteria requires
another Quick Filtering run so old rankings are not displayed for new settings.

Under **Backtesting**, expand **Game Outcome** for PPP, EPA and ELO / Power Rating,
or **Player Stats** for Weighted Historical Performance, Usage & Opportunity,
Opponent-Adjusted Matchup and Pace & Game Environment. Each model has a separate
page for the selected sport with its evaluation approach, required inputs,
saved market snapshot count, current settings read from the model, and a model
documentation download. Selecting a different sport updates the current model
page and its settings. Links and browser back/forward navigation are supported.
Each methodology page also includes **Run backtest**, team/player and statistic
filters, an optional evaluation date range, game count and error tolerance.
Player pages accept an optional fixed reference line for Over probability scores.
Runs use saved inputs, fixed model defaults and earlier available observations
at each game’s cutoff; opening a page reloads its latest saved report.

Results include event accuracy with a Wilson interval, MAE, RMSE, bias, sample
error variance/SD, within-tolerance accuracy, 80% predictive interval coverage,
Brier score, log loss and a prior-data baseline. Six charts show predictions
versus actual outcomes, agreement, residuals, rolling MAE, calibration and historical interval accuracy.
The forecast error histogram displays exact mean bias, median, sample SD, sample
count, MAE and RMSE above the plot. Hover a statistic for its definition, min/max,
quartiles and 5th/95th percentiles. Errors are predicted minus actual; sample SD
uses the n−1 denominator and is unavailable for a single observation. Notes below
the error, calibration and rolling-error charts explain their interpretation.
Individual reports use a trailing five-observation MAE; comparison charts use ten.
Game calibration checks home wins, while player calibration checks Over a fixed
reference line, excluding pushes. These are not separate calibration checks of
every sportsbook spread, total or alternate player line.
Game pages switch between home margin and total points. A detailed table,
skipped-game reasons and CSV export support inspection. Reports and model replay
caches stay in ignored `data/backtesting/`; source or odds APIs are never called
by a run. See [backtesting details](docs/BACKTESTING.md).

NBA and NFL each include **Visualized In-Game Data** in the left navigation.
Choose a season, phase, team and recorded game. NBA shots appear on a maple
court with regulation markings, made/missed symbols, player/period/result filters,
and half/full court views. Select an individual shot/play to isolate it. Legacy shot coordinates are converted from tenths of
a foot; free throws are excluded. The summary/table include filtered attempts
outside the current view, and the chart reports hidden heaves or missing locations.
NFL offensive plays appear on a striped field with end zones, yard lines and NFL
hash marks. Filter by offense, passer/rusher/receiver, period, play type and drive.
Horizontal position uses recorded line of scrimmage and yards gained; vertical
left/middle/right lanes are explicitly schematic, not measured player routes.
All plays face the same direction; incomplete passes use actual gain rather than
intended air yards. Hover for play details and expand **Event details & CSV export**
for the full filtered records.

Existing NBA play-by-play archives and nflverse raw CSVs are reused. Missing NBA
games use the official NBA CDN play-by-play endpoint; an uncached NFL season
downloads its nflverse play-by-play once. Saved season files are indexed once per
file revision under ignored `data/cache/in_game/`, and only the selected game's
events reach the browser. **Refresh game list** updates the catalogue; **Refresh
game data** checks the source for that game's events (NFL refreshes the season
source). Source timestamps and refresh-fallback notices are shown. These are
recorded events rather than a live tracking feed; coverage, including older NBA
CDN games, depends on the provider. Unavailable games show a message, not synthetic
locations. No paid odds requests are used. Run `python scripts/check_in_game.py`
for offline coordinate, cache, filtering and navigation checks.

NFL drives can be selected from either team's drive list or the labeled drive
dropdown. Each list shows the drive result and recorded offensive-play count/net
yards; the focused field labels plays in drive order. Personnel and play-calling
breakdowns follow the same team, drive, period, player, play-type and individual
play filters. When all drives are selected, each offense and opposing defense
retain separate percentage denominators.

**Load / refresh personnel & coverage** retrieves optional nflverse
[participation](https://nflreadr.nflverse.com/reference/load_participation.html)
and [FTN charting](https://nflreadr.nflverse.com/reference/load_ftn_charting.html)
season files, joined by exact NFL game/play IDs and indexed locally. Saved
charting is reused automatically without another download. Participation is
available from 2016; from 2023 onward, FTN participation is released after the
postseason. The public FTN charting subset starts in 2022 and can arrive during
the season. Missing or failed supplementary downloads leave the base game usable.

The breakdowns include offensive personnel (11/12/21 packages when the recorded
counts support them), defensive DL/LB/DB personnel, run/dropback share, offensive
formation, man/zone, coverage schemes, box counts, pass rushers, blitzers, play
action and RPO. A dropback includes passes, sacks and marked QB scrambles; kneels
and spikes are excluded from run/dropback shares. Coverage percentages use the
selected dropbacks faced; personnel percentages use selected offensive plays.
Every percentage includes Unknown in its denominator and shows recorded/eligible
counts. No missing value is counted as a recorded No. Defensive formation is
shown only when explicitly supplied; positional personnel never implies a front,
alignment or coverage call. Exact raw packages and charting values are included
in event details/CSV exports. Sources and timestamps are displayed with attribution
to FTN Data or NFL NextGenStats via nflverse under CC BY-SA 4.0.

The separate **Odds API** tab connects to your [The Odds API](https://the-odds-api.com/) account and shows monthly credit usage as a donut chart, with used, unused, and total credits. Add `ODDS_API_KEY=your_api_key_here` to the ignored `.env` file and restart `python index.py`. The key stays on the server and is never placed in browser components or stored in the usage cache.

Usage comes from the provider's `x-requests-used` and `x-requests-remaining` headers on `GET /v4/sports/`, which [does not consume usage credits](https://the-odds-api.com/liveapi/guides/v4/#get-sports). Counters cover the subscription's current quota cycle across all sports; the app does not infer a calendar-month reset date. Usage loads when Odds API is opened, refreshes every five minutes while visible, and can be checked immediately using **Connect / Refresh usage**. The server shares a five-minute memory cache for the account. A failed refresh labels any saved snapshot explicitly; missing keys and invalid responses show setup/errors rather than invented credit values.

FanDuel Market loads public ESPN schedules without an API key. Choose a 7- or 14-day window to see dated cards with away/home teams, EDT start times (`MM/DD/YYYY HH:MM:SS EDT`, or Time TBD), venues, neutral-site labels, and game-detail links. Games are grouped by their displayed EDT calendar date, including evening games after midnight UTC. Live, completed, canceled, and postponed games are excluded. The FanDuel link opens the selected sport's sportsbook page. **NBA, NFL and NCAAF** cards offer **Pull Live Markets** beside **Game details**, saved snapshots and dedicated game views.

### Weighted Historical Performance player forecasts

[WHP implementation details](models/Player_Stats/Weighted_Historical_Performance/README.md)
cover source inputs, NBA/NFL/NCAAF windows, prior-season carryover, workload
decomposition, forecast uncertainty, pushes and market comparison. In a saved
game, open a player-prop tab to see **Market implied**, **WHP probability**,
**WHP fair odds** and **Edge**. The **Game Outcome Forecast Method** and **Player
Forecast Method** dropdowns sit side by side. Each shows the latest historical
game/performance date actually used by its calculated forecast underneath.
Player dates summarize the displayed prop category; individual player dates
remain in the model-value hover text. Uncalculated or unavailable forecasts are
labeled explicitly, and dates reset when the saved game/snapshot changes.
Existing NBA/NFL player logs are reused. Missing histories can be prepared with
`python -m models.Player_Stats.Weighted_Historical_Performance --prepare` plus
the sport, start and team arguments documented in the WHP guide; NCAAF requires
`CFBD_API_KEY`. No history preparation controls appear in the game view.

WHP uses up to 20 NBA, 16 NFL or 12 NCAAF appearances with independently replayed
forecast errors for dispersion. NBA/NFL look up the current and previous season;
NCAAF now looks up the current and two previous seasons, including targeted transfers.
Missing targets, insufficient samples, ambiguous players and first/last scorer
definitions remain unavailable with a reason. Anytime/total scorer TDs require
an authoritative all-scoring `touchdowns` target; passing TDs are not scorer TDs.
Criteria applies to calculated player selections and preserves the current tab.
The separate PPP/EPA/ELO selector controls NBA/NFL/NCAAF Game Lines.

NCAAF uses the same 17 football market keys as NFL and the existing CSV/credit
audit pipeline. College coverage depends on the selected event and FanDuel.
Only **Pull Live Markets** makes a paid odds request. College PPP/EPA/ELO reuse
school-scoped CSVs under `data/modeling/ncaaf`, prepared when a school is chosen
in Historical Data. See [NCAAF data and forecasts](docs/NCAAF_FORECASTS.md).
WHP forecasts and comparison audits are saved in
`data/modeling/player_stats/whp`; historical source observations stay in the
existing cache except for dedicated college sources and optional imports.

### API credit history

Market snapshots live in **`data/market_data`**. The former top-level `market_data` folder has been moved there; existing snapshots remain readable with the same filenames. New live pulls use this location automatically.

The Excel-readable credit ledger is **`data/API Log/api_credit_usage.csv`**. Each paid pull records its sport, matchup, game/event IDs, requested markets, exact `x-requests-last` credit charge, account counters when reported, timestamp in UTC and formatted EDT, and outcome. Missing charges remain blank and are labeled **Unreported** in the dashboard; they are never replaced with an estimated maximum. A request that fails after being sent is recorded as failed, with its reported charge or an unknown charge. Account identifiers are irreversible fingerprints; API keys are never written to this file. Generated snapshots and logs are ignored by Git.

The **Odds API** page places a current-month NBA/NFL/NCAAF composition pie beside the account quota donut, followed by per-sport monthly credit metrics, an all-history daily usage line chart, and a searchable, sortable table with CSV export. These reports read the local ledger without paid API requests. Calendar months and daily totals use EDT (UTC-4); timestamps display as `MM/DD/YYYY HH:MM:SS EDT`. Unknown charges are excluded from chart totals and explicitly counted. Activity outside this app remains reflected in the provider's quota donut, but cannot be attributed to a sport in the local ledger.

Existing saved NBA and NFL snapshots are imported into the ledger without making API requests. Reopening a saved game, repeating the same click, or restarting the app does not create duplicate log entries. The SQLite snapshot index can recover missing log entries without another paid pull. NBA pulls are logged as NBA and included in the same usage charts and history table. The ledger and reports support all three sports.

### NBA live markets and PPP

NBA event-based EPV forecasting is available through `python -m models.Game_Outcome.EPA.NBA_EPA --seasons 2025 --home DET --away BOS --as-of 2026-10-20T03:48:47+00:00` and the NBA market forecast selector. It reconstructs possession scoring from public events, quarantines inconsistent games, fits pre-action expected possession values and signed team residuals, and calibrates a normal score-error baseline. The separate `python -m models.Game_Outcome.EPA.prepare_nba_epa --seasons 2025 --download` command remains a raw-input audit; subsequent audit runs without `--download` are offline. See [NBA EPA/EPV setup](models/Game_Outcome/EPA/NBA_EPA_SETUP.md) for preparation, reconstruction results and research limitations.

NBA shares the NFL market-page structure: team-logo cards, bold latest-pull timestamps, a different background for data pulled within 24 hours, saved snapshot selection, an in-game **Pull Live Markets** button, collapsible player ladders and Game Lines comparisons. NBA cards and matchup headers use local PNGs from `images/NBA/Team Logos`; NFL uses its local PNGs. LA Clippers matches the `Los Angeles Clippers.png` filename. Abbreviations appear in the badge only when a local file is missing.

The dashboard reads only the plural `images` folder. The removed singular `image` folder is not needed. All 30 NBA and 32 NFL assets are served through the app's local image routes.

Each explicit NBA pull makes one FanDuel event-odds request for **13 keys**, costing **up to 13 credits** according to returned markets. Actual cost comes from response headers. No automatic odds refreshes, other bookmakers or fantasy markets are included. Keys follow [The Odds API's market list](https://the-odds-api.com/sports-odds-data/betting-markets.html):

| Category | Keys |
| --- | --- |
| Game Lines | `alternate_spreads`, `alternate_totals` |
| Points / Rebounds / Assists / Threes | `player_points_alternate`, `player_rebounds_alternate`, `player_assists_alternate`, `player_threes_alternate` |
| Defense / Turnovers | `player_blocks_alternate`, `player_steals_alternate`, `player_turnovers_alternate` |
| Combos | `player_points_assists_alternate`, `player_points_rebounds_alternate`, `player_rebounds_assists_alternate`, `player_points_rebounds_assists_alternate` |

Snapshots use `data/market_data/nba_<home>_<away>_<game-date>_<UTC-snapshot-timestamp>.csv`, with a secondary `data/cache/nba_markets.sqlite3` index. Opening a game reads saved files; only **Pull Live Markets** requests new odds. NBA and NFL records stay separate even if ESPN IDs coincide.

**PPP** defaults to the NBA-specific model in [models/Game_Outcome/PPP/NBA_PPP.py](models/Game_Outcome/PPP/NBA_PPP.py). Select **EPA** for the independently fitted public event-based EPV model in [models/Game_Outcome/EPA/NBA_EPA.py](models/Game_Outcome/EPA/NBA_EPA.py). The selection updates probability/fair-odds headings, values, Edge and all four charts. On Game Lines, Apply Criteria evaluates **both teams' spreads and both Over/Under totals**, using defaults of 75% probability and 5 percentage points Edge. Both must be strictly exceeded. Matching rows receive a highlighted border, including rows beyond Show more; a summary reports spread/total match counts or explicitly says that no markets match. Player props use the separate WHP model when history and calibration are sufficient; missing estimates are explicitly unavailable.

Each market initially shows at most six selections. Apply Criteria keeps extra
selections collapsed; use **Show more** to reveal them and the bottom **Show less**
control to collapse them again. Applied criteria also add gold segments, fill
and markers to qualifying lines in both teams' spread charts and the Over/Under
charts. These use the same probability and Edge checks as the table rows.

NFL and NBA also offer **ELO / Power Rating** in the forecast selector. It uses
sequential neutral Elo, ridge point-margin strengths, a separate positive
offense/defense and drive/possession scoring model for totals, and fitted
chronological Elo/margin reconciliation. ELO has independent frozen estimates;
selection updates probability, fair odds, Edge, highlights and all four charts
without pulling odds. PPP remains the default. See [the ELO documentation](models/Game_Outcome/ELO/README.md)
for exact lookbacks, offseason carry, initial parameters and normal-baseline
limitations. NCAAF has its own college-only ELO model and calibration.

The NBA model uses the existing NBA Stats/SportsDataverse box-score adapters, estimated shared possessions, the EPA note's section 6.2 additive PPP baseline and section 6.3 shared pace, with chronological score-error calibration. Observations and frozen estimates live under `data/modeling/nba`. First estimates may download public statistics; this consumes no Odds API credits. See [model details](models/Game_Outcome/PPP/README.md#8-nba-specific-ppp-implementation) for definitions and limitations.

### NFL live market snapshots

The game-detail view has its own **Pull Live Markets** button. Opening a game checks `data/market_data` and displays saved snapshots without requesting odds. A button click explicitly requests a fresh snapshot, writes a new CSV, selects that snapshot immediately, and updates the game card's latest-data time. The detail button uses the same FanDuel-only 17-market package and duplicate-click protection as the card button.

With `ODDS_API_KEY` configured in `.env`, click **Pull Live Markets** on one NFL game to save its current FanDuel odds. The app resolves the ESPN matchup to a unique Odds API event using both teams and commencement time through the free events endpoint. It then makes **one** current event-odds request with `bookmakers=fanduel`, American odds, and these **17 market keys**:

| Category | Requested keys |
| --- | --- |
| Game | `alternate_spreads`, `alternate_totals` |
| Passing | `player_pass_yds_alternate`, `player_pass_tds_alternate`, `player_pass_attempts_alternate`, `player_pass_completions_alternate`, `player_pass_interceptions_alternate` |
| Receiving | `player_reception_yds_alternate`, `player_receptions_alternate`, `player_reception_tds_alternate` |
| Rushing | `player_rush_yds_alternate`, `player_rush_attempts_alternate`, `player_rush_tds_alternate` |
| Touchdowns | `player_anytime_td`, `player_1st_td`, `player_last_td`, `player_tds` |

The [event-odds pricing rules](https://the-odds-api.com/liveapi/guides/v4/#usage-quota-costs-4) charge for distinct markets returned and bookmaker groups, so this configuration costs **up to 17 credits**, with lower charges when markets are unavailable. Player counts and numbers of alternate thresholds do not increase the number of market types. No main player-prop markets, `player_tds_over`, broader alternate categories, other bookmakers, or historical endpoints are added. The actual `x-requests-last` charge is displayed; a missing cost header is shown as unreported.

After a pull, the game card only updates **Latest data** directly beneath **Game details**, in the same footer as Pull Live Markets. Saved times use `MM/DD/YYYY HH:MM:SS EDT`; for example, `2026-10-19T23:35:23+00:00` displays as `10/19/2026 19:35:23 EDT`. All displayed timestamps, including game kickoffs, saved snapshots, API connection checks, source updates, and retrieval statuses, use EDT (UTC?4) and the same numeric format. Stored UTC timestamps and CSV filenames are preserved. UTC timestamps remain unchanged in the stored CSV data and filenames. Click the card or Game details to enter the saved-game view. It follows the supplied FanDuel examples: an Eastern-time matchup header, Game Lines / Passing Props / Receiving Props / Rushing Props / TD Scorer Props tabs, collapsible player markets, alternate selection ladders, blue American-odds boxes, and Show more controls. Only returned categories and selections appear; the Unavailable Markets panel is removed. Threshold labels preserve the actual integer-stat meaning: Over 174.5 passing yards is 175+ yards; Under lines retain the provider's exact threshold. Source responses update the account usage cache used by the Odds API tab.

Each pull writes a separate CSV in **`data/market_data`**, for example `nfl_sf_wsh_2026-10-19_20261019T233523000000Z.csv`. The filename is `nfl_<home abbreviation>_<away abbreviation>_<Eastern game date>_<UTC snapshot timestamp>.csv`. Every outcome gets a row with event IDs, teams, abbreviations, game date, snapshot time, market category/key, player/team, selection, line, American odds, and source update time. The first row also includes snapshot metadata and the complete original API response as JSON cells, preserving source fields beyond the displayed markets. Existing SQLite-only pulls are exported from the fields already saved; the original API response cannot be reconstructed if it was not previously retained.

Saved CSVs are loaded on the next visit without an API request, including when the SQLite index is missing. The existing `data/cache/nfl_markets.sqlite3` remains a secondary snapshot index and duplicate-request guard. Both stores are scoped to the connected account, and generated CSVs are ignored by Git. Keep the same `ODDS_API_KEY` configured to reopen that account's saved data. Game-detail links such as `#nfl-game-401873011` can be bookmarked and restore the NFL market view after reopening the app.

Only the button initiates a paid request. Opening pages, refreshing schedules, switching snapshots/categories, and expanding saved markets do not pull odds. The clicked button is disabled while loading; identical click identifiers are deduplicated across app restarts. A failed or timed-out paid request is not automatically retried, since its charge may already have been applied. Another deliberate click requests a fresh snapshot. Unmatched or ambiguous games are rejected before any paid request.

Schedules load only while FanDuel Market is selected, refresh every five minutes, and are cached by sport/day in `data/cache/schedules.sqlite3`. **Refresh upcoming games** bypasses the cache. Source failures are shown explicitly, including dates with missing coverage or saved schedules. Historical filters do not restrict the upcoming schedule. NCAAF schedules cover FBS teams and do not require `CFBD_API_KEY`; the key is still required for NCAAF historical imports.

## Parlay analysis slip

In any saved FanDuel game, click **Market odds** to add/remove a leg in the
bottom-docked parlay slip. Each leg shows market implied probability, default
PPP/WHP probability and fair odds. The footer shows leg count and combined
market/calculated odds and probabilities. Collapse, individual removal, Clear
all and browser-session persistence are included. Same-game combinations are
explicitly labeled independent-leg estimates, not a FanDuel SGP quote.
See [pricing, push handling and limitations](docs/PARLAY.md).

## Run locally

Requires Python 3.11 or newer. From this repository in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements/requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe index.py
```

Open **http://127.0.0.1:8050**. Use `--port 8051` to change the port, or `--debug` for development. The default server listens locally. Stop it with Ctrl+C.

With the virtual environment activated, you can also run `python index.py`. The original `python app.py` entry point remains available.

Running `index.py` directly also selects this project's `.venv` automatically when it exists, so an IDE Run button using system Python can still launch the app. VS Code's default interpreter is configured to use `.venv`. Install the requirements with the setup command above when creating a new environment.

Select NBA, NFL, or NCAAF, then choose a season. NBA and NFL offer team/player statistics; NCAAF offers teams only and automatically resets the mode to Teams when selected. The selected scope loads automatically when you open the page or change a filter, populating **Team or player** and **Statistic** from the actual observations. You can also click **Load historical data** to retry. Selectors are disabled with a source-status message when no data can be loaded. The game log hides Game ID for every sport and omits Week for NBA. It supports filtering, sorting, and pagination. **Export loaded dataset** downloads all loaded rows and numeric statistics as CSV.

Player statistics are filtered by the selected player's position and available observations. Football kickers see kicking stats, defenders see defensive stats, and offensive players see their relevant stat families. Relevant zero values remain selectable. NBA positions share the same basketball box-score metrics. If a historical position label is unavailable, football options use only that player's recorded stat families. An incompatible statistic is replaced automatically when you switch players. Existing player caches without position metadata refresh automatically on their next load.

In player mode, the first analysis filter is **Team**, followed by **Player**. Choose a team to narrow the player list and the selected player's current-season games, or select **All teams** to include every loaded team. The career graph always covers the selected player's full available history across teams. **Statistic type** selects a category, and **Statistic** then lists its detailed measures—for example **Passing → Passing yards / Passing touchdowns / Completions**. Categories also respect position relevance. Basketball uses categories such as Scoring, Shooting, Rebounding, Playmaking, and Defense. Changing a team, player, or category preserves compatible choices and replaces incompatible selections automatically. These analysis filters use the loaded dataset without additional source requests.

NFL team statistics include **Scoring → PF (Points for) / PA (Points against)**, joined to actual final scores from the nflverse schedule. Missing scores stay unavailable, while zero-point games remain valid. Older NFL team caches refresh once to add the scoring fields.

Starting the Python server does not download data until you open the page. Opening the dashboard loads its default scope, using the local cache when available. No sample statistics are presented as real observations.

**Performance over time** uses bars for the selected statistic and the next-appearance baseline, with the prior rolling-window mean kept as a line. It labels games with their date and **away @ home** matchup, for example `2025-11-01 · GSW @ TOR`. Longer game logs show a readable subset of axis labels; every point's hover shows its date and matchup. The next-appearance baseline has no fabricated future date or opponent.

Selecting **Players** for NBA or NFL adds a **Player career** graph directly below the performance graph. It loads every published season available from the selected sport's summary source, matches the player by ID across all teams, and follows the selected statistic and regular/postseason phase. It ignores the season and week filters so earlier seasons and transfers remain visible. Counting statistics use **season totals**; rates keep their source units. Gaps remain missing rather than becoming zero. The caption reports source coverage and failed seasons. The first load can take longer; summaries are shared across players and saved in `data/cache/career.sqlite3` for 24 hours.

## NFL PPP market probabilities

The **Forecast method** dropdown above the saved NFL market tables offers **PPP** (default), **EPA** and **ELO / Power Rating**. Selecting a method changes the probability and fair-odds columns, Edge, criteria highlighting and all four comparison charts. `models/Game_Outcome/EPA/EPA.py` implements signed opponent-adjusted passing/rushing EPA, a trained points-per-drive/EPA scoring bridge, and historical score-error uncertainty from the supplied EPA research PDF. All methods use cached observations and saved quotes; switching methods never pulls live odds. Their archives have independent method prefixes. Run EPA independently with `python models/Game_Outcome/EPA/EPA.py --seasons 2025 2026 --home BAL --away CIN --as-of 2026-10-19T23:35:23+00:00`. See [the EPA model documentation](models/Game_Outcome/EPA/README.md) for formulas, configuration and limitations.

`models/Game_Outcome/PPP/PPP.py` implements the supplied PPP strategy document's sections 1–5 using joint opponent-adjusted efficiency, shared drive pace, separate non-offensive scoring and the section 4.1 normal baseline. Spread/total variances come from chronological held-out forecast errors, rather than the PDF's illustrative standard deviations. Public NFL schedules, team box scores, play-by-play and player snap counts are prepared in `data/modeling/nfl`; no Odds API credits are used for modeling.

On a saved NFL game's **Game Lines** tab, every alternate spread and total shows **Market odds**, **Market implied**, **PPP probability**, and **PPP fair odds**. Whole-point lines account for pushes; hover the PPP cells for win/loss/push probabilities. Four charts below the markets separately compare each team's handicap, Over, and Under, using teal PPP and blue market-implied probability lines with transparent fills at the saved lines. Spread labels include the team and handicap, such as `Baltimore - 3.5`. Bookmaker margin remains included in the market probabilities. Player props retain their existing layout. Estimates are saved and reused after restarting; missing model data shows an explicit message alongside the saved market quotes.

Prepare data or run the model separately with `python models/Game_Outcome/PPP/PPP.py --seasons 2025 2026 --home SF --away WSH --as-of 2026-10-19T23:35:23+00:00`. See [models/README.md](models/README.md) for source definitions, formulas, validation requirements, cache behavior and limitations. This is the report's normal baseline; key-number probabilities are approximate and no weather or injury adjustment is assumed.

## Usage & Opportunity player forecasts

NBA/NFL saved FanDuel games offer **Usage & Opportunity (UO)** in the **Player
Forecast Method** dropdown, alongside the default WHP method. UO simulates saved
team workload, player participation/share scenarios and efficiency to show **UO
probability**, **UO fair odds** and **Edge**. Apply Criteria uses the selected
method. Hover a forecast for workload, predictive intervals, simulation error,
last source date and EV. Reading a snapshot never pulls odds or downloads source
history for NBA/NFL. NCAAF supports UO through its college adapter and prepares
missing CFBD inputs on first use; later views reuse saved CSVs.

The implementation, exact NBA/NFL lookbacks, season carryover, supported markets,
input requirements and limitations are documented in
[`models/Player_Stats/Usage_Opportunity/README.md`](models/Player_Stats/Usage_Opportunity/README.md)
and its separate `UO_Implementation_Guide.pdf`. It uses the existing NBA and
nflverse box-score sources; routes, tracking chances, injuries and red-zone feeds
are not connected. NFL touchdown/order markets therefore remain unavailable in UO.

If player or team inputs are missing, prepare both seasons explicitly:

```powershell
python -m models.Player_Stats.Usage_Opportunity --sport NBA --prepare --start "2026-10-25T23:00:00Z"
python -m models.Player_Stats.Usage_Opportunity --sport NFL --prepare --start "2026-10-25T17:00:00Z"
```

`--refresh` refreshes source history. These preparation commands make public data
requests, never Odds API requests. UO forecasts and evaluation records live in the
ignored `data/modeling/player_stats/usage_opportunity/` folder. These are research
defaults; use the included historical snapshot replay to evaluate accuracy and
calibration before relying on the estimates.

## Opponent-Adjusted Matchup player forecasts

NBA/NFL saved FanDuel games also offer **Opponent-Adjusted Matchup (OAM)** in
**Player Forecast Method**. OAM first neutralizes historical matchup effects, then
adjusts opportunity and efficiency components using chronological actual/expected
defense ratios, recency weighting and small-sample shrinkage. NFL uses recorded
position cohorts where sufficient data exists. The player table, Edge and Apply
Criteria follow the selected model; hover a value for the neutral mean, adjusted
mean, net multiplier and uncertainty. No new odds pull is needed.

See [`models/Player_Stats/Opponent_Adjusted_Matchup/README.md`](models/Player_Stats/Opponent_Adjusted_Matchup/README.md)
and `OAM_Implementation_Guide.pdf` for exact windows, sample requirements, prior
season carryover, components, optional pregame lineup scenarios and historical
WHP/UO/OAM comparison. The engine uses the existing NBA/nflverse box scores;
unconnected tactical, tracking, route and live personnel feeds are not inferred.
NCAAF now supports OAM and PGE as well as UO and WHP. See
[college player methods](docs/NCAAF_PLAYER_METHODS.md) for college-specific
inputs, lookbacks and coverage. Opportunity-model touchdown/order forecasts
remain unavailable without the required source inputs.

```powershell
python -m models.Player_Stats.Opponent_Adjusted_Matchup --sport NBA --prepare --start "2026-10-25T23:00:00Z"
python -m models.Player_Stats.Opponent_Adjusted_Matchup --sport NFL --prepare --start "2026-10-25T17:00:00Z"
```

Preparation saves league-wide team/player inputs across two seasons. Forecasts
and defensive-fit records are cached under the ignored
`data/modeling/player_stats/opponent_adjusted_matchup/` folder. Accuracy requires
chronological out-of-sample evaluation; implementation checks are not a claim
that matchup adjustment improves every prop market.

## NBA team logo downloader

Run `python download_nba_logos.py` to download all 30 current NBA team logos from ESPN's public team directory. It writes **500×500 transparent PNGs**, proportionally resized and centered, named for the team (for example `Golden State Warriors.png`). The default folder is **`images/NBA/Team Logos`**, alongside the NFL logos in the project's existing plural `images` folder. Restart the dashboard after adding logo files so they appear on NBA cards and matchup headers.

Install its image dependency with `python -m pip install -r requirements/requirements-logos.txt`. Existing valid files are reported as **Already saved**, with their full paths; use `--overwrite` to download them again or `--output "another folder"` to select a different destination. The final message shows the total number of valid files ready in that folder. Failed files can be retried by rerunning; incomplete downloads never replace a completed image. No API key or Odds API credits are needed.

## NCAAF top 100 team logos

Run `python download_ncaaf_logos.py` to save **500×500 transparent PNGs** under
`images/NCAAF/Team Logos`, named for each school and mascot (for example
`Michigan Wolverines.png`). Install Pillow first with
`python -m pip install -r requirements/requirements-logos.txt`.

This utility uses your existing `.env` **CFBD_API_KEY** to retrieve
[SRS ratings](https://api.collegefootballdata.com/api/ratings) and the
[season's FBS team directory and logo URLs](https://api.collegefootballdata.com/api/teams).
It selects the 100 highest SRS ratings among that season's FBS teams. Ties are
ordered by school name. Same-name ratings are resolved using the FBS directory's
conference; unresolved identities stop the run. This is an SRS power ranking,
not an AP or CFP poll.
The default tries the current season, then explicitly reports a fallback to the
previous season if fewer than 100 rated FBS teams are available. `--year 2025`
selects exactly that season and disables fallback. API access depends on your
CollegeFootballData account; no Odds API requests or credits are used.

Use `--overwrite` to redownload existing valid files, `--count 25` for a smaller
selection, or `--output "another folder"` for a different destination. Each PNG
is proportionally resized, centered and validated; invalid existing files are
retried. The final summary shows the full output path and ready count.
`_download_manifest.json` records the season, ranking, SRS score, team ID,
filename and result. Failures return a nonzero exit status; rerun to retry them.
Existing files outside the selected top 100 are preserved. NCAAF upcoming-game
cards and game matchup headers use these local PNGs, matching the team display
name to the filename. Teams without a downloaded logo show their abbreviation.
Restart `python index.py` after adding logos so the dashboard indexes the files.

## NFL team logos

The standalone `download_nfl_logos.py` script downloads the 32 NFL team logos from ESPN's public team directory to **`images/NFL/Team Logos`**. Filenames use full team names, such as `Arizona Cardinals.png`. Every output is validated as a complete **500×500 PNG**. Sources with different dimensions are resized proportionally and centered on a transparent canvas.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements/requirements-logos.txt
.\.venv\Scripts\python.exe download_nfl_logos.py
```

This utility runs independently of the dashboard and does not need an API key. Existing valid logos are skipped. Use `--overwrite` to replace them, or `--output "another folder"` to choose a different destination. A failed run exits nonzero and can be rerun to retry missing files.

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

After adding or changing a key, restart `python index.py`, select NCAAF → Historical Data,
and click **Load historical data**. The default is the prior regular season for Michigan.
Once loaded, the statistic types reflect the available box-score fields: Scoring,
Offense (yards and first/third/fourth downs), Passing, Rushing, Defense, Kicking,
Returns, Fumbles and Penalties. Source failures appear above the selectors; an empty
selector is not evidence that the school has no statistics. Successful imports are
cached locally and can be reopened without another request for 24 hours.

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
  schedule.py                  Upcoming ESPN games and daily schedule cache
  odds_api.py                  Server-side Odds API connection and quota usage
  nfl_markets.py               Fixed 17-market FanDuel NFL pulls and saved snapshots
  nba_markets.py               NBA 13-market package and shared snapshot persistence
  nba_model_data.py            Paired NBA box scores and estimated possessions
  nba_ppp_service.py           NBA snapshot cutoffs and archived PPP estimates
  market_view.py               Saved-game matchup headers and player odds ladders
  forecast.py                  Rolling baseline and chronological evaluation
  ingest.py                    Multi-season command line importer
models/                       Game-outcome and player-stat forecasting packages
requirements/                 Runtime, documentation and logo dependency lists
scripts/check_project.py      Offline CI layout, import, math, route and logo checks
```

Run the offline checks:

```powershell
.\.venv\Scripts\python.exe scripts/check_project.py
.\.venv\Scripts\python.exe scripts/check_backtesting.py
.\.venv\Scripts\python.exe scripts/check_research.py
.\.venv\Scripts\python.exe scripts/check_market_history.py
.\.venv\Scripts\python.exe scripts/check_mispricing.py
.\.venv\Scripts\python.exe scripts/check_comparisons.py
.\.venv\Scripts\python.exe scripts/check_performance.py
```

The **Strategy & Research** tab has separate **Game Outcome** and **Player Stats**
strategy menus. Choose a method and sport to read its implemented methodology,
data requirements and configuration fields. All dataclass configuration fields,
including nested UO baseline settings for OAM/PGE, have typed controls and help
text. Model sport follows the sport selector; **Reset sport defaults** restores
that sport's settings. Changing the evaluation team preserves edited parameters.

Game Outcome experiments evaluate all eligible games or one team's games. Player
Stats can evaluate all players, a specific team's players, or one player for a
chosen statistic. All-player/team sample limits count distinct games; metrics
count individual player appearances. Player histories stay separate, and UO/OAM/
PGE simulate all selected subjects in each matchup together with finite team pools.
Evaluation scope does not remove eligible league/opponent history from training.

Research settings are validated before launch and passed directly to the actual
forecast engines. They participate in report/cache identity and are recorded in
JSON reports and the **Parameters used for this simulation** section. Research
and sport-page jobs retain separate page selections while sharing the progress
monitor, saved-report history, results charts and CSV exports. Experiments use
saved local inputs; load/prepare histories through the sport pages before testing.

Both research menus also contain **Methodology Comparison**. Select a sport and
all or any subset of the group's methods, then run with shared team/player,
statistic, date, game-count, postseason, lookback and reference-line settings.
Each method runs independently with its sport-specific defaults and appears in
the global progress monitor. By default the summary recalculates metrics on
identical games/player appearances successfully evaluated by every completed
method. An alternative shows each method's full evaluated sample; sample counts,
skips, interval levels and conflicting recorded outcomes remain explicit.
Compare MAE/RMSE, bias, coverage, event scores and rolling errors, export a summary
CSV, and inspect any method's original full report. **Previous simulations and
comparisons** can reopen a saved comparison or compare one saved simulation per
selected method, including experiments with customized parameters. Player runs
must use the same statistic and reference line. Small comparison manifests under
`data/backtesting/comparisons/<sport>/<group>/` reference the existing JSON/CSV
reports; results survive page navigation and application restarts. Failed methods
retain their reasons while successful method reports remain available.

Backtesting reuses identical calibration folds, ratings and seasonal source reads
within each run. Caches are bounded and isolated between jobs; training contents,
cutoffs and model settings identify each fit. Lookback windows, availability
rules, Monte Carlo counts and random seeds retain their original behavior.
Backtests run concurrently: change the controls and use **Run another backtest**
while an earlier run is active. The progress monitor at the far-left edge shows
all active runs; each backtesting page remembers its latest run and successful
results. Returning to a page resumes its progress or displays its completed
report, including runs that finished while you were elsewhere. Previous results
remain visible during a rerun and if that rerun fails. Page references persist
for the browser tab's session; completed reports are restored from local disk.
Identical active requests reuse the existing job. Each completed run saves its
own report and CSV exports.
Expand **Previous simulations** on a backtesting page, choose a saved run, and
click **Open simulation** to restore its results. The list is specific to that
sport and strategy and includes older runs saved before CSV exports were added.
Opening a historical run leaves any active jobs running in the background.

Files are stored under `data/backtesting/<sport>/<group>/<method>/runs/<scope>_<run-id>/`.
Each run contains `report.json` and CSV exports (separate margin and total files
for game models; one statistic file for player models). CSVs open in Excel.
The app loads JSON to retain the original configuration, predictions, intervals,
and skipped-game reasons together. Opening an older JSON-only run generates its
missing CSV exports. The hash-named JSON files one directory above are reusable
cached reports; either a cache report or its independent `report.json` archive
can restore a simulation. These local data files are ignored by Git.
PPP/EPA calibration computes only the folds needed to recover the exact latest
validation residuals, including postponed games and unavailable forecasts.

To measure reuse on your saved game data without downloading inputs or writing
reports, run `python scripts/benchmark_backtesting.py --sport NFL --model ppp --games 20`.
The benchmark compares cold calculations with run-local reuse and checks exact
equality of evaluated records and skipped-game reasons. Timings exclude input
loading and rendering and depend on your saved history and selected model.

Saved-season decoding reuses at most four payloads, keyed by their exact contents.
Each cache lookup returns an independent frame; expiry checks, manual refreshes
and stale-data notices still apply. Statistic filtering operates across numeric
columns together, and WHP quote audits index opposing selections once while
preserving the rule that duplicate opposing quotes are ambiguous.
`scripts/check_performance.py` checks these behaviors without source downloads.

GitHub Actions installs the project, checks dependencies, compiles source and runs this offline check on Python 3.11 and 3.13. It does not require local source caches, credentials, or test folders, and makes no external data/odds requests. Local `test/` and `tests/` folders are ignored by Git. If you maintain local tests, run them separately with `python -m unittest discover -s tests -v`.

Generated `output/` copies, caches, forecasts, account usage logs, local environment files, editor settings and scratch files are also ignored. The source code, dependency lists, team logos under `images/`, and canonical model manuals under `models/` remain versioned. Ignore rules do not remove files from your computer or erase earlier Git history. Live data access is a separate check.
