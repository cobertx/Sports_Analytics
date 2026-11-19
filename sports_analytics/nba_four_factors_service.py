"""Archive NBA Four Factors estimates at the market snapshot cutoff."""

from models.Game_Outcome.Four_Factors import NBAFourFactorsForecast, NBAFourFactorsModel
from .nba_pace_service import NBAPaceMarketService


class NBAFourFactorsMarketService(NBAPaceMarketService):
    method = "FF"
    forecast_class = NBAFourFactorsForecast

    def __init__(self, data=None, model=None):
        super().__init__(data, model or NBAFourFactorsModel())
