"""Archive snapshot-cutoff NBA Pace & Possession forecasts."""

from models.Game_Outcome.Pace_Possession import NBAPaceForecast, NBAPaceModel
from .nba_model_data import NbaPaceData
from .nba_ppp_service import NBAPPPMarketService


class NBAPaceMarketService(NBAPPPMarketService):
    method = "PACE"
    forecast_class = NBAPaceForecast

    def __init__(self, data=None, model=None):
        super().__init__(data or NbaPaceData(), model or NBAPaceModel())
