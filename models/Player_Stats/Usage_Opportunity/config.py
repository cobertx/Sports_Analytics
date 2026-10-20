"""Fixed research defaults, not parameters tuned to the displayed market."""
from dataclasses import dataclass
import math
from .errors import UsageOpportunityError

MODEL_VERSION = 'uo-1'


@dataclass(frozen=True)
class UsageOpportunityConfig:
    role_window: int = 20
    role_half_life: float = 5
    efficiency_window: int = 60
    efficiency_half_life: float = 20
    team_window: int = 20
    minimum_games: int = 5
    simulations: int = 12000
    seed: int = 20261020

    def __post_init__(self):
        integers = (self.role_window, self.efficiency_window, self.team_window,
                    self.minimum_games, self.simulations, self.seed)
        if (any(not isinstance(x, int) or isinstance(x, bool) for x in integers)
                or not 2 <= self.minimum_games <= min(self.role_window, self.team_window)
                or self.efficiency_window < self.role_window
                or not 1000 <= self.simulations <= 200000 or self.seed < 0
                or not all(math.isfinite(x) and x > 0 for x in (self.role_half_life, self.efficiency_half_life))):
            raise UsageOpportunityError('Invalid Usage & Opportunity configuration.')

    @classmethod
    def for_sport(cls, sport):
        if sport == 'NBA':
            return cls()
        if sport == 'NFL':
            return cls(role_window=16, role_half_life=3, efficiency_window=32,
                       efficiency_half_life=8, team_window=16, minimum_games=3)
        raise UsageOpportunityError('Usage & Opportunity currently supports NBA and NFL only.')
