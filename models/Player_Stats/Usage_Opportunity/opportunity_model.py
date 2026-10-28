"""Recency-weighted participation scenarios and finite-volume allocation."""
import numpy as np
import pandas as pd
from models.season_weights import apply_season_weights
from .errors import UsageOpportunityError


def weights(n, half_life):
    if n < 1:
        raise UsageOpportunityError('No eligible observations for opportunity estimation.')
    result = np.exp2(-np.arange(n - 1, -1, -1, dtype=float) / half_life)
    return result / result.sum()


def history_weights(frame, half_life, config):
    return apply_season_weights(weights(len(frame), half_life), frame,
                                config.sport, config.prior_season_weight)


def observed(frame, columns, minimum, window):
    """Keep only jointly observed features, preserving within-game dependence."""
    missing = set(columns) - set(frame)
    if missing:
        raise UsageOpportunityError('Missing opportunity inputs: ' + ', '.join(sorted(missing)))
    result = frame
    if not all(pd.api.types.is_numeric_dtype(frame[column].dtype) for column in columns):
        result = frame.copy()
        result[list(columns)] = result[list(columns)].apply(pd.to_numeric, errors='coerce')
    # Nullable pandas columns preserve NA through isfinite; DataFrame.all()
    # skips those values by default. Convert explicitly so missing observations
    # cannot enter bootstrap states as apparently complete rows.
    values = result[list(columns)].to_numpy(dtype=float, na_value=np.nan)
    result = result[np.isfinite(values).all(axis=1)].sort_values(['event_date', 'game_id']).tail(window)
    integer_columns = [column for column in columns if column not in {'min', 'team_min'}]
    if integer_columns and not np.isclose(result[integer_columns].to_numpy(float) % 1, 0, atol=1e-8).all():
        raise UsageOpportunityError('Box-score events and yardage must be integer counts; only minutes may be fractional.')
    if len(result) < minimum:
        raise UsageOpportunityError(f'Need {minimum} complete observed games; only {len(result)} available. Prepare the prior season too.')
    return result


def draw_rows(frame, config, rng, *, common_uniform=None):
    w = history_weights(frame, config.role_half_life, config)
    u = rng.random(config.simulations) if common_uniform is None else common_uniform
    indices = np.searchsorted(np.cumsum(w), u, side='right').clip(max=len(frame)-1)
    return frame.iloc[indices].reset_index(drop=True)


def allocate(total, shares, rng):
    """Multinomial allocation to modeled players plus an explicit Other bucket.

    Each simulated column is integral; row sums equal the exact team volume.
    Conflicting sampled roles are normalized instead of exceeding the pool.
    """
    total, shares = np.asarray(total, int), np.asarray(shares, float)
    if (shares.ndim != 2 or shares.shape[0] != len(total) or (total < 0).any()
            or not np.isfinite(shares).all() or (shares < 0).any()):
        raise UsageOpportunityError('Invalid team allocation inputs.')
    shares = shares / np.maximum(1., shares.sum(axis=1))[:, None]
    probabilities = np.column_stack([shares, np.maximum(0., 1 - shares.sum(axis=1))])
    remaining = total.copy()
    mass = np.ones(len(total))
    columns = []
    for p in probabilities.T[:-1]:
        conditional = np.divide(p, mass, out=np.zeros_like(p), where=mass > 1e-12).clip(0, 1)
        count = rng.binomial(remaining, conditional)
        columns.append(count)
        remaining -= count
        mass = np.maximum(0., mass - p)
    return np.column_stack([*columns, remaining])


def share_rows(player, team, columns, config, rng, *, minutes=False, adjustment=None, environment=None):
    """Bootstrap aligned player/team appearances, not future realized workload."""
    totals = team[['game_id', 'team', *columns]].rename(columns={c: 'team_' + c for c in columns})
    sample = player.merge(totals, on=['game_id', 'team'], how='inner', validate='one_to_one')
    required = [*columns, *('team_' + c for c in columns)]
    if minutes:
        required += ['min', 'team_min']
    required = list(dict.fromkeys(required))
    sample = observed(sample, required, config.minimum_games, config.role_window)
    if sample[required].lt(0).any().any():
        raise UsageOpportunityError('Negative counts/minutes cannot define an opportunity share.')
    for column in columns:
        if (sample[column] > sample['team_' + column] + 1e-6).any():
            raise UsageOpportunityError(f'Player {column} exceeds its matched team total.')
    drawn = draw_rows(sample, config, rng) if environment is None else environment.draw_rows(sample, config, rng)
    shares = {column: np.divide(drawn[column].to_numpy(float), drawn['team_' + column].to_numpy(float),
                                out=np.zeros(config.simulations), where=drawn['team_' + column].to_numpy(float) > 0)
              for column in columns}
    if minutes:
        regulation = np.divide(240., drawn['team_min'].to_numpy(float),
                               out=np.ones(config.simulations), where=drawn['team_min'].to_numpy(float) > 0)
        shares['minutes'] = (drawn['min'].to_numpy(float) * regulation).clip(0, 48)
        if environment is not None:
            shares['minutes'] *= environment.duration[str(drawn.iloc[0].team)]/48
    if adjustment is not None:
        shares = adjustment.shares(drawn, shares)
    if environment is not None:
        shares = environment.role_shares(drawn, shares)
    return shares, sample
