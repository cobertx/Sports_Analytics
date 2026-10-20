"""Fixed research defaults for chronological schedule-adjusted defense."""
from dataclasses import dataclass, field
import math
from models.Player_Stats.Usage_Opportunity.config import UsageOpportunityConfig


@dataclass(frozen=True)
class MatchupConfig:
    baseline: UsageOpportunityConfig = field(default_factory=UsageOpportunityConfig)
    defense_window: int = 30
    defense_half_life: float = 8
    defense_prior: float = 8
    offense_prior: float = 4
    position_prior: float = 16
    minimum_defense_games: int = 3
    league_window: int = 256
    minimum_league_games: int = 16
    factor_floor: float = .5
    factor_ceiling: float = 1.8

    def __post_init__(self):
        integers = (self.defense_window, self.minimum_defense_games, self.league_window, self.minimum_league_games)
        if (any(not isinstance(x, int) or isinstance(x, bool) for x in integers)
                or not 2 <= self.minimum_defense_games <= self.defense_window
                or not 2 <= self.minimum_league_games <= self.league_window
                or not all(math.isfinite(x) and x > 0 for x in
                           (self.defense_half_life, self.defense_prior, self.offense_prior, self.position_prior))
                or not 0 < self.factor_floor < 1 < self.factor_ceiling):
            raise ValueError('Invalid opponent-adjusted matchup configuration.')

    @classmethod
    def for_sport(cls, sport):
        base = UsageOpportunityConfig.for_sport(sport)
        return cls(baseline=base) if sport == 'NBA' else cls(
            baseline=base, defense_window=16, defense_half_life=4, defense_prior=10,
            offense_prior=6, position_prior=20, league_window=128)


MODEL_VERSION = 'oam-1'
