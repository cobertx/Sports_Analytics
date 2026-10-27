"""NBA box-score hierarchy: minutes, finite shot allocation, makes and combos.

Rebound/assist/defensive-event pools are explicit box-score proxies. Tracking
chances, injury information and actual overtime predictions are not inferred.
"""
import numpy as np
from .efficiency_model import conversion
from .opportunity_model import share_rows, allocate, observed
from .simulation import SimulationResult, team_states, integer_pool
from .errors import UsageOpportunityError

CHANNELS = ('two_pa', 'fg3a', 'fta', 'oreb', 'dreb', 'ast', 'stl', 'blk', 'tov')


def features(frame):
    required = {'fga', 'fg3a', 'fgm', 'fg3m', 'ftm', 'fta', 'min', 'oreb', 'dreb', 'ast', 'stl', 'blk', 'tov'}
    if not required <= set(frame):
        raise UsageOpportunityError('NBA opportunity model needs complete shooting, minutes and box-score channels.')
    data = frame.copy()
    data['two_pa'], data['two_pm'] = data.fga - data.fg3a, data.fgm - data.fg3m
    return data


def simulate_nba(players, teams, config, rng, *, adjustment=None, environment=None):
    result = SimulationResult()
    teams = {team: features(rows) for team, rows in teams.items()}
    state_source = team_states if environment is None else environment.team_states
    states = state_source(teams, config, rng, [*CHANNELS, 'min', 'two_pm', 'fg3m', 'ftm'])
    for team, state in states.items():
        if (state['min'] < 240).any() or state[list(CHANNELS)].lt(0).any().any():
            raise UsageOpportunityError('NBA team counts must be nonnegative and team minutes at least 240.')
        ids, roles, posteriors, samples = [], [], {}, {}
        for identity, info in players.items():
            if info['team'] != team:
                continue
            try:
                frame = features(info['history'])
                role, role_sample = share_rows(frame, teams[team], [*CHANNELS, 'min'], config, rng, minutes=True, environment=environment)
                fit = {}
                for name, made, attempts in [('two', 'two_pm', 'two_pa'), ('three', 'fg3m', 'fg3a'), ('ft', 'ftm', 'fta')]:
                    fit[name] = conversion(frame, made, attempts, config, rng, adjustment=adjustment, team=team)
                ids.append(identity); roles.append(role); posteriors[identity] = fit
                samples[identity] = role_sample
            except UsageOpportunityError as exc:
                result.errors[identity] = f'Current-team opportunity history for {identity} ({team}): {exc}'
        if not ids:
            continue
        minutes = np.column_stack([role['minutes'] for role in roles])
        budget = 240. if environment is None else 5*environment.duration[team]
        scale = np.minimum(1., budget / np.maximum(1., minutes.sum(axis=1)))
        minutes *= scale[:, None]
        pools, allocations = {}, {}
        for channel in CHANNELS:
            # Normalize historical team overtime to a regulation baseline.
            projected = state[channel].to_numpy(float) * 240/state['min'].to_numpy(float)
            if environment is not None:
                projected = environment.nba_pool(team, state, channel)
            if adjustment is not None:
                projected = adjustment.ratio(team, state, 'nba:' + channel, projected)
            pool = integer_pool(projected)
            shares = np.column_stack([role[channel] for role in roles]) * scale[:, None]
            allocations[channel] = allocate(pool, shares, rng)
            pools[channel] = pool
        # The Other bucket uses team-level shooting rates; quoted players have
        # separately estimated beta posteriors and shared volume uncertainty.
        makes = {}
        for channel, name, made in [('two_pa', 'two', 'two_pm'), ('fg3a', 'three', 'fg3m'), ('fta', 'ft', 'ftm')]:
            p_other, _, _ = conversion(teams[team], made, channel, config, rng, adjustment=adjustment, team=team)
            probabilities = np.column_stack([posteriors[identity][name][0] for identity in ids] + [p_other])
            makes[name] = rng.binomial(allocations[channel], probabilities)
        # An assist requires a made FG. Dead/non-assisted makes are retained.
        team_fg = makes['two'].sum(axis=1) + makes['three'].sum(axis=1)
        assist_pool = np.minimum(pools['ast'], team_fg)
        pools['ast'] = assist_pool
        allocations['ast'] = allocate(assist_pool, np.column_stack([r['ast'] for r in roles])*scale[:, None], rng)
        for i, identity in enumerate(ids):
            player_fg = makes['two'][:, i] + makes['three'][:, i]
            assists = np.minimum(allocations['ast'][:, i], team_fg-player_fg)
            allocations['ast'][:, -1] += allocations['ast'][:, i]-assists
            allocations['ast'][:, i] = assists
            values = dict(pts=2*makes['two'][:, i]+3*makes['three'][:, i]+makes['ft'][:, i],
                          reb=allocations['oreb'][:, i]+allocations['dreb'][:, i], ast=assists,
                          fg3m=makes['three'][:, i], stl=allocations['stl'][:, i],
                          blk=allocations['blk'][:, i], tov=allocations['tov'][:, i])
            result.players[identity] = values
            used = [samples[identity], *[fit[2] for fit in posteriors[identity].values()],
                    observed(teams[team], [*CHANNELS, 'min'], config.minimum_games, config.team_window)]
            result.diagnostics[identity] = dict(
                training_games=len(samples[identity]), last_game_date=max(f.event_date.max() for f in used).date().isoformat(),
                opportunity_projection={'minutes': float(minutes[:, i].mean()),
                    **{c: float(allocations[c][:, i].mean()) for c in CHANNELS}},
                efficiency_projection={name: fit[1] for name, fit in posteriors[identity].items()},
                limitations='Regulation-only; historical role scenarios. Rebounds/assists/defense use box-score pools, not tracking chances. No injury, lineup or opponent adjustment.')
        result.audit[team] = dict(minutes_total=minutes.sum(axis=1), other_minutes=budget-minutes.sum(axis=1),
                                  pools=pools, allocations=allocations, makes=makes)
    return result
