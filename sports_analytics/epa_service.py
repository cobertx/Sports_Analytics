"""Independent EPA estimates using the shared saved-game cutoff/cache contract."""

from Models.Game_Outcome.EPA.EPA import EPAForecast, EPAModel
from .nfl_model_data import NflModelData
from .ppp_service import PPPMarketService


class EPAMarketService(PPPMarketService):
    method = "EPA"
    forecast_class = EPAForecast
    model_version = 1

    def __init__(self, data=None, model=None):
        super().__init__(data or NflModelData(), model or EPAModel())
