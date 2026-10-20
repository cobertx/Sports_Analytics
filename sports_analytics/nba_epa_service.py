"""Frozen NBA event-based EPV forecasts through the shared market contract."""

from Models.Game_Outcome.EPA.NBA_EPA import NBAEPAForecast, NBAEPAModel
from Models.Game_Outcome.EPA.nba_epa_training import NBAEPAData
from .nba_ppp_service import NBAPPPMarketService


class NBAEPAMarketService(NBAPPPMarketService):
    method = "EPA"
    forecast_class = NBAEPAForecast
    model_version = 2
    input_patterns = ("team_games_*.csv", "team_games_*.manifest.json", "state_statistics_*.npz")

    def __init__(self, data=None, model=None):
        super().__init__(data or NBAEPAData(), model or NBAEPAModel())
