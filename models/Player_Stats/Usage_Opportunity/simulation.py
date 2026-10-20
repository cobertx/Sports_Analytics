"""Shared team states and finite team pools for all players in a saved game."""
from dataclasses import dataclass, field
import numpy as np
from .opportunity_model import observed, draw_rows


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
    return np.maximum(0, np.rint(np.asarray(values, float))).astype(int)
