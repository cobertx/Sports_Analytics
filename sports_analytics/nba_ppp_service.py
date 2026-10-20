"""Snapshot-cutoff NBA PPP estimates, archived separately from NFL forecasts."""

from Models.Game_Outcome.PPP.NBA_PPP import NBAPPPForecast, NBAPPPModel
from .nba_markets import NBA_TEAM_CODES
from .nba_model_data import NbaModelData
from .ppp_service import PPPMarketService


class NBAPPPMarketService(PPPMarketService):
    sport = "NBA"
    forecast_class = NBAPPPForecast
    team_codes = NBA_TEAM_CODES
    season_start_month = 10

    def __init__(self, data=None, model=None):
        super().__init__(data or NbaModelData(), model or NBAPPPModel())
