"""School-scoped CFBD history reused by Historical Data, models and markets.

Only explicitly requested schools, the current season and two prior seasons are
downloaded. CSVs include both sides of completed games, never future results.
"""
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
import unicodedata
import pandas as pd
from .data import DataSourceError,HttpClient,numeric_stats
from .nfl_model_data import atomic_text

DIRECTORY=Path(__file__).resolve().parents[1]/'data/modeling/ncaaf'
API='https://api.collegefootballdata.com'
LOCK=threading.RLock()
VERSION=1


def identity(value):
    text=unicodedata.normalize('NFKD',str(value)).encode('ascii','ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+',' ',text).strip()


def school_token(school):
    return re.sub(r'[^a-z0-9]+','_',school.lower()).strip('_')+'_'+sha256(identity(school).encode()).hexdigest()[:8]


def seasons_for_start(start):
    t=pd.Timestamp(start)
    season=t.year-int(t.month<3)
    return list(range(max(2001,season-2),season+1))


def aggregate_games(games,advanced,boxes=()):
    """A queried school's defense supplies the actual other side of each pair."""
    complete={str(g['id']):g for g in games if g.get('completed') is True}
    stats={}
    for entry in advanced:
        gid=str(entry['gameId'])
        for club,side in [(entry['team'],'offense'),(entry['opponent'],'defense')]:
            source=entry.get(side) or {}
            stats[(gid,club)]=dict(plays=source.get('plays'),drives=source.get('drives'),
                                  ppa=source.get('ppa'),ppa_sum=source.get('totalPPA'))
    fallback={}
    for box in boxes:
        for club in box.get('teams',[]):
            values=numeric_stats({r['category']:r['stat'] for r in club.get('stats',[])})
            fallback[(str(box['id']),club['team'])]=values
    rows=[]
    for gid,g in complete.items():
        pair=[]
        for home in [True,False]:
            team,other=(g['homeTeam'],g['awayTeam']) if home else (g['awayTeam'],g['homeTeam'])
            source=stats.get((gid,team),{})
            box=fallback.get((gid,team),{})
            plays=source.get('plays')
            if plays is None and {'attempts','rushing_attempts'}<=set(box):
                # NCAA box rushing attempts already include sacks.
                plays=box['attempts']+box['rushing_attempts']
            points=g.get('homePoints' if home else 'awayPoints')
            allowed=g.get('awayPoints' if home else 'homePoints')
            if plays is None or points is None or allowed is None:
                break
            event=pd.Timestamp(g['startDate'])
            if event.tzinfo is None:raise DataSourceError('CFBD game start must include a timezone.')
            pair.append(dict(game_id=gid,season=int(g['season']),season_type=g.get('seasonType','regular'),
                week=g['week'],event_date=event.isoformat(),available_at=(event+pd.Timedelta(hours=8)).isoformat(),
                team=team,opponent=other,team_id=str(g.get('homeId' if home else 'awayId',team)),
                is_home=int(home),neutral=int(bool(g.get('neutralSite',False))),
                points=float(points),points_allowed=float(allowed),offensive_plays=float(plays),
                offensive_drives=source.get('drives'),off_ppa=source.get('ppa'),off_ppa_sum=source.get('ppa_sum'),
                volume_source='CFBD advanced plays' if source.get('plays') is not None else 'NCAA box attempts + rushing attempts'))
        if len(pair)!=2:continue
        for i,r in enumerate(pair):
            r.update(defensive_plays=pair[1-i]['offensive_plays'],defensive_drives=pair[1-i]['offensive_drives'],
                     def_ppa=pair[1-i]['off_ppa'],def_ppa_sum=pair[1-i]['off_ppa_sum'])
        rows.extend(pair)
    frame=pd.DataFrame(rows)
    if not frame.empty:
        frame=frame[(frame.offensive_plays>0)&(frame.defensive_plays>0)]
        counts=frame.groupby('game_id').size()
        frame=frame[frame.game_id.isin(counts[counts.eq(2)].index)]
    return frame


def college_player_team_rows(games,boxes):
    """Map explicit CFBD team totals; an absent statistic stays unavailable."""
    complete={str(g['id']):g for g in games if g.get('completed') is True}
    fields={'attempts':'attempts','completions':'completions','net_passing_yards':'passing_yards',
            'rushing_attempts':'carries','rushing_yards':'rushing_yards','interceptions':'interceptions'}
    rows=[]
    for box in boxes:
        game=complete.get(str(box['id']))
        if game is None:continue
        for side in box.get('teams',[]):
            team=side['team']
            if team not in {game['homeTeam'],game['awayTeam']}:continue
            home=team==game['homeTeam']
            values=numeric_stats({r['category']:r['stat'] for r in side.get('stats',[])})
            event=pd.Timestamp(game['startDate'])
            if event.tzinfo is None:raise DataSourceError('CFBD game start must include a timezone.')
            row=dict(game_id=str(box['id']),entity_id=str(side['teamId']),entity_name=team,team=team,
                opponent=game['awayTeam'] if home else game['homeTeam'],season=game['season'],
                event_date=event.isoformat(),available_at=(event+pd.Timedelta(hours=8)).isoformat(),
                points=game.get('homePoints' if home else 'awayPoints'))
            row.update({target:values.get(source) for source,target in fields.items()})
            rows.append(row)
    return pd.DataFrame(rows)


class NcaafModelData:
    def __init__(self,directory=None,client=None):
        self.directory=Path(directory or DIRECTORY)
        self.client=client or HttpClient()

    def _request(self,endpoint,params):
        key=os.getenv('CFBD_API_KEY','').strip()
        if not key:raise DataSourceError('Set CFBD_API_KEY in .env to prepare college history.')
        result=self.client.json(API+endpoint,params=params,headers={'Authorization':'Bearer '+key})
        if not isinstance(result,list):raise DataSourceError('CFBD returned an unexpected history schema.')
        return result

    def resolve_school(self,display):
        path=self.directory/'schools.json'
        if path.exists():
            teams=json.loads(path.read_text(encoding='utf-8'))
        else:
            teams=self._request('/teams/fbs',{})
            atomic_text(path,json.dumps(teams))
        matches=set()
        for t in teams:
            school=t['school']
            aliases=[school,f"{school} {t.get('mascot') or ''}",t.get('abbreviation'),
                     t.get('altName1'),t.get('altName2'),t.get('altName3')]
            if identity(display) in {identity(a) for a in aliases if a}:matches.add(school)
        if len(matches)!=1:raise DataSourceError(f'Could not uniquely map {display} to a CFBD school.')
        return next(iter(matches))

    def _raw(self,name,endpoint,params,refresh):
        path=self.directory/'raw'/name
        if path.exists() and not refresh:return json.loads(path.read_text(encoding='utf-8'))
        rows=self._request(endpoint,params)
        atomic_text(path,json.dumps(rows,allow_nan=False))
        return rows

    def team_path(self,year,school):return self.directory/f'team_games_{year}_{school_token(school)}.csv'
    def player_path(self,year,school):return self.directory/f'player_games_{year}_{school_token(school)}.csv'

    def prepare_player_inputs(self,teams,start,refresh=False):
        """Official team denominators for props; never infer targets or sacks.

        Separate CSVs upgrade existing game-model caches without redownloading
        advanced history. Each selected school's boxes contain both opponents.
        """
        with LOCK:
            for display in teams:
                school=self.resolve_school(display)
                for year in seasons_for_start(start):
                    token=f'{year}_{school_token(school)}'
                    path=self.directory/f'player_team_games_{token}.csv'
                    if path.exists() and not refresh:continue
                    params={'year':year,'team':school,'seasonType':'both'}
                    games=self._raw('games_'+token+'.json','/games',params,refresh)
                    boxes=self._raw('boxes_'+token+'.json','/games/teams',params,refresh)
                    frame=college_player_team_rows(games,boxes)
                    if not frame.empty:atomic_text(path,frame.to_csv(index=False))

    def player_team_frames(self,seasons=None):
        paths=[p for p in self.directory.glob('player_team_games_*.csv')
               if not seasons or any(p.name.startswith(f'player_team_games_{year}_') for year in seasons)]
        if not paths:raise DataSourceError('No saved college player/team denominators. Prepare the selected schools first.')
        rows=pd.concat([pd.read_csv(p,dtype={'game_id':str,'entity_id':str})
                        for p in sorted(paths,key=lambda p:p.stat().st_mtime_ns)],ignore_index=True)
        return rows.drop_duplicates(['game_id','team'],keep='last').reset_index(drop=True)

    def prepare_team(self,display,start,refresh=False,players=True):
        with LOCK:
            school=self.resolve_school(display)
            total=0;notices=[]
            for year in seasons_for_start(start):
                token=f'{year}_{school_token(school)}'
                destination=self.team_path(year,school)
                player_path=self.player_path(year,school)
                if destination.exists() and (player_path.exists() or not players) and not refresh:
                    continue
                params={'year':year,'team':school,'seasonType':'both'}
                try:
                    games=self._raw('games_'+token+'.json','/games',params,refresh)
                    if not destination.exists() or refresh:
                        advanced=[];boxes=[]
                        try:
                            advanced=self._raw('advanced_'+token+'.json','/stats/game/advanced',
                                               {**params,'excludeGarbageTime':'false'},refresh)
                        except DataSourceError as exc:
                            notices.append(f'{school} {year}: advanced/PPA unavailable. {exc}')
                        if not advanced:
                            boxes=self._raw('boxes_'+token+'.json','/games/teams',params,refresh)
                        frame=aggregate_games(games,advanced,boxes)
                        if not frame.empty:
                            atomic_text(destination,frame.to_csv(index=False));total+=len(frame)
                            atomic_text(destination.with_suffix('.manifest.json'),json.dumps(dict(version=VERSION,school=school,
                                season=year,fetched_at=datetime.now(timezone.utc).isoformat(),
                                games=int(frame.game_id.nunique()),source='CollegeFootballData scoped games / advanced / boxes',
                                availability='Game start + 8 hours proxy; not an archived publication timestamp'),indent=2))
                    if players and (not player_path.exists() or refresh):
                        from models.Player_Stats.Weighted_Historical_Performance.historical_data import cfbd_player_rows,model_frame
                        boxes=self._raw('players_'+token+'.json','/games/players',params,refresh)
                        frame=cfbd_player_rows(games,boxes,school)
                        if not frame.empty:
                            frame['season']=year
                            frame=model_frame(frame)
                            atomic_text(player_path,frame.to_csv(index=False))
                except DataSourceError as exc:
                    notices.append(f'{school} {year}: {exc}')
            if refresh and any((self.directory/f'player_team_games_{year}_{school_token(school)}.csv').exists()
                               for year in seasons_for_start(start)):
                try:self.prepare_player_inputs([school],start,refresh=True)
                except DataSourceError as exc:notices.append(f'Player team totals refresh unavailable: {exc}')
            saved=self.team_frames(schools=[school])
            if saved.empty:raise DataSourceError('No college modeling history prepared. '+' '.join(notices))
            return f'{school}: saved scoped team/player CSV history under data/modeling/ncaaf. '+' '.join(notices)

    def team_frames(self,seasons=None,schools=None):
        paths=[]
        for path in self.directory.glob('team_games_*.csv'):
            if seasons and not any(path.name.startswith(f'team_games_{year}_') for year in seasons):continue
            if schools and not any(path==self.team_path(int(path.name.split('_')[2]),s) for s in schools):continue
            paths.append(path)
        if not paths:return pd.DataFrame()
        # Mirrored observations can overlap when several schools have been selected.
        data=pd.concat([pd.read_csv(p,dtype={'game_id':str,'team_id':str}) for p in sorted(paths,key=lambda p:p.stat().st_mtime_ns)],ignore_index=True)
        return data.drop_duplicates(['game_id','team'],keep='last').sort_values(['event_date','game_id','team']).reset_index(drop=True)

    def prepare(self,teams,start,refresh=False):
        notices=[]
        for team in teams:
            try:notices.append(self.prepare_team(team,start,refresh=refresh))
            except DataSourceError as exc:notices.append(str(exc))
        frame=self.team_frames(seasons_for_start(start))
        if frame.empty:raise DataSourceError('Load a college school in Historical Data to prepare game/player histories. '+' '.join(notices))
        return frame,notices

    def prepare_players(self,snapshot,rows):
        """Fetch prior-school boxes only for exact IDs quoted in the active tab."""
        from models.Player_Stats.Weighted_Historical_Performance.historical_data import (
            PlayerHistoryRepository,identity_name,cfbd_player_rows,model_frame)
        with LOCK:
            data=PlayerHistoryRepository(college_directory=self.directory).load('NCAAF',snapshot['commence_time'],
                                               [snapshot['home_team'],snapshot['away_team']])
            cutoff=min(pd.Timestamp(snapshot['pulled_at']),pd.Timestamp(snapshot['commence_time']))
            data=data[data.event_date.lt(cutoff)&data.available_at.lt(cutoff)]
            years=seasons_for_start(snapshot['commence_time'])
            for subject in sorted({r['subject'] for r in rows if r['market_key'].startswith('player_')}):
                found=data[data.entity_name.map(identity_name).eq(identity_name(subject))]
                if found.entity_id.nunique()!=1:continue
                latest=found.sort_values('event_date').iloc[-1]
                player_id=str(latest.entity_id)
                token=re.sub(r'[^0-9a-zA-Z_-]','',player_id)
                if not token:continue
                marker=self.directory/'transfers'/f'{token}_{years[-1]}.manifest.json'
                if marker.exists():continue
                try:
                    matches=self._raw(f'player_search_{token}.json','/player/search',{'searchTerm':str(latest.entity_name)},False)
                    match=next((m for m in matches if str(m.get('id'))==player_id),None)
                    parts=[]
                    for stint in (match or {}).get('teamStints',[]):
                        school=stint['team']
                        if identity(school)==identity(latest.team):continue
                        for year in years[:-1]:
                            if not int(stint.get('startYear') or year)<=year<=int(stint.get('endYear') or years[-1]):continue
                            params={'year':year,'team':school,'seasonType':'both'}
                            source=f'{year}_{school_token(school)}'
                            path=self.player_path(year,school)
                            if path.exists():frame=pd.read_csv(path,dtype={'game_id':str,'entity_id':str})
                            else:
                                games=self._raw('games_'+source+'.json','/games',params,False)
                                boxes=self._raw('players_'+source+'.json','/games/players',params,False)
                                frame=cfbd_player_rows(games,boxes,school)
                                if not frame.empty:
                                    frame['season']=year;frame=model_frame(frame)
                                    atomic_text(path,frame.to_csv(index=False))
                            if not frame.empty:
                                personal=frame[frame.entity_id.astype(str).eq(player_id)]
                                if not personal.empty:parts.append(personal)
                    if parts:
                        personal=pd.concat(parts,ignore_index=True).drop_duplicates(['game_id','entity_id'])
                        atomic_text(self.directory/'transfers'/f'{token}_{years[-1]}.csv',personal.to_csv(index=False))
                    atomic_text(marker,json.dumps(dict(player_id=player_id,lookup_season=years[-1],
                        rows=sum(len(p) for p in parts),source='Exact CFBD player ID / teamStints; scoped prior-school boxes')))
                except (DataSourceError,ValueError,KeyError):
                    # Keep current-school history and explicit sample-size errors.
                    continue


def main():
    import argparse
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1]/'.env')
    parser=argparse.ArgumentParser(description='Prepare only selected CFBD schools and their three-season CSV history; no live odds.')
    parser.add_argument('--teams',nargs='+',required=True)
    parser.add_argument('--start',default=datetime.now(timezone.utc).isoformat(),help='Matchup date/time determining the lookup seasons')
    parser.add_argument('--refresh',action='store_true')
    args=parser.parse_args()
    try:
        frame,notices=NcaafModelData().prepare(args.teams,args.start,refresh=args.refresh)
        for notice in notices:print(notice)
        print(f'{frame.game_id.nunique()} unique games / {len(frame)} paired team rows available.')
    except (DataSourceError,ValueError,OSError) as exc:
        parser.exit(1,str(exc)+'\n')


if __name__=='__main__':main()
