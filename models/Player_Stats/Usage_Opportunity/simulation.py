"""Shared team states and finite team pools for all players in a saved game."""
from dataclasses import dataclass, field
import numpy as np
from .opportunity_model import observed, draw_rows
from .errors import UsageOpportunityError


@dataclass
class SimulationResult:
    players: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    audit: dict = field(default_factory=dict)


def team_states(teams, config, rng, columns):
    """Common bootstrap quantile links matchup pace/volume scenarios.

    This is a declared positive-dependence research assumption; it is not an
    estimated matchup effect. Sampled rows preserve each team's channel mix.
    """
    common = rng.random(config.simulations)
    return {team: draw_rows(observed(rows, columns, config.minimum_games, config.team_window),
                            config, rng, common_uniform=common)
            for team, rows in teams.items()}


def integer_pool(values):
    values = np.asarray(values, float)
    if not np.isfinite(values).all():
        raise UsageOpportunityError('Team volume requires finite observed inputs; missing counts or minutes are not zero.')
    return np.maximum(0, np.rint(values)).astype(int)
