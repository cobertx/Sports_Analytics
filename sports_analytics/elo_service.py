"""Separate frozen Elo/power forecasts using existing public historical adapters."""

from Models.Game_Outcome.ELO import ELOConfig, ELOForecast, ELOModel, NBAELOForecast
from .nba_model_data import NbaModelData
from .nfl_model_data import NflModelData
from .nba_markets import NBA_TEAM_CODES
from .ppp_service import PPPMarketService


class ELOMarketService(PPPMarketService):
    method = "ELO"
    forecast_class = ELOForecast
    model_version = 1
    input_patterns = ("team_games_*.csv", "team_games_*.manifest.json")

    def __init__(self, data=None, model=None):
        super().__init__(data or NflModelData(), model or ELOModel())


class NBAELOMarketService(ELOMarketService):
    sport = "NBA"
    season_start_month = 10
    team_codes = NBA_TEAM_CODES
    forecast_class = NBAELOForecast

    def __init__(self, data=None, model=None):
        super().__init__(data or NbaModelData(), model or ELOModel(ELOConfig.nba()))
