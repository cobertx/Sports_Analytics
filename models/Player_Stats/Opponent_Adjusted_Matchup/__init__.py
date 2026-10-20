"""NBA/NFL schedule-adjusted player opportunity and efficiency forecasts."""
from .config import MatchupConfig
from .distributions import MatchupForecast
from .prediction_service import OpponentAdjustedMarketService

__all__=['MatchupConfig','MatchupForecast','OpponentAdjustedMarketService']
