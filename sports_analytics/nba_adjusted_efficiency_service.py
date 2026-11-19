"""Archive snapshot-cutoff NBA adjusted offense and defense forecasts."""

from models.Game_Outcome.Adjusted_Offensive_Defensive_Efficiency import (
    NBAAdjustedEfficiencyForecast, NBAAdjustedEfficiencyModel,
)
from .nba_pace_service import NBAPaceMarketService


class NBAAdjustedEfficiencyMarketService(NBAPaceMarketService):
    method = "AODE"
    forecast_class = NBAAdjustedEfficiencyForecast

    def __init__(self, data=None, model=None):
        super().__init__(data, model or NBAAdjustedEfficiencyModel())
