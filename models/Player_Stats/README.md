# Player statistics models

[Weighted Historical Performance](Weighted_Historical_Performance/README.md) implements NBA,
NFL and NCAAF player-game forecasts, source preparation, chronological calibration,
saved prop probabilities, fair odds and Edge. Its folder includes calculation
modules, input-schema documentation, the original source PDF and a separate
implementation-guide PDF.

Run `python -m models.Player_Stats.Weighted_Historical_Performance --help` from the
repository root. In a saved FanDuel game, open a player tab and use **Prepare
player history** if data is missing. NCAAF preparation requires `CFBD_API_KEY`;
Historical Data remains team-only for college football.

Team/game outcome strategies are organized separately under
[Game_Outcome](../Game_Outcome/README.md).
