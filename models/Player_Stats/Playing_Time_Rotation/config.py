"""Configuration for NBA playing-time forecasts."""

from dataclasses import dataclass
import math

from models.Player_Stats.Usage_Opportunity.config import UsageOpportunityConfig


@dataclass(frozen=True)
class RotationConfig:
    rotation_window: int = 25
    roster_window: int = 15
    minutes_half_life: float = 12.0
    role_prior_strength: float = 5.0
    baseline: UsageOpportunityConfig = UsageOpportunityConfig()

    def __post_init__(self):
        windows = (self.rotation_window, self.roster_window)
        if (any(type(window) is not int for window in windows)
                or not self.baseline.minimum_games <= self.roster_window <= self.rotation_window <= 250
                or self.baseline.sport != "NBA"
                or not math.isfinite(self.minutes_half_life) or self.minutes_half_life <= 0
                or not math.isfinite(self.role_prior_strength) or self.role_prior_strength < 0):
            raise ValueError("Invalid NBA playing-time rotation settings.")

    @classmethod
    def for_sport(cls, sport):
        if sport != "NBA":
            raise ValueError("Playing-Time Rotation supports NBA player statistics.")
        return cls()
