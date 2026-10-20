"""Research defaults for market-blind game volume and role scenarios."""
from dataclasses import dataclass, field
import math
from models.Player_Stats.Usage_Opportunity.config import UsageOpportunityConfig

MODEL_VERSION = 'pge-1'


@dataclass(frozen=True)
class PaceConfig:
    baseline: UsageOpportunityConfig = field(default_factory=UsageOpportunityConfig)
    league_window: int = 512
    minimum_league_games: int = 24
    pace_prior: float = 5
    ridge: float = 10
    scenario_half_life: float = 128
    role_smoothing: float = .25

    def __post_init__(self):
        if (not isinstance(self.league_window, int) or not isinstance(self.minimum_league_games, int)
                or not 4 <= self.minimum_league_games <= self.league_window
                or not all(math.isfinite(x) and x > 0 for x in
                           (self.pace_prior, self.ridge, self.scenario_half_life))
                or not 0 < self.role_smoothing <= 1):
            raise ValueError('Invalid Pace & Game Environment configuration.')

    @classmethod
    def for_sport(cls, sport):
        return cls(baseline=UsageOpportunityConfig.for_sport(sport),
                   league_window=512 if sport == 'NBA' else 256,
                   scenario_half_life=128 if sport == 'NBA' else 64)
