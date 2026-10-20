# Player statistics models

[Weighted Historical Performance](Weighted_Historical_Performance/README.md) implements NBA,
NFL and NCAAF player-game forecasts, source preparation, chronological calibration,
saved prop probabilities, fair odds and Edge. Its folder includes calculation
modules, input-schema documentation, the original source PDF and a separate
implementation-guide PDF.

Run `python -m models.Player_Stats.Weighted_Historical_Performance --help` from the
repository root. Prepare missing history with its CLI; NCAAF requires `CFBD_API_KEY`.
Historical Data remains team-only for college football.

[Usage & Opportunity](Usage_Opportunity/README.md) implements a separate NBA/NFL
hierarchy for team volume, player participation/allocation and conversion
efficiency, using shared Monte Carlo samples for prop probabilities and fair odds.
Its folder contains the calculation modules, input schema, CLI, backtesting tools,
original source PDF and code-specific implementation guide. In saved NBA/NFL
FanDuel games, select **Usage & Opportunity (UO)** in **Player Forecast Method**.
WHP remains the default; UO is unavailable for college games.

[Opponent-Adjusted Matchup](Opponent_Adjusted_Matchup/README.md) adds chronological
schedule-normalized defense factors, recency weighting, neutral shrinkage and NFL
position-specific components to the opportunity/efficiency simulation. Select
**Opponent-Adjusted Matchup (OAM)** in NBA/NFL saved-game player forecasts. Its
folder includes the source PDF, separate implementation guide, CLI, optional
pregame lineup scenarios and WHP/UO/OAM historical model comparison. NCAAF OAM
and unconnected tactical/personnel feeds are explicitly unavailable.

Team/game outcome strategies are organized separately under
[Game_Outcome](../Game_Outcome/README.md).
