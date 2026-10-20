"""Chronological, local-data-only replay of the actual forecasting models.

No source preparation, sportsbook request or synthetic market price is used.
Evaluation targets are final outcomes; they are never passed into a forecast.
"""
from dataclasses import asdict
from datetime import datetime,timezone
from hashlib import sha256
from importlib import import_module
import json,math,threading
from pathlib import Path
import numpy as np
import pandas as pd

from .data import DataSourceError
from .service import DataService
from .ncaaf_model_data import NcaafModelData
from .display_time import EASTERN
from models.Player_Stats.Weighted_Historical_Performance.historical_data import model_frame,atomic_json
from models.Player_Stats.Weighted_Historical_Performance.markets import NBA,FOOTBALL,stat_spec
from models.Player_Stats.Usage_Opportunity.markets import market_spec

ROOT=Path(__file__).resolve().parents[1]


def evaluation_bounds(start,end,now):
    def parsed(value):
        result=pd.Timestamp(value)
        if pd.isna(result):raise ValueError('Choose valid evaluation dates.')
        return (result.tz_localize(EASTERN) if result.tzinfo is None else result).tz_convert('UTC')
    lower=parsed(start) if start else pd.Timestamp('1900-01-01',tz='UTC')
    upper=parsed(end)+pd.Timedelta(days=1) if end else now
    if lower>=upper:raise ValueError('Evaluation start must be on or before evaluation end.')
    return lower,upper


def game_model(sport,key):
    source={('NBA','ppp'):('PPP.NBA_PPP','NBAPPPModel'),('NFL','ppp'):('PPP.PPP','PPPModel'),
        ('NCAAF','ppp'):('PPP.NCAAF_PPP','NCAAFPPPModel'),('NBA','epa'):('EPA.NBA_EPA','NBAEPAModel'),
        ('NFL','epa'):('EPA.EPA','EPAModel'),('NCAAF','epa'):('EPA.NCAAF_EPA','NCAAFEPAModel'),
        ('NBA','elo'):('ELO.ELO','ELOModel'),('NFL','elo'):('ELO.ELO','ELOModel'),
        ('NCAAF','elo'):('ELO.NCAAF_ELO','NCAAFELOModel')}
    module,name=source[sport,key];module=import_module('models.Game_Outcome.'+module)
    return getattr(module,name)(module.ELOConfig.nba()) if sport=='NBA' and key=='elo' else getattr(module,name)()


def diagnostics(records,tolerance):
    """One point forecast per evaluated game/target, not per alternate line."""
    if not records:return {}
    error=np.array([r['predicted']-r['actual'] for r in records],float)
    result=dict(samples=len(error),mae=float(np.abs(error).mean()),rmse=float(np.sqrt(np.mean(error**2))),
        bias=float(error.mean()),error_variance=float(error.var(ddof=1)) if len(error)>1 else None,
        error_sd=float(error.std(ddof=1)) if len(error)>1 else None,
        within_tolerance=float(np.mean(np.abs(error)<=tolerance)),tolerance=tolerance)
    baseline=[abs(r['baseline']-r['actual']) for r in records if r.get('baseline') is not None]
    result['baseline_mae']=float(np.mean(baseline)) if baseline else None
    covered=[r['lower']<=r['actual']<=r['upper'] for r in records if r.get('lower') is not None and r.get('upper') is not None]
    result['coverage_90']=float(np.mean(covered)) if covered else None
    settled=[r for r in records if r.get('probability') is not None and r.get('event_actual') is not None]
    result['probability_samples']=len(settled)
    result['pushes']=sum(r.get('push',False) for r in records)
    if settled:
        p=np.array([r['probability'] for r in settled]);y=np.array([r['event_actual'] for r in settled])
        result.update(accuracy=float(np.mean((p>=.5)==y)),brier=float(np.mean((p-y)**2)),
            log_loss=float(-np.mean(y*np.log(p.clip(1e-12,1-1e-12))+(1-y)*np.log1p(-p.clip(1e-12,1-1e-12)))),
            reliability=[dict(predicted=float(p[mask].mean()),observed=float(y[mask].mean()),n=int(mask.sum()),
                              lower=i/10,upper=(i+1)/10) for i in range(10)
                         for mask in [(p>=i/10)&(p<((i+1)/10) if i<9 else p<=1)] if mask.any()])
    return result


def player_quantile(forecast,q):
    if hasattr(forecast,'quantile'):return float(forecast.quantile(q))
    # Invert the model's actual integer CDF, including count/empirical WHP.
    sd=math.sqrt(forecast.variance)
    lo=int(math.floor(forecast.mean-12*sd-20));hi=int(math.ceil(forecast.mean+12*sd+20))
    if forecast.nonnegative:lo=0
    while lo<hi:
        mid=(lo+hi)//2;price=forecast.market(mid,'Under')
        if price.win+price.push>=q:hi=mid
        else:lo=mid+1
    return float(lo)


class BacktestingService:
    def __init__(self,root=None,data=None,now=None):
        self.root=Path(root or ROOT/'data/backtesting')
        self.data=data or DataService()
        self.now=now or (lambda:pd.Timestamp.now(tz='UTC'))
        self._lock=threading.RLock()

    def game_inputs(self,sport,key):
        directory=ROOT/'data/modeling'/sport.lower()
        dataset=None
        if sport=='NBA' and key=='epa':directory=directory/'epa'
        paths=sorted(directory.glob('team_games_*.csv'),key=lambda p:p.stat().st_mtime_ns)
        if not paths:raise DataSourceError(f'No saved {sport} {key.upper()} team history. Load historical data or prepare this model’s inputs first.')
        frame=pd.concat([pd.read_csv(p,dtype={'game_id':str}) for p in paths],ignore_index=True)
        frame=frame.drop_duplicates(['game_id','team'],keep='last')
        for c in ['event_date','available_at']:frame[c]=pd.to_datetime(frame[c],utc=True,errors='coerce',format='mixed')
        if sport=='NBA' and key=='epa':
            from models.Game_Outcome.EPA.nba_epa_training import NBAEPADataset
            ids=[];xx=[];xy=[]
            for path in sorted(directory.glob('state_statistics_*.npz')):
                with np.load(path,allow_pickle=False) as state:
                    ids.extend(state['game_ids'].astype(str).tolist());xx.extend(state['xx']);xy.extend(state['xy'])
            if not ids or len(ids)!=len(set(ids)):raise DataSourceError('NBA EPA needs unique saved possession state statistics. Prepare NBA EPA inputs.')
            dataset=NBAEPADataset(frame,ids,np.asarray(xx),np.asarray(xy),{})
        return frame,dataset

    def player_inputs(self,sport):
        frames=[]
        if sport=='NCAAF':
            for path in sorted((ROOT/'data/modeling/ncaaf').glob('player_games_*.csv'),key=lambda p:p.stat().st_mtime_ns):
                frames.append(pd.read_csv(path,dtype={'entity_id':str,'game_id':str}))
        else:
            with self.data.connect() as db:
                saved=db.execute('SELECT key, query FROM datasets').fetchall()
            for key,raw in saved:
                query=json.loads(raw)
                if query.get('sport')==sport and query.get('entity_type')=='player':
                    cached=self.data.cached(key)
                    if cached is not None:frames.append(cached.frame)
        imported=ROOT/'data/modeling/player_stats/whp/imports'/f'{sport.lower()}.csv'
        if imported.exists():frames.append(pd.read_csv(imported,dtype={'entity_id':str,'game_id':str}))
        if not frames:raise DataSourceError(f'No saved {sport} player history. Load a player in Historical Data or prepare player inputs first.')
        frame=pd.concat(frames,ignore_index=True)
        # Recover dates from the saved NFL schedule, as the forecast repository does.
        schedule=ROOT/'data/modeling/nfl/raw/games.csv'
        if sport=='NFL' and schedule.exists():
            dates=pd.read_csv(schedule,dtype={'game_id':str},usecols=['game_id','gameday']).drop_duplicates('game_id').set_index('game_id').gameday
            frame['event_date']=pd.to_datetime(frame.event_date,utc=True,errors='coerce',format='mixed').fillna(
                pd.to_datetime(frame.game_id.astype(str).map(dates),utc=True,errors='coerce'))
        frame=model_frame(frame)
        frame['entity_id']=frame.entity_id.astype(str);frame['game_id']=frame.game_id.astype(str)
        frame=frame.drop_duplicates(['entity_id','game_id'],keep='last')
        return frame[frame.available_at.lt(self.now())&frame.event_date.lt(self.now())].sort_values(['event_date','game_id'])

    def options(self,sport,group,key,player=None):
        if group=='game-outcome':
            frame,_=self.game_inputs(sport,key)
            return [dict(label='All teams',value='__all__'),*[dict(label=t,value=t) for t in sorted(frame.team.unique())]],[],None
        frame=self.player_inputs(sport)
        latest=frame.drop_duplicates('entity_id',keep='last').sort_values('entity_name')
        options=[dict(label=f'{r.entity_name} · {r.team}',value=r.entity_id) for r in latest.itertuples()]
        selected=frame[frame.entity_id.eq(str(player or (options[0]['value'] if options else '')))]
        markets=[]
        for market,spec in (NBA if sport=='NBA' else FOOTBALL).items():
            if key!='whp' and market_spec(sport,market) is None:continue
            if set(spec.fields)<=set(selected) and selected[list(spec.fields)].notna().all(axis=1).any():
                name=market.removeprefix('player_').replace('_',' ').title()
                markets.append(dict(label=name,value=market))
        return options,markets,frame

    def player_service(self,key):
        module,name={'uo':('Usage_Opportunity','UsageOpportunityMarketService'),
                     'oam':('Opponent_Adjusted_Matchup','OpponentAdjustedMarketService'),
                     'pge':('Pace_Game_Environment','PaceGameEnvironmentMarketService')}[key]
        service=getattr(import_module('models.Player_Stats.'+module+'.prediction_service'),name)()
        service.repository.root=self.root/'model_cache'/key
        return service

    def latest(self,sport,group,key):
        directory=self.root/sport.lower()/group/key
        paths=sorted(directory.glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
        marker=directory/'last_run.json'
        if marker.exists():
            try:
                name=json.loads(marker.read_text(encoding='utf-8'))['report']
                if len(name)==69 and name.endswith('.json') and all(c in '0123456789abcdef' for c in name[:-5]):
                    paths.insert(0,directory/name)
            except (ValueError,KeyError,OSError,TypeError):pass
        for path in paths:
            try:
                result=json.loads(path.read_text(encoding='utf-8'))['result']
                if all(result['context'][name]==value for name,value in [('sport',sport),('group',group),('model',key)]):
                    return result
            except (ValueError,KeyError,OSError):continue
        return None

    def run(self,sport,group,key,entity='__all__',market=None,start=None,end=None,limit=20,tolerance=5,line=None):
        if sport not in {'NBA','NFL','NCAAF'} or group not in {'game-outcome','player-stats'}:raise ValueError('Choose a valid sport and model.')
        if not isinstance(limit,int) or not 1<=limit<=100:raise ValueError('Choose between 1 and 100 evaluation games.')
        if not isinstance(tolerance,(int,float)) or not math.isfinite(tolerance) or tolerance<0:raise ValueError('Tolerance must be a finite nonnegative number.')
        if line is not None and (not isinstance(line,(int,float)) or not math.isfinite(line)):raise ValueError('Reference line must be a finite number.')
        lower,upper=evaluation_bounds(start,end,self.now())
        with self._lock:
            if group=='game-outcome':
                model=game_model(sport,key);frame,dataset=self.game_inputs(sport,key)
                # A complete pair must be known today, including both result lags.
                candidates=frame[frame.event_date.ge(lower)&frame.event_date.lt(upper)&frame.available_at.lt(self.now())]
                if entity!='__all__':
                    candidates=candidates[candidates.game_id.isin(candidates[candidates.team.eq(entity)].game_id)]
                games=[g for _,g in candidates.groupby('game_id') if len(g)==2 and g.is_home.eq(1).sum()==1 and g.is_home.eq(0).sum()==1]
                games=sorted(games,key=lambda g:(g.event_date.min(),str(g.iloc[0].game_id)))[-limit:]
                config=asdict(model.config)
            else:
                if key not in {'whp','uo','oam','pge'}:raise ValueError('Choose an available player model.')
                frame=self.player_inputs(sport);dataset=None
                spec=stat_spec(sport,market or '')
                if spec is None or (key!='whp' and market_spec(sport,market or '') is None):raise ValueError('Choose a supported player statistic.')
                selected=frame[frame.entity_id.eq(str(entity))].copy()
                if not set(spec.fields)<=set(selected):raise DataSourceError('The selected statistic is missing from saved player data.')
                selected['value']=selected[list(spec.fields)].apply(pd.to_numeric,errors='coerce').sum(axis=1,min_count=len(spec.fields))
                if spec.opportunity and spec.opportunity in selected:selected['opportunity']=selected[spec.opportunity]
                games=[g for _,g in selected[selected.value.notna()&selected.event_date.ge(lower)&selected.event_date.lt(upper)].tail(limit).iterrows()]
                from .backtesting_view import find_model,model_config
                config=model_config(find_model(sport,(group,key)),sport)
            if not games:raise DataSourceError('No completed saved games match these filters.')
            signature=sha256(b''.join(p.read_bytes() for p in sorted([Path(__file__),*ROOT.joinpath('models').rglob('*.py'),
                ROOT/'sports_analytics/ncaaf_model_data.py']))).hexdigest()
            # Include all saved source revisions used by repositories, not only target rows.
            sources=[p for folder in ['data/cache','data/modeling'] for p in (ROOT/folder).rglob('*')
                     if p.is_file() and p.suffix in {'.csv','.sqlite3','.npz'} and 'forecasts' not in p.parts]
            source_state=[(str(p.relative_to(ROOT)),p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(sources)]
            identity=dict(sport=sport,group=group,model=key,entity=entity,market=market,start=start,end=end,limit=limit,
                          tolerance=tolerance,line=line,config=config,implementation=signature,
                          inputs=sha256((frame.to_json(orient='table',date_format='iso',index=False)+json.dumps(source_state)).encode()).hexdigest())
            identity['evaluation_games']=[str((g.iloc[0] if group=='game-outcome' else g).game_id) for g in games]
            cache=self.root/sport.lower()/group/key/(sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()+'.json')
            if cache.exists():
                saved=json.loads(cache.read_text(encoding='utf-8'))
                if saved.get('identity')==identity:
                    atomic_json(cache.parent/'last_run.json',dict(report=cache.name))
                    return saved['result']
            result=dict(context={k:identity[k] for k in ['sport','group','model','entity','market','start','end','limit','tolerance','line']},
                records=[],skipped=[],created_at=datetime.now(timezone.utc).isoformat(),config=config)
            result['context']['entity_label']=('All teams' if entity=='__all__' else str(entity)) if group=='game-outcome' else str(selected.iloc[-1].entity_name)
            if group=='game-outcome':self._games(result,model,frame,dataset,games)
            else:self._players(result,sport,key,frame,selected,games,spec,market,line)
            if not result['records']:
                reasons=list(dict.fromkeys(r['reason'] for r in result['skipped']))[:3]
                raise DataSourceError('No out-of-sample predictions could be evaluated. '+' '.join(reasons))
            result['attempted']=len(games)
            result['metrics']=diagnostics(result['records'],tolerance)
            atomic_json(cache,dict(identity=identity,result=result))
            atomic_json(cache.parent/'last_run.json',dict(report=cache.name))
            return result

    def _games(self,result,model,frame,dataset,games):
        for pair in games:
            h=pair[pair.is_home.eq(1)].iloc[0];a=pair[pair.is_home.eq(0)].iloc[0]
            try:
                cutoff=h.event_date
                # Only model inputs and schedule identities go into predict.
                forecast=model.predict(dataset if dataset is not None else frame,h.team,a.team,cutoff.isoformat(),bool(h.get('neutral',False)))
                margin=float(h.points-a.points);total=float(h.points+a.points)
                prior=frame[frame.event_date.lt(cutoff)&frame.available_at.lt(cutoff)]
                home=prior[prior.is_home.eq(1)]
                baseline_margin=float((home.points-home.points_allowed).mean()) if len(home) else None
                baseline_total=float((home.points+home.points_allowed).mean()) if len(home) else None
                prob=forecast.spread(forecast.home_team,0).probability
                common=dict(game_id=str(h.game_id),date=cutoff.isoformat(),label=f'{a.team} @ {h.team}',
                    probability=prob,event_actual=int(margin>0) if margin else None,push=margin==0,
                    actual_margin=margin,predicted_margin=forecast.margin_mean,
                    actual_total=total,predicted_total=forecast.total_mean,
                    margin_sd=forecast.margin_sd,total_sd=forecast.total_sd,
                    baseline_margin=baseline_margin,baseline_total=baseline_total,
                    latest_training=forecast.diagnostics.get('last_game_date'))
                common.update(actual=margin,predicted=forecast.margin_mean,baseline=baseline_margin,
                              lower=forecast.margin_mean-1.64485362695*forecast.margin_sd,
                              upper=forecast.margin_mean+1.64485362695*forecast.margin_sd)
                result['records'].append(common)
            except (ValueError,RuntimeError,OSError,np.linalg.LinAlgError) as exc:
                result['skipped'].append(dict(game_id=str(h.game_id),date=h.event_date.isoformat(),reason=str(exc)))

    def _players(self,result,sport,key,frame,selected,games,spec,market,line):
        from models.Player_Stats.Weighted_Historical_Performance.core import WHPConfig,WeightedHistoricalPerformance
        service=self.player_service(key) if key!='whp' else None
        team_games,_=self.game_inputs(sport,'ppp') if service else (None,None)
        if sport in {'NBA','NFL'}:
            module=import_module('.nba_markets' if sport=='NBA' else '.nfl_markets',package='sports_analytics')
            codes=getattr(module,sport+'_TEAM_CODES');names={v:k for k,v in codes.items()}
        else:names={}
        for game in games:
            try:
                cutoff=game.event_date
                past=selected[selected.event_date.lt(cutoff)&selected.available_at.lt(cutoff)&selected.value.notna()]
                if key=='whp':
                    fitted=WeightedHistoricalPerformance(WHPConfig.for_sport(sport)).forecast(past,cutoff,nonnegative=spec.nonnegative,count=spec.count)
                else:
                    pair=team_games[team_games.game_id.astype(str).eq(str(game.game_id))]
                    if len(pair)!=2:raise DataSourceError('No saved paired matchup teams for this player appearance.')
                    h=pair[pair.is_home.eq(1)].iloc[0];a=pair[pair.is_home.eq(0)].iloc[0]
                    row=dict(subject=game.entity_name,market_key=market,market=market,line=line if line is not None else 0,
                             selection='Over',odds=None,group='Player Stats',updated='Not reported')
                    snapshot=dict(sport=sport,game_id=str(game.game_id),home_team=names.get(h.team,h.team),
                        away_team=names.get(a.team,a.team),pulled_at=(cutoff-pd.Timedelta(microseconds=1)).isoformat(),
                        commence_time=cutoff.isoformat(),rows=[row])
                    forecast=service.forecast(snapshot);fitted=forecast.forecasts.get((game.entity_name,market))
                    if fitted is None:raise DataSourceError(forecast.errors.get((game.entity_name,market),'Required opportunity history is unavailable.'))
                price=fitted.market(line,'Over') if line is not None else None
                actual=float(game.value);mean=float(getattr(fitted,'mean'))
                result['records'].append(dict(game_id=str(game.game_id),date=cutoff.isoformat(),
                    label=f'{game.entity_name} · {game.team} vs {game.get("opponent","")}',actual=actual,predicted=mean,
                    lower=player_quantile(fitted,.05),upper=player_quantile(fitted,.95),
                    baseline=float(past.value.tail(5).mean()) if len(past) else None,
                    probability=price.probability if price else None,
                    event_actual=int(actual>line) if line is not None and actual!=line else None,
                    push=bool(line is not None and actual==line),latest_training=fitted.diagnostics.get('last_game_date')))
            except (ValueError,RuntimeError,OSError,IndexError,np.linalg.LinAlgError) as exc:
                result['skipped'].append(dict(game_id=str(game.game_id),date=game.event_date.isoformat(),reason=str(exc)))
