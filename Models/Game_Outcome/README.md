# Game outcome models

This package contains team/game forecasts used by FanDuel Market. Each
methodology folder includes its calculation code, implementation manual and
original research reference.

| Method | Implementation | Documentation |
| --- | --- | --- |
| PPP | NFL points per play; NBA points per possession | [Guide](PPP/PPP_Implementation_Guide.pdf), [README](PPP/README.md) |
| EPA | NFL EPA scoring bridge; NBA event-based EPV | [Guide](EPA/EPA_Implementation_Guide.pdf), [README](EPA/README.md) |
| ELO | Sequential Elo, power margins and separate scoring/pace | [Guide](ELO/ELO_Implementation_Guide.pdf), [README](ELO/README.md) |

Run commands from the project root:

```powershell
python -m models.Game_Outcome.PPP --help
python -m models.Game_Outcome.PPP.NBA_PPP --help
python -m models.Game_Outcome.EPA --help
python -m models.Game_Outcome.EPA.NBA_EPA --help
python -m models.Game_Outcome.ELO --help
```

Dashboard services remain in `sports_analytics`. Prepared data and frozen
estimates remain under `data/modeling/nfl` and `data/modeling/nba`; saved quotes
remain in `data/market_data`. The package move changes imports and launch paths,
with the calculation methods and snapshot cache identities preserved.

The sibling [Player_Stats](../Player_Stats/README.md) package is reserved for
player-statistic prediction models.
