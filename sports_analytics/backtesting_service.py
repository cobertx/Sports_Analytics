"""Chronological, local-data-only replay of the actual forecasting models.

No source preparation, sportsbook request or synthetic market price is used.
Evaluation targets are final outcomes; they are never passed into a forecast.
"""
from dataclasses import asdict,replace
from datetime import datetime,timezone
from hashlib import sha256
from importlib import import_module
from functools import lru_cache
import json,math,threading,re,unicodedata
from os import walk
from statistics import NormalDist
from uuid import uuid4
from pathlib import Path
import numpy as np
import pandas as pd
from models.replay import replay_scope,fingerprint

from .data import DataSourceError
from .service import DataService
from .ncaaf_model_data import NcaafModelData
from .display_time import EASTERN
from .research_config import configured,player_config
from models.Player_Stats.Weighted_Historical_Performance.historical_data import model_frame,atomic_json
from models.Player_Stats.Weighted_Historical_Performance.markets import NBA,FOOTBALL,stat_spec
from models.Player_Stats.Usage_Opportunity.markets import market_spec

ROOT=Path(__file__).resolve().parents[1]
PREDICTIVE_INTERVAL=.8
PREDICTIVE_Z=NormalDist().inv_cdf((1+PREDICTIVE_INTERVAL)/2)
CALIBRATION_BINS=50


def file_revision(path):
    stamp=path.stat()
    return str(path.resolve()),stamp.st_size,stamp.st_mtime_ns,stamp.st_ctime_ns,stamp.st_ino


@lru_cache(maxsize=4)
def implementation_signature(revisions):
    return sha256(b''.join(Path(revision[0]).read_bytes() for revision in revisions)).hexdigest()


def source_revisions(root=ROOT):
    """Repository inputs only; never enumerate generated forecast/audit trees."""
    sources=[]
    for directory in [root/'data/cache',root/'data/modeling']:
        for folder,children,names in walk(directory):
            children[:]=[name for name in children if name not in {'forecasts','evaluations'}]
            sources.extend(Path(folder)/name for name in names if Path(name).suffix in {'.csv','.sqlite3','.npz','.json'})
    for path in list(sources):
        if path.suffix=='.sqlite3' and Path(str(path)+'-wal').exists():sources.append(Path(str(path)+'-wal'))
    revisions=[]
    for path in sorted(sources):
        try:revisions.append((str(path.relative_to(root)),*file_revision(path)[1:]))
        except FileNotFoundError:pass  # SQLite can checkpoint/remove its WAL between the scan and stat.
    return revisions


@lru_cache(maxsize=16)
def input_csv(revision,dtypes,columns):
    return pd.read_csv(revision[0],dtype=dict(dtypes),usecols=columns)


def read_input_csv(path,dtype,columns=None):
    # Cache parsing, never mutable caller frames. File edits invalidate immediately.
    return input_csv(file_revision(path),tuple(dtype.items()),tuple(columns) if columns else None).copy(deep=True)


def valid_report_page(sport,group,key):
    return (sport in {'NBA','NFL','NCAAF'} and group in {'game-outcome','player-stats'}
            and key in ({'ppp','epa','elo'} if group=='game-outcome' else {'whp','uo','oam','pge'}))


def read_report(path,sport,group,key):
    """Read full JSON archives and older cache wrappers without refitting."""
    try:
        payload=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload,dict):return None
        result=payload.get('result',payload)
        if not isinstance(result,dict) or not isinstance(result.get('context'),dict):return None
        if not all(result['context'].get(k)==v for k,v in [('sport',sport),('group',group),('model',key)]):return None
        if not isinstance(result.get('records'),list):return None
        # Early reports used only the cache filename as their persistent ID.
        run_id=result.get('run_id') or (path.stem if re.fullmatch(r'[0-9a-f]{64}',path.stem) else None)
        if not isinstance(run_id,str) or not re.fullmatch(r'[0-9a-f]{64}',run_id):return None
        return {**result,'run_id':run_id}
    except (ValueError,OSError):return None


@lru_cache(maxsize=256)
def report_summary(path,size,modified,sport,group,key):
    """Cache small summaries by file revision, never full prediction arrays."""
    result=read_report(Path(path),sport,group,key)
    if not result or not result['records']:return None
    try:
        created=pd.Timestamp(result['created_at'])
        if pd.isna(created):return None
        created=(created.tz_localize('UTC') if created.tzinfo is None else created).tz_convert('UTC')
        context=result['context']
        label=context.get('entity_label') or ('All teams' if context.get('entity')=='__all__' else context.get('entity','All teams'))
        if group=='player-stats' and not context.get('entity_label') and isinstance(result['records'][0].get('label'),str):
            label=result['records'][0]['label'].split(' · ')[0]
        return dict(run_id=result['run_id'],created_at=created.isoformat(),
            workspace=context.get('workspace','backtesting'),
            entity=context.get('entity'),team_filter=context.get('team_filter'),
            entity_label=label,
            market=context.get('market'),limit=context.get('limit') or len(result['records']),
            lookback=context.get('lookback'),include_playoffs=context.get('include_playoffs'),line=context.get('line'),
            start=context.get('start'),end=context.get('end'),evaluated=len(result['records']),
            skipped=len(result.get('skipped',[])),attempted=result.get('attempted',len(result['records'])+len(result.get('skipped',[]))))
    except (KeyError,TypeError,ValueError):return None


def default_evaluation_games(sport,entity='__all__'):
    return ({'NBA':1000,'NFL':250,'NCAAF':1000} if entity=='__all__' else
            {'NBA':82,'NFL':34,'NCAAF':24})[sport]


def default_lookback(sport,group,key):
    from .backtesting_view import find_model,model_config
    config=model_config(find_model(sport,(group,key)),sport)
    if group=='game-outcome':
        return 'All available (Model Default)','This model has no fixed game-count window; it uses eligible saved history with recency weighting.'
    if key=='whp':
        return f'{config["window"]} (Model Default)','Target window in appearances. Older eligible games also support model priors and calibration.'
    baseline=config.get('baseline',config)
    return (f'{baseline["role_window"]} / {baseline["efficiency_window"]} / {baseline["team_window"]} (Model Default)',
        'Default role / efficiency / team windows. League priors and calibration retain their model settings. Enter a whole number to cap all input histories.')


def parse_lookback(value,sport,group,key):
    if value is None or value=='':return None
    if isinstance(value,str):
        if value=='Model Default':return None
        if value==default_lookback(sport,group,key)[0]:return None
        if re.fullmatch(r'\d+',value.strip()):value=int(value.strip())
    if isinstance(value,bool) or not isinstance(value,int) or not 2<=value<=10000:
        raise ValueError('Training lookback must be 2–10,000 whole games, blank, or the displayed Model Default.')
    return value


def slug(value):
    value=unicodedata.normalize('NFKD',str(value)).encode('ascii','ignore').decode()
    return re.sub(r'[^a-z0-9]+','_',value.lower()).strip('_')[:70] or 'all'


def export_filename(result,target='margin'):
    c=result['context'];created=pd.Timestamp(result['created_at']).strftime('%Y%m%dT%H%M%SZ')
    statistic=target if c['group']=='game-outcome' else c['market']
    return (f'{c["sport"].lower()}_{c["model"]}_{slug(c.get("entity_label",c["entity"]))[:12]}_{slug(statistic)[:12]}'
            f'_backtest{c["limit"]}games_lookback{c.get("lookback") or "model_default"}'
            f'_{"post" if c.get("include_playoffs") else "reg"}'
            f'_{created}_{result.get("run_id","legacy")[:8]}.csv')


def evaluated_records(result,target='margin'):
    records=[dict(r) for r in result['records']];context=result['context']
    if context['group']=='game-outcome':
        for row in records:
            row.update(actual=row['actual_'+target],predicted=row['predicted_'+target],baseline=row['baseline_'+target],
                lower=row['predicted_'+target]-PREDICTIVE_Z*row[target+'_sd'],
                upper=row['predicted_'+target]+PREDICTIVE_Z*row[target+'_sd'],interval_level=PREDICTIVE_INTERVAL)
    else:
        for row in records:
            # Older saved reports did not retain the distribution/80% quantiles.
            row.setdefault('interval_level',context.get('interval_level',.9))
    return records


def eligible_pairs(frame,entity,lower,upper,now):
    candidates=frame[frame.event_date.ge(lower)&frame.event_date.lt(upper)&frame.available_at.lt(now)]
    if entity!='__all__':
        candidates=candidates[candidates.game_id.isin(candidates[candidates.team.eq(entity)].game_id)]
    games=[g for _,g in candidates.groupby('game_id') if len(g)==2 and g.is_home.eq(1).sum()==1 and g.is_home.eq(0).sum()==1]
    return sorted(games,key=lambda g:(g.event_date.min(),str(g.iloc[0].game_id)))


def season_phases(frame,sport,known=None,default=None):
    """Normalize verified source phases; unknown phases are never assumed regular."""
    result=frame.copy()
    aliases={'reg':'regular','regular':'regular','regular season':'regular',
             'post':'postseason','postseason':'postseason','playoffs':'postseason',
             'playoff':'postseason','playin':'postseason','play-in':'postseason',
             'wc':'postseason','div':'postseason','con':'postseason','sb':'postseason',
             'pre':'preseason','preseason':'preseason'}
    phase=pd.Series(index=result.index,dtype='object')
    for column in ['season_type','seasonType','game_type']:
        if column in result:phase=phase.fillna(result[column].astype(str).str.lower().str.strip().map(aliases))
    if known:phase=phase.fillna(result.game_id.astype(str).map(known))
    if default:phase=phase.fillna(default)
    if sport=='NBA':
        # NBA IDs: 002 regular season, 004 playoffs, 005 play-in.
        ids=result.game_id.astype(str).str.fullmatch(r'00[245]\d{7}')
        inferred=result.game_id.astype(str).str[:3].map({'002':'regular','004':'postseason','005':'postseason'})
        phase=phase.fillna(inferred.where(ids))
    result['season_type']=phase.fillna('unknown')
    return result


def estimation_history(frame,cutoff,lookback=None,*,entity='team',paired=False):
    """Strict pre-game availability, then at most N appearances per identity.

    Paired models keep a game only when both sides survive their own window.
    This can leave fewer than N appearances; it never imports an older opponent
    row just to complete a pair. Calibration uses this same bounded input pool.
    """
    result=frame[frame.event_date.lt(cutoff)&frame.available_at.lt(cutoff)].sort_values(['event_date','game_id'])
    if lookback is not None:result=result.groupby(entity,sort=False,dropna=False).tail(lookback)
    if paired:result=result[result.groupby('game_id').game_id.transform('size').eq(2)]
    return result.copy()


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
    result['interval_coverage']=float(np.mean(covered)) if covered else None
    level=records[0].get('interval_level',PREDICTIVE_INTERVAL)
    result[f'coverage_{round(100*level)}']=result['interval_coverage']
    settled=[r for r in records if r.get('probability') is not None and r.get('event_actual') is not None]
    result['probability_samples']=len(settled)
    result['pushes']=sum(r.get('push',False) for r in records)
    if settled:
        p=np.array([r['probability'] for r in settled]);y=np.array([r['event_actual'] for r in settled])
        result.update(accuracy=float(np.mean((p>=.5)==y)),brier=float(np.mean((p-y)**2)),
            log_loss=float(-np.mean(y*np.log(p.clip(1e-12,1-1e-12))+(1-y)*np.log1p(-p.clip(1e-12,1-1e-12)))),
            reliability=[dict(predicted=float(p[mask].mean()),observed=float(y[mask].mean()),n=int(mask.sum()),
                              lower=i/CALIBRATION_BINS,upper=(i+1)/CALIBRATION_BINS) for i in range(CALIBRATION_BINS)
                         for mask in [(p>=i/CALIBRATION_BINS)&(p<((i+1)/CALIBRATION_BINS) if i<CALIBRATION_BINS-1 else p<=1)] if mask.any()])
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
    def __init__(self,root=None,data=None,now=None,*,isolated=False,publish_latest=True):
        self.root=Path(root or ROOT/'data/backtesting')
        self.data=data or DataService()
        self.now=now or (lambda:pd.Timestamp.now(tz='UTC'))
        self._jobs={};self._jobs_lock=threading.RLock()
        self._publish_latest=publish_latest
        self._processes=None
        if isolated:
            from .backtest_process import BacktestProcesses
            self._processes=BacktestProcesses()

    def _remember_report(self,cache):
        if self._publish_latest:
            # Dashboard workers send completed reports to the parent; only the
            # parent writes this shared marker, including on Windows.
            with self._jobs_lock:atomic_json(cache.parent/'last_run.json',dict(report=cache.name))

    def start(self,*,display_label=None,**parameters):
        """Run independently of other jobs and the HTTP request thread."""
        signature=sha256(json.dumps(parameters,sort_keys=True).encode()).hexdigest()
        with self._jobs_lock:
            for job_id,job in self._jobs.items():
                if job['signature']==signature and job['state']=='running':return job_id
            # Retain a bounded number of completed jobs; reports live on disk.
            completed=[k for k,v in self._jobs.items() if v['state']!='running']
            for old in completed[:-31]:self._jobs.pop(old,None)
            job_id=uuid4().hex
            self._jobs[job_id]=dict(signature=signature,state='running',percent=0,completed=0,total=0,
                message='Waiting to start',parameters=parameters,
                display_label=display_label or ('All teams' if parameters.get('entity','__all__')=='__all__' else str(parameters['entity'])))
        def progress(done,total,message):
            with self._jobs_lock:
                self._jobs[job_id].update(completed=done,total=total,message=message,
                    percent=min(99,int(100*done/total)) if total else 0)
        def work():
            try:
                if self._processes is None:
                    result=self.run(**parameters,progress=progress)
                else:
                    result=self._processes.run(self.root,self.data,self.now(),parameters,progress)
                    context=result['context']
                    self._remember_report(self.root/context['sport'].lower()/context['group']/context['model']/(result['run_id']+'.json'))
                with self._jobs_lock:
                    job=self._jobs[job_id]
                    job.update(state='completed',percent=100,result=result,
                        reused=job['message']=='Loaded saved results',message='Completed')
            except Exception as exc:
                with self._jobs_lock:self._jobs[job_id].update(state='failed',message=str(exc))
        threading.Thread(target=work,name='backtest-'+job_id[:8],daemon=True).start()
        return job_id

    def progress(self,job_id):
        with self._jobs_lock:return dict(self._jobs[job_id]) if job_id in self._jobs else None

    def active_jobs(self):
        """Small independent snapshots for the global monitor, without reports."""
        with self._jobs_lock:
            return [dict(id=job_id,state=job['state'],percent=job['percent'],completed=job['completed'],
                total=job['total'],message=job['message'],display_label=job['display_label'],
                parameters=dict(job['parameters'])) for job_id,job in self._jobs.items() if job['state']=='running']

    def available_games(self,sport,group,key,entity,market=None,start=None,end=None,include_playoffs=False,team_filter=None):
        lower,upper=evaluation_bounds(start,end,self.now())
        frame=(self.game_inputs(sport,key)[0] if group=='game-outcome' else self.player_inputs(sport))
        frame=season_phases(frame,sport)
        frame=frame[frame.season_type.isin(['regular','postseason'] if include_playoffs else ['regular'])]
        if group=='game-outcome':return len(eligible_pairs(frame,entity,lower,upper,self.now()))
        spec=stat_spec(sport,market or '')
        if spec is None or not set(spec.fields)<=set(frame):return 0
        selected=frame if entity=='__all__' else frame[frame.entity_id.eq(str(entity))]
        if team_filter:selected=selected[selected.team.eq(team_filter)]
        complete=selected[list(spec.fields)].apply(pd.to_numeric,errors='coerce').notna().all(axis=1)
        eligible=selected[complete&selected.event_date.ge(lower)&selected.event_date.lt(upper)]
        return int(eligible.game_id.nunique()) if entity=='__all__' else len(eligible)

    def game_inputs(self,sport,key):
        directory=ROOT/'data/modeling'/sport.lower()
        dataset=None
        if sport=='NBA' and key=='epa':directory=directory/'epa'
        paths=sorted(directory.glob('team_games_*.csv'),key=lambda p:p.stat().st_mtime_ns)
        if not paths:raise DataSourceError(f'No saved {sport} {key.upper()} team history. Load historical data or prepare this model’s inputs first.')
        frames=[]
        for path in paths:
            phase=next((p for p in ['regular','postseason'] if path.stem.endswith('_'+p)),None)
            frames.append(season_phases(read_input_csv(path,{'game_id':str}),sport,default=phase))
        frame=pd.concat(frames,ignore_index=True)
        frame=season_phases(frame,sport,self.phase_map(sport))
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
                frames.append(read_input_csv(path,{'entity_id':str,'game_id':str}))
        else:
            with self.data.connect() as db:
                saved=db.execute('SELECT key, query FROM datasets').fetchall()
            for key,raw in saved:
                query=json.loads(raw)
                if query.get('sport')==sport and query.get('entity_type')=='player':
                    cached=self.data.cached(key)
                    if cached is not None:frames.append(season_phases(cached.frame,sport,default=query.get('season_type')))
        imported=ROOT/'data/modeling/player_stats/whp/imports'/f'{sport.lower()}.csv'
        if imported.exists():frames.append(read_input_csv(imported,{'entity_id':str,'game_id':str}))
        if not frames:raise DataSourceError(f'No saved {sport} player history. Load a player in Historical Data or prepare player inputs first.')
        frame=pd.concat(frames,ignore_index=True)
        # Recover dates from the saved NFL schedule, as the forecast repository does.
        schedule=ROOT/'data/modeling/nfl/raw/games.csv'
        if sport=='NFL' and schedule.exists():
            dates=read_input_csv(schedule,{'game_id':str},['game_id','gameday']).drop_duplicates('game_id').set_index('game_id').gameday
            frame['event_date']=pd.to_datetime(frame.event_date,utc=True,errors='coerce',format='mixed').fillna(
                pd.to_datetime(frame.game_id.astype(str).map(dates),utc=True,errors='coerce'))
        frame=model_frame(frame)
        frame=season_phases(frame,sport,self.phase_map(sport))
        frame['entity_id']=frame.entity_id.astype(str);frame['game_id']=frame.game_id.astype(str)
        frame=frame.drop_duplicates(['entity_id','game_id'],keep='last')
        return frame[frame.available_at.lt(self.now())&frame.event_date.lt(self.now())].sort_values(['event_date','game_id'])

    def phase_map(self,sport):
        """Recover phase metadata for older prepared files without downloading."""
        directory=ROOT/'data/modeling'/sport.lower()
        paths=list(directory.glob('team_games_*.csv'))
        if sport=='NFL' and (directory/'raw/games.csv').exists():paths.append(directory/'raw/games.csv')
        if sport=='NCAAF':paths.extend((directory/'raw').glob('games_*.json'))
        revision=tuple(file_revision(path) for path in paths)
        return dict(self._phase_map(sport,revision))

    @lru_cache(maxsize=8)
    def _phase_map(self,sport,revision):
        known={};directory=ROOT/'data/modeling'/sport.lower()
        for path in directory.glob('team_games_*.csv'):
            data=read_input_csv(path,{'game_id':str})
            phase=next((p for p in ['regular','postseason'] if path.stem.endswith('_'+p)),None)
            data=season_phases(data,sport,default=phase)
            known.update(data[data.season_type.ne('unknown')].set_index('game_id').season_type.to_dict())
        if sport=='NFL' and (directory/'raw/games.csv').exists():
            data=season_phases(read_input_csv(directory/'raw/games.csv',{'game_id':str}),sport)
            known.update(data[data.season_type.ne('unknown')].set_index('game_id').season_type.to_dict())
        if sport=='NCAAF':
            for path in (directory/'raw').glob('games_*.json'):
                raw=json.loads(path.read_text(encoding='utf-8'))
                if isinstance(raw,list):
                    for game in raw:
                        phase=game.get('seasonType')
                        if phase in {'regular','postseason'}:known[str(game['id'])]=phase
        return known

    def options(self,sport,group,key,player=None):
        if group=='game-outcome':
            frame,_=self.game_inputs(sport,key)
            return [dict(label='All teams',value='__all__'),*[dict(label=t,value=t) for t in sorted(frame.team.unique())]],[],None
        frame=self.player_inputs(sport)
        latest=frame.drop_duplicates('entity_id',keep='last').sort_values('entity_name')
        options=[dict(label=f'{r.entity_name} · {r.team}',value=r.entity_id) for r in latest.itertuples()]
        selected=(frame if player=='__all__' else frame[frame.entity_id.eq(str(player or (options[0]['value'] if options else '')))])
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

    def saved(self,sport,group,key,run_id):
        """Restore one page's report by its opaque ID, including after restart."""
        if (not valid_report_page(sport,group,key)
                or not isinstance(run_id,str) or not re.fullmatch(r'[0-9a-f]{64}',run_id)):
            return None
        directory=self.root/sport.lower()/group/key
        result=read_report(directory/(run_id+'.json'),sport,group,key)
        if result and result['run_id']==run_id:return result
        # The report archive is independently usable if its cache was removed.
        for path in (directory/'runs').glob('*/report.json'):
            try:
                stat=path.stat()
                summary=report_summary(str(path),stat.st_size,stat.st_mtime_ns,sport,group,key)
            except OSError:continue
            if summary and summary['run_id']==run_id:return read_report(path,sport,group,key)
        return None

    def history(self,sport,group,key):
        """Newest-first completed simulations for exactly one sport and method."""
        if not valid_report_page(sport,group,key):return []
        directory=self.root/sport.lower()/group/key
        paths=[p for p in sorted(directory.glob('*.json')) if re.fullmatch(r'[0-9a-f]{64}',p.stem)]
        paths.extend(sorted((directory/'runs').glob('*/report.json')))
        reports={}
        for path in paths:
            try:
                stat=path.stat()
                summary=report_summary(str(path),stat.st_size,stat.st_mtime_ns,sport,group,key)
            except OSError:continue
            if summary:reports.setdefault(summary['run_id'],dict(summary))
        return sorted(reports.values(),key=lambda r:(r['created_at'],r['run_id']),reverse=True)

    def ensure_exports(self,result):
        """Materialize CSVs and an independent report for current/legacy runs."""
        context=result['context'];sport,group,key=(context[k] for k in ['sport','group','model'])
        run_id=result.get('run_id')
        if not valid_report_page(sport,group,key) or not isinstance(run_id,str) or not re.fullmatch(r'[0-9a-f]{64}',run_id):
            raise ValueError('Choose a saved simulation with a valid report ID.')
        directory=self.root/sport.lower()/group/key/'runs'/(slug(context.get('entity_label',context.get('entity','all')))[:8]+'_'+run_id[:8])
        result['saved_directory']=str(directory.relative_to(self.root))
        from .nfl_model_data import atomic_text
        for target in (['margin','total'] if group=='game-outcome' else ['player']):
            path=directory/export_filename(result,target)
            if not path.exists():atomic_text(path,pd.DataFrame(evaluated_records(result,target)).to_csv(index=False))
        if not (directory/'report.json').exists():atomic_json(directory/'report.json',result)
        return directory

    def latest(self,sport,group,key):
        if not valid_report_page(sport,group,key):return None
        directory=self.root/sport.lower()/group/key
        paths=sorted(directory.glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
        marker=directory/'last_run.json'
        if marker.exists():
            try:
                with self._jobs_lock:name=json.loads(marker.read_text(encoding='utf-8'))['report']
                if len(name)==69 and name.endswith('.json') and all(c in '0123456789abcdef' for c in name[:-5]):
                    paths.insert(0,directory/name)
            except (ValueError,KeyError,OSError,TypeError):pass
        for path in paths:
            result=read_report(path,sport,group,key)
            if result:return result
        history=self.history(sport,group,key)
        if history:return self.saved(sport,group,key,history[0]['run_id'])
        return None

    def run(self,sport,group,key,entity='__all__',market=None,start=None,end=None,limit=None,tolerance=5,line=None,
            include_playoffs=False,lookback=None,progress=None,config_overrides=None,team_filter=None,workspace='backtesting'):
        if sport not in {'NBA','NFL','NCAAF'} or group not in {'game-outcome','player-stats'}:raise ValueError('Choose a valid sport and model.')
        if workspace not in {'backtesting','research'}:raise ValueError('Choose a valid backtesting workspace.')
        if team_filter is not None and not isinstance(team_filter,str):raise ValueError('Choose one saved team or all teams.')
        if group=='game-outcome':
            market=None;line=None
            if team_filter:entity=team_filter;team_filter=None
        if limit is None:limit=default_evaluation_games(sport,entity)
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=10000:raise ValueError('Choose 1 to 10,000 whole evaluation games.')
        lookback=parse_lookback(lookback,sport,group,key)
        if not isinstance(include_playoffs,bool):raise ValueError('Choose whether to include playoffs.')
        if not isinstance(tolerance,(int,float)) or not math.isfinite(tolerance) or tolerance<0:raise ValueError('Tolerance must be a finite nonnegative number.')
        if line is not None and (not isinstance(line,(int,float)) or not math.isfinite(line)):raise ValueError('Reference line must be a finite number.')
        lower,upper=evaluation_bounds(start,end,self.now())
        if progress:progress(0,0,'Preparing saved inputs')
        if group=='game-outcome':
            model=game_model(sport,key);frame,dataset=self.game_inputs(sport,key)
            model.config=configured(model.config,config_overrides)
            frame=season_phases(frame,sport)
            frame=frame[frame.season_type.isin(['regular','postseason'] if include_playoffs else ['regular'])]
            # A complete pair must be known today, including both result lags.
            games=eligible_pairs(frame,entity,lower,upper,self.now())[-limit:]
            config=asdict(model.config)
        else:
            if key not in {'whp','uo','oam','pge'}:raise ValueError('Choose an available player model.')
            frame=season_phases(self.player_inputs(sport),sport);dataset=None
            frame=frame[frame.season_type.isin(['regular','postseason'] if include_playoffs else ['regular'])]
            spec=stat_spec(sport,market or '')
            if spec is None or (key!='whp' and market_spec(sport,market or '') is None):raise ValueError('Choose a supported player statistic.')
            selected=(frame if entity=='__all__' else frame[frame.entity_id.eq(str(entity))]).copy()
            if not set(spec.fields)<=set(selected):raise DataSourceError('The selected statistic is missing from saved player data.')
            selected['value']=selected[list(spec.fields)].apply(pd.to_numeric,errors='coerce').sum(axis=1,min_count=len(spec.fields))
            if spec.opportunity and spec.opportunity in selected:selected['opportunity']=selected[spec.opportunity]
            candidates=selected[selected.value.notna()&selected.event_date.ge(lower)&selected.event_date.lt(upper)]
            if team_filter:candidates=candidates[candidates.team.eq(team_filter)]
            if entity=='__all__':
                candidates=candidates.sort_values(['event_date','game_id','entity_id'])
                ids=candidates.drop_duplicates('game_id',keep='last').tail(limit).game_id
                candidates=candidates[candidates.game_id.isin(ids)]
            else:candidates=candidates.tail(limit)
            games=[g for _,g in candidates.iterrows()]
            config=asdict(configured(player_config(sport,key),config_overrides))
        if not games:raise DataSourceError('No completed saved games with verified season type match these filters. Load more historical data or adjust the dates/playoff selection.')
        if progress:progress(0,len(games),'Checking saved runs')
        signature=implementation_signature(tuple(file_revision(p) for p in sorted([Path(__file__),*ROOT.joinpath('models').rglob('*.py'),
            ROOT/'sports_analytics/ncaaf_model_data.py'])))
        # Game models use only the complete frame hashed below. Player models
        # additionally read league/team histories from their repositories.
        source_state=source_revisions() if group=='player-stats' else []
        identity=dict(sport=sport,group=group,model=key,entity=entity,market=market,start=start,end=end,limit=limit,
                      tolerance=tolerance,line=line,include_playoffs=include_playoffs,lookback=lookback,
                      interval_level=PREDICTIVE_INTERVAL,config=config,implementation=signature,
                      inputs=sha256((repr(fingerprint(frame))+json.dumps(source_state)).encode()).hexdigest())
        if workspace=='research' or team_filter is not None:identity.update(workspace=workspace,team_filter=team_filter)
        identity['evaluation_games']=[str((g.iloc[0] if group=='game-outcome' else g).game_id) for g in games]
        cache=self.root/sport.lower()/group/key/(sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()+'.json')
        if cache.exists():
            saved=json.loads(cache.read_text(encoding='utf-8'))
            if saved.get('identity')==identity:
                result={**saved['result'],'run_id':cache.stem}
                self.ensure_exports(result)
                self._remember_report(cache)
                if progress:progress(len(games),len(games),'Loaded saved results')
                return result
        result=dict(context={k:identity[k] for k in ['sport','group','model','entity','market','start','end','limit','tolerance','line','include_playoffs','lookback','interval_level']},
            records=[],skipped=[],created_at=datetime.now(timezone.utc).isoformat(),config=config,run_id=cache.stem)
        if workspace=='research' or team_filter is not None:result['context'].update(workspace=workspace,team_filter=team_filter)
        result['context']['entity_label']=(('All teams' if entity=='__all__' else str(entity)) if group=='game-outcome' else
            ('All players'+(' · '+team_filter if team_filter else '') if entity=='__all__' else str(selected.iloc[-1].entity_name)))
        if group=='game-outcome':self._games(result,model,frame,dataset,games,progress)
        else:self._players(result,sport,key,frame,selected,games,spec,market,line,progress)
        if not result['records']:
            reasons=list(dict.fromkeys(r['reason'] for r in result['skipped']))[:3]
            raise DataSourceError('No out-of-sample predictions could be evaluated. '+' '.join(reasons))
        result['attempted']=len(games)
        result['metrics']=diagnostics(result['records'],tolerance)
        if progress:progress(len(games),len(games),'Saving reports')
        self.ensure_exports(result)
        atomic_json(cache,dict(identity=identity,result=result))
        self._remember_report(cache)
        return result

    @replay_scope
    def _games(self,result,model,frame,dataset,games,progress=None):
        previous_cutoff=None
        for number,pair in enumerate(games,1):
            h=pair[pair.is_home.eq(1)].iloc[0];a=pair[pair.is_home.eq(0)].iloc[0]
            try:
                cutoff=h.event_date
                # Only model inputs and schedule identities go into predict.
                if cutoff!=previous_cutoff:
                    prior=estimation_history(frame,cutoff,result['context']['lookback'],paired=True)
                    previous_cutoff=cutoff
                inputs=replace(dataset,games=prior) if dataset is not None else prior
                forecast=model.predict(inputs,h.team,a.team,cutoff.isoformat(),bool(h.get('neutral',False)))
                margin=float(h.points-a.points);total=float(h.points+a.points)
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
                              lower=forecast.margin_mean-PREDICTIVE_Z*forecast.margin_sd,
                              upper=forecast.margin_mean+PREDICTIVE_Z*forecast.margin_sd,interval_level=PREDICTIVE_INTERVAL)
                result['records'].append(common)
            except (ValueError,RuntimeError,OSError,np.linalg.LinAlgError) as exc:
                result['skipped'].append(dict(game_id=str(h.game_id),date=h.event_date.isoformat(),reason=str(exc)))
            finally:
                if progress:progress(number,len(games),f'Evaluated {number:,} of {len(games):,} games')

    @replay_scope
    def _players(self,result,sport,key,frame,selected,games,spec,market,line,progress=None):
        from models.Player_Stats.Weighted_Historical_Performance.core import WHPConfig,WeightedHistoricalPerformance
        service=self.player_service(key) if key!='whp' else None
        team_games,_=self.game_inputs(sport,'ppp') if service else (None,None)
        pairs={str(game_id):pair for game_id,pair in team_games.groupby('game_id')} if service else {}
        whp=WeightedHistoricalPerformance(configured(WHPConfig.for_sport(sport),result['config'])) if key=='whp' else None
        if service:
            service.config=configured(player_config(sport,key),result['config'])
            # Overlapping replays can forecast the same historical matchup.
            # Keep their intermediate files independent, as well as their fits.
            service.repository.root=service.repository.root/result['run_id']
        context=result['context'];lookback=context['lookback']
        aggregate=context.get('entity')=='__all__'
        subjects={}
        if aggregate:
            for row in games:subjects.setdefault(str(row.game_id),set()).add(row.entity_name)
        joint_id=None;joint_forecast=None
        if service:
            load=service.repository.load
            phases=self.phase_map(sport)
            phases.update(frame.set_index('game_id').season_type.to_dict())
            def bounded_load(sport,start,teams):
                players,clubs=load(sport,start,teams)
                def bounded(rows,identity):
                    rows=model_frame(rows)
                    rows=season_phases(rows,sport,phases)
                    rows=rows[rows.season_type.isin(['regular','postseason'] if context['include_playoffs'] else ['regular'])]
                    return estimation_history(rows,pd.Timestamp(start)-pd.Timedelta(microseconds=1),lookback,entity=identity)
                return bounded(players,'entity_id'),bounded(clubs,'team')
            service.repository.load=bounded_load
        if sport in {'NBA','NFL'}:
            module=import_module('.nba_markets' if sport=='NBA' else '.nfl_markets',package='sports_analytics')
            codes=getattr(module,sport+'_TEAM_CODES');names={v:k for k,v in codes.items()}
        else:names={}
        for number,game in enumerate(games,1):
            try:
                cutoff=game.event_date
                own=selected[selected.entity_id.eq(str(game.entity_id))&selected.value.notna()]
                past=estimation_history(own,cutoff,lookback,entity='entity_id')
                if key=='whp':
                    fitted=whp.forecast(past,cutoff,nonnegative=spec.nonnegative,count=spec.count)
                else:
                    pair=pairs.get(str(game.game_id))
                    if pair is None or len(pair)!=2:raise DataSourceError('No saved paired matchup teams for this player appearance.')
                    h=pair[pair.is_home.eq(1)].iloc[0];a=pair[pair.is_home.eq(0)].iloc[0]
                    row=dict(subject=game.entity_name,market_key=market,market=market,line=line if line is not None else 0,
                             selection='Over',odds=None,group='Player Stats',updated='Not reported')
                    rows=[dict(row,subject=name) for name in sorted(subjects[str(game.game_id)])] if aggregate else [row]
                    snapshot=dict(sport=sport,game_id=str(game.game_id),home_team=names.get(h.team,h.team),
                        away_team=names.get(a.team,a.team),pulled_at=(cutoff-pd.Timedelta(microseconds=1)).isoformat(),
                        commence_time=cutoff.isoformat(),rows=rows)
                    if not aggregate or joint_id!=str(game.game_id):
                        joint_forecast=service.forecast(snapshot);joint_id=str(game.game_id)
                    forecast=joint_forecast;fitted=forecast.forecasts.get((game.entity_name,market))
                    if fitted is None:raise DataSourceError(forecast.errors.get((game.entity_name,market),'Required opportunity history is unavailable.'))
                price=fitted.market(line,'Over') if line is not None else None
                actual=float(game.value);mean=float(getattr(fitted,'mean'))
                result['records'].append(dict(game_id=str(game.game_id),date=cutoff.isoformat(),
                    label=f'{game.entity_name} · {game.team} vs {game.get("opponent","")}',actual=actual,predicted=mean,
                    lower=player_quantile(fitted,(1-PREDICTIVE_INTERVAL)/2),
                    upper=player_quantile(fitted,(1+PREDICTIVE_INTERVAL)/2),interval_level=PREDICTIVE_INTERVAL,
                    baseline=float(past.value.tail(5).mean()) if len(past) else None,
                    probability=price.probability if price else None,
                    event_actual=int(actual>line) if line is not None and actual!=line else None,
                    push=bool(line is not None and actual==line),latest_training=fitted.diagnostics.get('last_game_date')))
            except (ValueError,RuntimeError,OSError,IndexError,np.linalg.LinAlgError) as exc:
                result['skipped'].append(dict(game_id=str(game.game_id),date=game.event_date.isoformat(),
                    reason=(str(game.entity_name)+': ' if aggregate else '')+str(exc)))
            finally:
                if progress:progress(number,len(games),f'Evaluated {number:,} of {len(games):,} '+('player appearances' if aggregate else 'games'))
