"""Settings for point-in-time NBA lineup conditioning."""

from dataclasses import dataclass
import math

from models.Player_Stats.Playing_Time_Rotation.config import RotationConfig


@dataclass(frozen=True)
class InjuryLineupConfig(RotationConfig):
    report_max_age_minutes: int = 60
    minimum_lineup_games: int = 3
    role_shrinkage_games: float = 8.
    shooting_prior_attempts: float = 45.

    def __post_init__(self):
        super().__post_init__()
        if (type(self.report_max_age_minutes) is not int or not 1 <= self.report_max_age_minutes <= 360
                or type(self.minimum_lineup_games) is not int or not 2 <= self.minimum_lineup_games <= self.baseline.role_window
                or not math.isfinite(self.role_shrinkage_games) or self.role_shrinkage_games <= 0
                or not math.isfinite(self.shooting_prior_attempts) or self.shooting_prior_attempts <= 0):
            raise ValueError("Invalid NBA injury and lineup settings.")

    @classmethod
    def for_sport(cls, sport):
        if sport != "NBA":
            raise ValueError("Injury & Lineup Adjustment supports NBA player statistics.")
        return cls()
