"""Additive schedule-adjusted team ratings and joint NBA score simulation."""

from dataclasses import dataclass
import math

import numpy as np

from models.Game_Outcome.Pace_Possession.NBA_PACE import NBAPaceConfig, NBAPaceForecast, NBAPaceModel
from models.Game_Outcome.PPP.PPP import PPPError


@dataclass(frozen=True)
class NBAAdjustedEfficiencyConfig(NBAPaceConfig):
    efficiency_ridge: float = 18.
    venue_ridge: float = .0001
    seed: int = 20261119

    def __post_init__(self):
        super().__post_init__()
        if not math.isfinite(self.venue_ridge) or self.venue_ridge <= 0:
            raise PPPError("The home-court ridge penalty must be positive.")


def simulate(pace, pace_sd, rates, bias, covariance, draws, seed):
    """Draw game-level scoring rates in points per possession and reuse in OT."""
    rng = np.random.default_rng(seed)
    covariance = np.asarray(covariance, dtype=float)
    expected_rates = np.asarray(rates, dtype=float) + bias
    possessions = np.maximum(1, np.rint(rng.normal(pace, pace_sd, draws)))
    game_rates = np.maximum(.05, rng.multivariate_normal(expected_rates, covariance, draws))
    scores = np.rint(possessions[:, None] * game_rates).astype(int)
    return finish_overtime(scores, possessions, game_rates, pace, pace_sd, covariance, rng)


def finish_overtime(scores, possessions, game_rates, pace, pace_sd, rate_covariance, rng):
    tied = np.flatnonzero(scores[:, 0] == scores[:, 1])
    regulation_ties = len(tied)
    fraction = 5 / 48
    overtime_covariance = rate_covariance * (pace ** 2 * fraction)
    for _ in range(20):
        if not len(tied):
            break
        volume = np.maximum(1, np.rint(rng.normal(possessions[tied] * fraction, pace_sd * math.sqrt(fraction))))
        shocks = rng.multivariate_normal([0., 0.], overtime_covariance, len(tied))
        scores[tied] += np.maximum(0, np.rint(volume[:, None] * game_rates[tied] + shocks)).astype(int)
        tied = tied[scores[tied, 0] == scores[tied, 1]]
    # Resolve the exceptional path that is still tied after the overtime cap.
    scores[tied, rng.integers(0, 2, len(tied))] += 1
    return scores, regulation_ties


class NBAAdjustedEfficiencyForecast(NBAPaceForecast):
    simulate_scores = staticmethod(simulate)

    @staticmethod
    def adjustment_covariance(covariance, pace):
        # The availability overlay supplies score-point covariance. The
        # adjusted-efficiency simulation fits uncertainty in rate units.
        return covariance / pace ** 2


class NBAAdjustedEfficiencyModel(NBAPaceModel):
    method = "AODE"
    forecast_class = NBAAdjustedEfficiencyForecast
    description = "NBA possession-weighted additive offense and defensive suppression"
    covariance_key = "regulation_rate_covariance_points_per_possession"
    residual_variance_floor = .01 ** 2
    limitations = ("Estimated box-score possessions and time-scaled overtime. The base fit uses team and venue effects. "
                   "Rest, travel and uncertain lineup scenarios are outside the base fit.")

    def __init__(self, config=None):
        super().__init__(config or NBAAdjustedEfficiencyConfig())

    def _fit_efficiency(self, home, n, hi, ai, weights):
        possessions = home.possessions.to_numpy(float)
        exposure_weights = weights * possessions / 100
        rates = 100 * np.r_[home.points, home.points_allowed] / np.tile(possessions, 2)
        weights = np.tile(exposure_weights, 2)
        league = float(np.average(rates, weights=weights))

        rows = np.arange(len(home))
        away_rows = rows + len(home)
        design = np.zeros((len(rates), 2 * n + 1))
        design[rows, hi] = design[away_rows, ai] = 1
        design[rows, n + ai] = design[away_rows, n + hi] = -1
        venue = ~home.neutral.fillna(False).to_numpy(dtype=bool) if "neutral" in home else np.ones(len(home), dtype=bool)
        design[rows, -1], design[away_rows, -1] = .5 * venue, -.5 * venue

        weighted = design * weights[:, None]
        penalty = np.full(design.shape[1], self.config.efficiency_ridge)
        penalty[-1] = self.config.venue_ridge
        effects = np.linalg.solve(design.T @ weighted + np.diag(penalty), weighted.T @ (rates - league))
        return league, effects

    @staticmethod
    def _matchup_rates(efficiency, n, hi, ai, neutral):
        league, effects = efficiency
        half_edge = 0. if neutral else effects[-1] / 2
        rates = (league + effects[hi] - effects[n + ai] + half_edge,
                 league + effects[ai] - effects[n + hi] - half_edge)
        return np.asarray(rates) / 100

    @staticmethod
    def _scoring_residual(points, possessions, rates):
        return points / possessions - rates

    def _forecast_details(self, fit, home, away):
        league, effects = fit["efficiency"]
        n = len(fit["teams"])
        ratings = {}
        for team in (home, away):
            i = fit["teams"][team]
            offense = float(league + effects[i])
            defense = float(league - effects[n + i])
            ratings[team] = dict(offense=offense, defense=defense, net=offense - defense)
        return dict(adjusted_ratings=ratings, league_rating=league, home_edge=float(effects[-1]))
