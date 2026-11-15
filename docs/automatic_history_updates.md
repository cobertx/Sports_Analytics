# Automatic analysis history updates

The Windows task **SportsAnalyticsHistoryRefresh** runs the project's `pythonw.exe` and `scripts/refresh_analysis_history.py` every five minutes. It runs without a visible console while this Windows user is signed in and the computer is awake, including when the dashboard is closed. Missed work catches up after the computer becomes available. The dashboard's normal `app.py` / `index.py` startup also starts the updater. A file lease prevents the two workers from running overlapping refresh cycles.

The updater refreshes current-season NBA and NFL team and player results in the shared SQLite database. It rebuilds NBA team inputs, existing NFL prepared game models, and existing NBA EPA event inputs. For college football it watches the saved schedules of schools already prepared in this workspace, refreshes around expected game completion, and performs daily catch-up for schedule changes and corrections. Missing completed-game team/player/opportunity statistics continue to retry. The configured CFBD account is used for these college scopes; new schools are prepared when selected as before. Older seasons remain cached unless explicitly refreshed.

Current-season source caches expire after five minutes across historical views, forecasts, career summaries, college results and event archives. Large archives use HTTP conditional requests when the provider supplies validators. Downloads and publications remain atomic. An incomplete replacement that drops existing game/participant records preserves the last valid dataset and reports a failed refresh.

Completed results record availability at successful retrieval when that is earlier than the conservative historical proxy. Repeat checks retain the first observed availability time. Prepared NBA inputs use those timestamps, so a published final does not wait an extra two days to enter a current forecast. Historical analysis still filters every observation by its original pregame cutoff; future results never become eligible simply because they have been downloaded.

Forecast cache identities include their actual input contents. NBA/NFL game forecasts prepare/check their inputs before looking up cached estimates. Kalshi, FanDuel and Market Summary views periodically check source revisions and recalculate affected estimates. Historical views and career charts also check for updates. New dashboard backtest/research jobs refresh their sport before freezing the simulation inputs. Saved reports and quote snapshots remain reproducible artifacts and are not overwritten with new estimates.

Refresh failures preserve saved data for inspection, but current forecast paths reject stale required sources. The dashboard shows failed or overdue updates and retries automatically. A successful check means the source was checked, not that a provider has already published a game it does not yet supply.

Inspect `data/cache/history_updates.json` or the dashboard's `/api/history-updates` endpoint for each source's last attempt, last success, latest available game, pending state and error. Windows Task Scheduler reports the unattended worker's exit result. A nonzero exit means at least one source needs retry.

Run a manual catch-up from the workspace:

```powershell
.venv/Scripts/python.exe scripts/refresh_analysis_history.py
```

Regression checks live in `scripts/check_history_updates.py`, alongside the existing model, cache, dashboard and historical-data checks.
