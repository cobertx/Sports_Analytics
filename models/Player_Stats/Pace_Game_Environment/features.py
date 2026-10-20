"""Paired, regulation-equivalent team volume with observed game scripts."""
import numpy as np
import pandas as pd


def volume_rows(sport, teams):
    output = []
    for game_id, game in teams.groupby('game_id', sort=False):
        if len(game) != 2 or game.team.nunique() != 2:
            continue
        a, b = [r for _, r in game.iterrows()]
        if ('opponent' not in game or a.opponent != b.team or b.opponent != a.team
                or a.event_date != b.event_date):
            continue
        if sport == 'NBA':
            needed = ['fga', 'fta', 'oreb', 'tov', 'min']
            if not set(needed) <= set(game) or not np.isfinite(game[needed].to_numpy(float)).all():
                continue
            duration = (float(a['min'])+float(b['min']))/10
            if duration < 47.9 or abs(float(a['min'])-float(b['min'])) > 1:
                continue
            if not np.isclose((duration-48)/5, round((duration-48)/5), atol=.03):
                continue
            possessions = np.mean([r.fga+.44*r.fta-r.oreb+r.tov for r in [a,b]])
            volumes = [possessions*48/duration]*2
            extra = [duration-48]*2
            mode = 'estimated possessions / actual minutes'
        else:
            pbp = 'pge_reg_plays' in game and game.pge_reg_plays.notna().all()
            if pbp:
                volumes = [float(r.pge_reg_plays) for r in [a,b]]
                extra = [float(r.pge_ot_plays) for r in [a,b]]
                mode = 'saved nflverse play-by-play'
            else:
                if not {'attempts','carries','sacks_suffered'} <= set(game):
                    continue
                volumes = [float(r.attempts+r.carries+r.sacks_suffered) for r in [a,b]]
                extra = [0., 0.]
                mode = 'box-score plays proxy; overtime unseparated'
            duration = 60.
        scores = [float(r.get('pts', r.get('pf', np.nan))) for r in [a,b]]
        # A supplied margin is preferable to inferring a missing score.
        margins = [float(r.get('plus_minus',np.nan)) for r in [a,b]]
        margins = [value if np.isfinite(value) else scores[i]-scores[1-i] for i,value in enumerate(margins)]
        if not np.isfinite([*volumes, *extra, *margins]).all() or min(volumes) <= 0:
            continue
        if not np.isclose(margins[0], -margins[1], atol=.1):
            continue
        for i,r in enumerate([a,b]):
            output.append(dict(game_id=str(game_id),team=r.team,opponent=[b,a][i].team,
                event_date=r.event_date,available_at=max(a.available_at,b.available_at),
                volume=volumes[i],allowed_volume=volumes[1-i],extra=extra[i],duration=duration,
                margin=margins[i],source=mode))
    result = pd.DataFrame(output)
    if result.empty:
        raise ValueError('PGE needs paired team boxes with opponents, scoring margins and valid volume. Prepare both seasons.')
    return result.sort_values(['event_date','game_id','team']).reset_index(drop=True)
