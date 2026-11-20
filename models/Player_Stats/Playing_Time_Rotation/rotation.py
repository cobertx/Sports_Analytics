"""Complete historical team boxes provide joint participation and minutes scenarios."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from models.season_weights import calendar_weights
from sports_analytics.data import DataSourceError

REGULATION = 48
TEAM_MINUTES = 5 * REGULATION


def project_minutes(proposed, caps, floors=None):
    """Project each draw onto a capped, 240-minute simplex."""
    proposed, caps = np.asarray(proposed, float), np.asarray(caps, float)
    floors = np.zeros_like(caps) if floors is None else np.asarray(floors, float)
    if (proposed.ndim != 2 or proposed.shape != caps.shape
            or floors.shape != caps.shape or not np.isfinite(floors).all()
            or (floors < 0).any() or (floors > caps).any() or (floors.sum(axis=1) > TEAM_MINUTES).any()
            or not np.isfinite(proposed).all() or not np.isfinite(caps).all()
            or (caps < 0).any() or (caps > REGULATION).any() or (caps.sum(axis=1) < TEAM_MINUTES).any()):
        raise DataSourceError("No feasible 240-minute rotation under the available player caps.")
    low = -proposed.max(axis=1)
    high = (caps - proposed).max(axis=1)
    for _ in range(40):
        middle = (low + high) / 2
        short = np.clip(proposed + middle[:, None], floors, caps).sum(axis=1) < TEAM_MINUTES
        low = np.where(short, middle, low)
        high = np.where(short, high, middle)
    return np.clip(proposed + ((low + high) / 2)[:, None], floors, caps)


@dataclass(frozen=True)
class RotationDraws:
    minutes: dict
    training_games: int
    last_game_date: str


def rotation_history(players, teams):
    """Return verified complete team boxes and minutes normalized to regulation."""
    if "min" not in players or "min" not in teams:
        raise DataSourceError("Player and team minutes are required for Playing-Time Rotation.")
    game_minutes = teams.drop_duplicates("game_id", keep=False).set_index("game_id")
    game_minutes = game_minutes.sort_values("event_date", kind="stable")
    totals = pd.to_numeric(game_minutes["min"], errors="coerce")
    # Source boxes can round individual minutes. Allow a small team-level error
    # while rejecting incomplete lineups and durations between NBA periods.
    overtime = ((totals - TEAM_MINUTES) / 25).round()
    budgets = TEAM_MINUTES + 25 * overtime
    candidate_games = game_minutes.loc[overtime.ge(0) & (totals - budgets).abs().le(2)]
    data = players.loc[players.game_id.isin(candidate_games.index), ["game_id", "entity_id", "min"]].copy()
    data["min"] = pd.to_numeric(data["min"], errors="coerce")
    wide = data.pivot(index="game_id", columns="entity_id", values="min").fillna(0)
    budgets = budgets.reindex(wide.index)
    bad_games = data.loc[data["min"].isna() | ~np.isfinite(data["min"]) | data["min"].lt(0), "game_id"]
    complete = (~wide.index.isin(bad_games) & wide.gt(0).sum(axis=1).ge(5)
                & (wide.sum(axis=1) - budgets).abs().le(3)
                & wide.max(axis=1).le(budgets / 5 + .05))
    games = candidate_games.loc[candidate_games.index.isin(wide.index[complete])]
    return wide.reindex(games.index).mul(TEAM_MINUTES / budgets.reindex(games.index), axis=0).clip(upper=REGULATION), games


def draw_rotation(players, teams, current_ids, cutoff, config, rng):
    """Sample whole observed participation patterns and regularize positive minutes."""
    return sample_rotation(*rotation_history(players, teams), current_ids, cutoff, config, rng)


def sample_rotation(wide, games, current_ids, cutoff, config, rng, *, active=(), limits=None, size=None):
    games = games.tail(config.rotation_window)
    wide = wide.reindex(games.index)
    active = set(active)
    eligible = set(wide.columns[wide.gt(0).any()])
    if not active <= eligible & set(current_ids):
        raise DataSourceError("A required participant has no eligible minutes in the saved rotation window.")
    roster = sorted((set(wide.columns[wide.tail(config.roster_window).gt(0).any()]) | active) & set(current_ids))
    wide = wide.loc[:, roster]
    maximum = np.array([(limits or {}).get(player, REGULATION) for player in roster], dtype=float)
    forced = np.array([player in active for player in roster], dtype=bool)
    available = wide.gt(0).to_numpy() | forced
    feasible = (available * maximum).sum(axis=1) >= TEAM_MINUTES
    wide, games = wide.loc[feasible], games.loc[feasible]
    if len(games) < config.baseline.minimum_games:
        raise DataSourceError(f"Playing-Time Rotation needs {config.baseline.minimum_games} complete saved rotation games "
                              f"with at least five current eligible players; {len(games)} available.")

    values = wide.to_numpy(float)
    weights = np.asarray(calendar_weights(games, cutoff, config.minutes_half_life, "NBA",
                                         config.baseline.prior_season_weight), dtype=float)
    total_weight = weights.sum()
    if not np.isfinite(total_weight) or total_weight <= 0:
        raise DataSourceError("No positive rotation weights. Increase the minutes half-life.")
    weights = weights / total_weight
    positive = values > 0
    role_weights = positive * weights[:, None]
    mass = role_weights.sum(axis=0)
    means = np.divide(weights @ values, mass, out=np.zeros_like(mass), where=mass > 0)
    squared = (role_weights ** 2).sum(axis=0)
    effective = np.divide(mass ** 2, squared, out=np.zeros_like(mass), where=squared > 0)
    strengths = effective + config.role_prior_strength
    share = np.divide(effective, strengths, out=np.ones_like(effective), where=strengths > 0)
    priors = means.copy()
    # Role groups are inferred from pregame minutes. Bench and high-minute
    # roles borrow information within their own group.
    for lower, upper in [(0, 14), (14, 26), (26, REGULATION + 1)]:
        tier = (means >= lower) & (means < upper) & (mass > 0)
        if tier.any():
            priors[tier] = np.median(means[tier])
    templates = values[rng.choice(len(values), size=config.baseline.simulations if size is None else size, p=weights)]
    caps = (templates > 0) | forced
    templates = np.where(forced & (templates == 0), means, templates)
    adjusted = templates + ((1 - share) * (priors - means))[None, :]
    floors = np.broadcast_to(forced * np.minimum(1, maximum), adjusted.shape)
    minutes = project_minutes(adjusted, maximum * caps, floors)
    return RotationDraws(dict(zip(roster, minutes.T)), len(games), games.event_date.max().date().isoformat())
