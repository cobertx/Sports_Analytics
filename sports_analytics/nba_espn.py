"""Published ESPN box scores for seasons not yet available from NBA Stats."""
import ast
from functools import lru_cache
from io import StringIO

import pandas as pd

from .data import DataSourceError, normalize
from .historical_links import normalized_name

ARCHIVE='https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
IDENTITIES='https://raw.githubusercontent.com/swar/nba_api/master/src/nba_api/stats/library/data.py'
ALIASES={'GS':'GSW','NY':'NYK','NO':'NOP','SA':'SAS','UTAH':'UTA','WSH':'WAS','PHO':'PHX','BRK':'BKN','CHO':'CHA'}
STATS={'minutes':'min','points':'pts','team_score':'pts','rebounds':'reb','total_rebounds':'reb',
       'assists':'ast','steals':'stl','blocks':'blk','turnovers':'tov','fouls':'pf','plus_minus':'plus_minus',
       'field_goals_made':'fgm','field_goals_attempted':'fga','three_point_field_goals_made':'fg3m',
       'three_point_field_goals_attempted':'fg3a','free_throws_made':'ftm','free_throws_attempted':'fta',
       'offensive_rebounds':'oreb','defensive_rebounds':'dreb'}


@lru_cache(maxsize=2)
def identities(text):
    # The registry is Python source, but only literal data is read; never execute it.
    tables={}
    for node in ast.parse(text).body:
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
            name=node.targets[0].id
            if name in {'players','teams'}:tables[name]=ast.literal_eval(node.value)
    exact={};normalized={}
    for row in tables['players']:
        if not isinstance(row[0],int) or not isinstance(row[3],str):raise ValueError('Invalid player identity')
        exact.setdefault(row[3].casefold(),set()).add(str(row[0]))
        normalized.setdefault(normalized_name(row[3]),set()).add(str(row[0]))
    teams={row[1]:str(row[0]) for row in tables['teams'] if isinstance(row[0],int)}
    return exact,normalized,teams


def load(query,client):
    kind=query.entity_type
    url=f'{ARCHIVE}espn_nba_{kind}_boxscores/{kind}_box_{query.season+1}.csv'
    try:
        raw=pd.read_csv(StringIO(client.get(url).text),dtype=str,low_memory=False)
        raw=raw[pd.to_numeric(raw['season'],errors='coerce').eq(query.season+1) &
                pd.to_numeric(raw['season_type'],errors='coerce').eq(2 if query.season_type=='regular' else 3)].copy()
        # Check both reported sides of a final result; live games and DNPs are not appearances.
        teams=raw[['game_id','team_id','team_winner','team_score','opponent_team_score']].drop_duplicates()
        scored=teams[['team_score','opponent_team_score']].apply(pd.to_numeric,errors='coerce')
        winner=teams.team_winner.str.lower().map({'true':True,'false':False})
        teams=teams.assign(valid=scored.notna().all(axis=1) & scored.ge(0).all(axis=1) &
                           scored.mod(1).eq(0).all(axis=1) & scored.team_score.ne(scored.opponent_team_score) &
                           winner.notna() & winner.eq(scored.team_score.gt(scored.opponent_team_score)),winner=winner)
        completed=teams.groupby('game_id').agg(sides=('team_id','nunique'),rows=('team_id','size'),
                                              valid=('valid','all'),winners=('winner','sum'))
        completed=completed[completed.sides.eq(2)&completed.rows.eq(2)&completed.valid&completed.winners.eq(1)].index
        raw=raw[raw.game_id.isin(completed)].copy()
        if kind=='player':
            raw=raw[raw.did_not_play.str.lower().eq('false') & pd.to_numeric(raw.minutes,errors='coerce').gt(0)].copy()
        if raw.empty:return normalize(raw,query)
        exact,canonical,team_ids={}, {}, {}
        try:exact,canonical,team_ids=identities(client.get(IDENTITIES).text)
        except (DataSourceError,KeyError,TypeError,ValueError,SyntaxError,IndexError):pass
        def player_id(row):
            found=exact.get(row['athlete_display_name'].casefold()) or canonical.get(normalized_name(row['athlete_display_name']),set())
            return next(iter(found)) if len(found)==1 else 'espn:'+row['athlete_id']
        team=raw.team_abbreviation.replace(ALIASES)
        opponent=raw.opponent_team_abbreviation.replace(ALIASES)
        home=raw['home_away' if kind=='player' else 'team_home_away']
        frame=pd.DataFrame(dict(game_id='espn:'+raw.game_id,event_date=raw.game_date_time,team=team,opponent=opponent,
            entity_id=raw.apply(player_id,axis=1) if kind=='player' else team.map(team_ids).fillna('espn:'+raw.team_id),
            entity_name=raw.athlete_display_name if kind=='player' else raw.team_display_name,
            position=raw.athlete_position_abbreviation if kind=='player' else None,
            matchup=(opponent+' @ '+team).where(home.eq('home'),team+' @ '+opponent)))
        for original,name in STATS.items():
            if original in raw and not (kind=='player' and original=='team_score'):
                frame[name]=pd.to_numeric(raw[original],errors='coerce')
        for made,attempted,percentage in [('fgm','fga','fg_pct'),('fg3m','fg3a','fg3_pct'),('ftm','fta','ft_pct')]:
            if made in frame and attempted in frame:frame[percentage]=frame[made].div(frame[attempted].where(frame[attempted].ne(0)))
        if kind=='team':frame['plus_minus']=frame.pts-pd.to_numeric(raw.opponent_team_score,errors='coerce')
        frame=normalize(frame,query)
        frame.attrs['source']='ESPN box scores via SportsDataverse'
        return frame
    except (KeyError,TypeError,ValueError) as exc:
        raise DataSourceError('The ESPN NBA archive returned an unexpected box-score schema.') from exc
