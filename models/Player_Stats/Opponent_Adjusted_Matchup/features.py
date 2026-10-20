"""Actual production and aligned exposures: all features retain source units."""
import numpy as np
import pandas as pd

ALIASES = {'WAS': 'WSH', 'LA': 'LAR', 'JAC': 'JAX', 'BRK': 'BKN', 'CHO': 'CHA', 'PHO': 'PHX'}


def feature_rows(sport, players, teams):
    team = teams.copy()
    team['offense'] = team.team.replace(ALIASES)
    team['defense'] = team.opponent.astype('string').replace(ALIASES)
    if team.defense.isna().any() or team.defense.str.strip().eq('').any():
        raise ValueError('Opponent modeling requires actual opponent identities for team games.')
    if team.duplicated(['game_id', 'offense']).any():
        raise ValueError('Team games must have unique game/club identities.')
    frames = []

    def add(frame, name, actual, exposure):
        a, e = pd.to_numeric(actual, errors='coerce'), pd.to_numeric(exposure, errors='coerce')
        mask = np.isfinite(a) & np.isfinite(e) & e.gt(0)
        selected = frame.loc[mask, ['game_id', 'offense', 'defense', 'event_date', 'available_at']].copy()
        selected['actual'], selected['exposure'], selected['component'] = a[mask], e[mask], name
        frames.append(selected)

    if sport == 'NBA':
        required = {'fga','fgm','fg3a','fg3m','fta','ftm','oreb','dreb','ast','stl','blk','tov'}
        if not required <= set(team):
            raise ValueError('NBA defense features need complete team box-score shooting and opportunity columns.')
        possessions = team.fga + .44*team.fta - team.oreb + team.tov
        for channel in ['oreb','dreb','ast','stl','blk','tov','fg3a','fta']:
            add(team, 'nba:'+channel, team[channel], possessions)
        add(team, 'nba:two_pa', team.fga-team.fg3a, possessions)
        add(team, 'nba:two_p', team.fgm-team.fg3m, team.fga-team.fg3a)
        add(team, 'nba:three_p', team.fg3m, team.fg3a)
        add(team, 'nba:ft_p', team.ftm, team.fta)
    elif sport == 'NFL':
        required = {'attempts','carries','sacks_suffered','targets','receptions','receiving_yards',
                    'rushing_yards','completions','interceptions'}
        if not required <= set(team):
            raise ValueError('NFL defense features need observed attempts, sacks, carries, targets and conversion totals.')
        dropbacks = team.attempts+team.sacks_suffered
        add(team, 'nfl:dropback', dropbacks, dropbacks+team.carries)
        add(team, 'nfl:sack', team.sacks_suffered, dropbacks)
        add(team, 'nfl:target', team.targets, team.attempts)
        add(team, 'nfl:catch', team.receptions, team.targets)
        add(team, 'nfl:rec_ypc', team.receiving_yards, team.receptions)
        add(team, 'nfl:rush_ypc', team.rushing_yards, team.carries)
        add(team, 'nfl:int_rate', team.interceptions, team.attempts-team.completions)
        if 'position' in players and {'targets','receptions','receiving_yards','carries','rushing_yards'} <= set(players):
            p = players.copy()
            p['role'] = p.position.astype('string').str.upper().replace({'FB':'RB','HB':'RB'})
            p = p[p.role.isin(['WR','TE','RB','QB'])]
            columns = ['targets','receptions','receiving_yards','carries','rushing_yards']
            # A partially observed cohort is not promoted to a complete positional
            # total; team-wide factors remain the explicit fallback.
            p['_complete'] = p[columns].notna().all(axis=1)
            p = p[p.groupby(['game_id','team','role'])['_complete'].transform('all')]
            grouped = p.groupby(['game_id','team','role'], as_index=False).agg(
                **{c:(c,'sum') for c in columns}, player_available=('available_at','max'))
            totals = team[['game_id','team','offense','defense','event_date','available_at','targets','carries']].rename(
                columns={'targets':'team_targets','carries':'team_carries'})
            grouped = grouped.merge(totals, on=['game_id','team'], how='inner', validate='many_to_one')
            grouped['available_at'] = grouped[['available_at','player_available']].max(axis=1)
            for role, part in grouped.groupby('role'):
                add(part, 'nfl:catch:'+role, part.receptions, part.targets)
                add(part, 'nfl:rec_ypc:'+role, part.receiving_yards, part.receptions)
                add(part, 'nfl:rush_ypc:'+role, part.rushing_yards, part.carries)
                add(part, 'nfl:target_share:'+role, part.targets, part.team_targets)
                add(part, 'nfl:carry_share:'+role, part.carries, part.team_carries)
    else:
        raise ValueError('Opponent-adjusted matchup currently supports NBA and NFL only.')
    result = pd.concat(frames, ignore_index=True)
    return result.sort_values(['component','event_date','game_id','offense']).reset_index(drop=True)
