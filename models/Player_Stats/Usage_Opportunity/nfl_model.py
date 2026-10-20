"""NFL plays -> dropbacks/runs -> target/carry shares -> catches and yardage.

Receiver catches/yardage are the common pools for QB completions/passing yards.
No invented route, snap or red-zone data; TD/order props remain unavailable.
"""
import numpy as np
from .efficiency_model import conversion, yards_per_event
from .opportunity_model import share_rows, allocate, observed
from .simulation import SimulationResult, team_states, integer_pool
from .errors import UsageOpportunityError

TEAM_COLUMNS = ('attempts', 'carries', 'sacks_suffered', 'targets', 'receptions', 'receiving_yards', 'rushing_yards')


def split_completions(attempts, completed, rng):
    """Assign exactly the shared catches to QB attempts without exceeding them."""
    remaining_attempts, remaining_makes = attempts.sum(axis=1).copy(), completed.copy()
    results = []
    for column in attempts.T[:-1]:
        count = rng.hypergeometric(column, remaining_attempts-column, remaining_makes)
        results.append(count)
        remaining_attempts -= column
        remaining_makes -= count
    return np.column_stack([*results, remaining_makes])


def simulate_nfl(players, teams, config, rng, *, adjustment=None, environment=None):
    result = SimulationResult()
    state_source = team_states if environment is None else environment.team_states
    states = state_source(teams, config, rng, TEAM_COLUMNS)
    for team, state in states.items():
        if state[['attempts', 'carries', 'sacks_suffered', 'targets', 'receptions']].lt(0).any().any():
            raise UsageOpportunityError('NFL workload counts cannot be negative.')
        ids, roles, frames, samples = [], [], {}, {}
        for identity, info in players.items():
            if info['team'] != team:
                continue
            try:
                frame = info['history']
                columns = ['attempts','carries','targets']
                if environment is not None and 'pge_scramble' in frame and 'pge_scramble' in teams[team]:
                    columns.append('pge_scramble')
                role, sample = share_rows(frame, teams[team], columns, config, rng, adjustment=adjustment, environment=environment)
                # Observed zero opportunities may be retained; missing features
                # are unavailable rather than silently inferred to be zero.
                observed(frame, ['receptions', 'receiving_yards', 'rushing_yards', 'completions', 'interceptions'],
                         config.minimum_games, config.efficiency_window)
                ids.append(identity); roles.append(role); frames[identity]=frame; samples[identity]=sample
            except UsageOpportunityError as exc:
                result.errors[identity] = str(exc)
        if not ids:
            continue
        environment_audit = {}
        if environment is not None:
            plays,attempts,carry_pool,environment_audit = environment.nfl_opportunities(team,state,rng)
        else:
            plays = integer_pool(state.attempts + state.carries + state.sacks_suffered)
            historical_dropbacks = state.attempts.to_numpy(float)+state.sacks_suffered.to_numpy(float)
            p_pass = np.divide(historical_dropbacks, plays, out=np.zeros(config.simulations), where=plays > 0).clip(0, 1)
            if adjustment is not None:
                p_pass = adjustment.ratio(team, state, 'nfl:dropback', p_pass).clip(0, 1)
            dropbacks = rng.binomial(plays, p_pass)
            p_sack = np.divide(state.sacks_suffered.to_numpy(float), historical_dropbacks,
                               out=np.zeros(config.simulations), where=historical_dropbacks > 0).clip(0, 1)
            if adjustment is not None:
                p_sack = adjustment.ratio(team, state, 'nfl:sack', p_sack).clip(0, 1)
            attempts = dropbacks-rng.binomial(dropbacks, p_sack)
            carry_pool = plays-dropbacks
        p_target = np.divide(state.targets.to_numpy(float), state.attempts.to_numpy(float),
                             out=np.zeros(config.simulations), where=state.attempts.to_numpy(float) > 0)
        if (p_target > 1 + 1e-6).any() or (state.receptions > state.targets).any():
            raise UsageOpportunityError('NFL targets/receptions exceed their eligible pass opportunity counts.')
        if adjustment is not None:
            p_target = adjustment.ratio(team, state, 'nfl:target', p_target).clip(0, 1)
        targets = allocate(rng.binomial(attempts, p_target.clip(0,1)), np.column_stack([r['targets'] for r in roles]), rng)
        carries = (allocate(carry_pool, np.column_stack([r['carries'] for r in roles]), rng) if environment is None else
                   environment.allocate_carries(carry_pool,roles,environment_audit,rng))
        qb_attempts = allocate(attempts, np.column_stack([r['attempts'] for r in roles]), rng)
        caught, rec_yards, rush_yards, efficiencies, used = [], [], [], {}, {}
        for i, identity in enumerate([*ids, '__other__']):
            frame = teams[team] if identity == '__other__' else frames[identity]
            p, posterior, conversion_sample = conversion(frame, 'receptions', 'targets', config, rng, adjustment=adjustment, team=team)
            catches = rng.binomial(targets[:, i], p)
            yards, yard_fit, yard_sample = yards_per_event(frame, 'receiving_yards', 'receptions', catches, config, rng, adjustment=adjustment, team=team)
            rush, rush_fit, rush_sample = yards_per_event(frame, 'rushing_yards', 'carries', carries[:, i], config, rng, adjustment=adjustment, team=team)
            caught.append(catches); rec_yards.append(yards); rush_yards.append(rush)
            efficiencies[identity] = dict(catch=posterior, receiving=yard_fit, rushing=rush_fit)
            used[identity] = [conversion_sample, yard_sample, rush_sample]
        caught, rec_yards, rush_yards = np.column_stack(caught), np.column_stack(rec_yards), np.column_stack(rush_yards)
        completed = split_completions(qb_attempts, caught.sum(axis=1), rng)
        total_yards = rec_yards.sum(axis=1)
        # Exact totals, with integer rounding residual assigned to Other.
        fractions = np.divide(completed, completed.sum(axis=1)[:, None],
                              out=np.zeros_like(completed, dtype=float), where=completed.sum(axis=1)[:, None] > 0)
        passing_yards = np.rint(fractions*total_yards[:, None]).astype(int)
        passing_yards[:, -1] += total_yards-passing_yards.sum(axis=1)
        for i, identity in enumerate(ids):
            frame = frames[identity].copy()
            frame['incompletions'] = frame.attempts-frame.completions
            interception_p, interception_fit, interception_sample = conversion(frame, 'interceptions', 'incompletions', config, rng, adjustment=adjustment, team=team)
            interceptions = rng.binomial(qb_attempts[:, i]-completed[:, i], interception_p)
            result.players[identity] = dict(attempts=qb_attempts[:, i], completions=completed[:, i],
                passing_yards=passing_yards[:, i], interceptions=interceptions, carries=carries[:, i],
                rushing_yards=rush_yards[:, i], targets=targets[:, i], receptions=caught[:, i], receiving_yards=rec_yards[:, i])
            histories = [samples[identity], *used[identity], interception_sample,
                         observed(teams[team], TEAM_COLUMNS, config.minimum_games, config.team_window)]
            result.diagnostics[identity] = dict(training_games=len(samples[identity]),
                last_game_date=max(f.event_date.max() for f in histories).date().isoformat(),
                opportunity_projection={name: float(values[:, i].mean()) for name, values in
                                        [('attempts', qb_attempts), ('carries', carries), ('targets', targets)]},
                efficiency_projection={**efficiencies[identity], 'interceptions_per_incompletion': interception_fit},
                limitations='Observed target shares replace unavailable routes/snaps. Yards use game-level residuals, not play-level gain samples. TDs need red-zone inputs. No injury or score/clock adjustment.')
        result.audit[team] = dict(**environment_audit, plays=plays, attempts=attempts, targets=targets, carries=carries,
            qb_attempts=qb_attempts, completions=completed, receptions=caught,
            passing_yards=passing_yards, receiving_yards=rec_yards)
    return result
