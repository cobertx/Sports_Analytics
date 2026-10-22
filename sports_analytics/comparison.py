"""Persistent research comparisons built from the existing independent replay jobs."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from .backtesting_service import diagnostics, evaluated_records, evaluation_bounds, parse_lookback
from .backtesting_view import CATALOG, GROUPS
from models.Player_Stats.Weighted_Historical_Performance.historical_data import atomic_json


def methods(group):
    return [model for model in CATALOG if model.group==group]


def page_route(tab,location_hash,sport):
    from .research_view import route
    selected=route(location_hash)
    return dict(sport=sport,group=selected[0]) if tab=='RESEARCH' and selected and selected[1]=='comparison' and sport in {'NBA','NFL','NCAAF'} else None


def selected_methods(group,keys):
    allowed={model.key for model in methods(group)}
    if not isinstance(keys,list) or not keys or len(keys)!=len(set(keys)) or not set(keys)<=allowed:
        raise ValueError('Select at least one method from this comparison group.')
    return [model.key for model in methods(group) if model.key in keys]


class Comparisons:
    def __init__(self,backtesting,*,watch=True):
        self.backtesting=backtesting
        self.root=Path(backtesting.root)/'comparisons'
        self.lock=threading.RLock()
        self.watch=watch

    def follow(self,context,identity):
        """Persist finished report references even when no browser is polling."""
        while True:
            try:manifest=self.refresh(context,identity)
            except OSError:
                # A reader or sync client can briefly hold a report on Windows.
                # Keep following the jobs so their final references are retained.
                time.sleep(.5)
                continue
            if not manifest or not any(e['state'] in {'queued','running'} for e in manifest['entries'].values()):return
            time.sleep(.5)

    def path(self,context,identity):
        if (not context or context.get('sport') not in {'NBA','NFL','NCAAF'} or context.get('group') not in GROUPS
                or not isinstance(identity,str) or not re.fullmatch('[0-9a-f]{32}',identity)):
            raise ValueError('Choose a saved comparison for this sport and group.')
        return self.root/context['sport'].lower()/context['group']/(identity+'.json')

    def read(self,context,identity,*,strict=False):
        try:
            result=json.loads(self.path(context,identity).read_text(encoding='utf-8'))
            if result.get('context')!=context or result.get('id')!=identity:return None
            if not isinstance(result.get('entries'),dict):return None
            selected_methods(context['group'],list(result['entries']))
            return result
        except FileNotFoundError:return None
        except OSError:
            if strict:raise
            return None
        except (ValueError,TypeError,AttributeError):return None

    def save(self,manifest):
        atomic_json(self.path(manifest['context'],manifest['id']),manifest)
        return manifest

    def history(self,context):
        directory=self.path(context,'0'*32).parent
        saved=[self.read(context,path.stem) for path in directory.glob('*.json')]
        return sorted([m for m in saved if m],key=lambda m:m['created_at'],reverse=True)

    def new(self,context,entries,settings,source):
        return self.save(dict(id=uuid4().hex,context=dict(context),created_at=datetime.now(timezone.utc).isoformat(),
                              entries=entries,settings=settings,source=source))

    def launch(self,context,keys,*,entity='__all__',market=None,team_filter=None,start=None,end=None,
               limit=100,tolerance=5,line=None,include_playoffs=False,lookback='Model Default'):
        keys=selected_methods(context['group'],keys)
        if not isinstance(entity,str) or not entity:raise ValueError('Choose an evaluation team or player cohort.')
        if isinstance(limit,bool) or not isinstance(limit,(int,float)) or not math.isfinite(limit) or int(limit)!=limit or not 1<=limit<=10000:
            raise ValueError('Choose 1–10,000 whole evaluation games.')
        if isinstance(tolerance,bool) or not isinstance(tolerance,(int,float)) or not math.isfinite(tolerance) or tolerance<0:
            raise ValueError('Error tolerance must be finite and nonnegative.')
        if line is not None and (isinstance(line,bool) or not isinstance(line,(int,float)) or not math.isfinite(line)):
            raise ValueError('Reference line must be a finite number or blank.')
        if context['group']=='player-stats' and not market:raise ValueError('Choose a statistic supported by the selected methods.')
        evaluation_bounds(start,end,self.backtesting.now())
        for key in keys:parse_lookback(lookback,context['sport'],context['group'],key)
        settings=dict(entity=entity,market=market if context['group']=='player-stats' else None,
            team_filter=team_filter if context['group']=='player-stats' else None,start=start,end=end,limit=int(limit),
            tolerance=tolerance,line=line if context['group']=='player-stats' else None,
            include_playoffs=include_playoffs,lookback=lookback)
        # Write an initial manifest before submitting any jobs. Each method's
        # normal JSON/CSV report remains independently recoverable.
        with self.lock:
            manifest=self.new(context,{key:dict(state='queued') for key in keys},settings,'new')
            for key in keys:
                try:
                    job=self.backtesting.start(sport=context['sport'],group=context['group'],key=key,
                        workspace='research',display_label='Comparison · '+entity,**settings)
                    manifest['entries'][key]=dict(state='running',job_id=job)
                except (ValueError,OSError,RuntimeError) as exc:
                    manifest['entries'][key]=dict(state='failed',message=str(exc))
                self.save(manifest)
            if self.watch:
                threading.Thread(target=self.follow,args=(dict(context),manifest['id']),
                                 name='comparison-'+manifest['id'][:8],daemon=True).start()
            return manifest

    def from_saved(self,context,keys,run_ids):
        keys=selected_methods(context['group'],keys)
        entries={}
        for key in keys:
            report=self.backtesting.saved(context['sport'],context['group'],key,run_ids.get(key))
            if not report:raise ValueError(f'Choose an available saved simulation for {key.upper()}.')
            entries[key]=dict(state='completed',run_id=report['run_id'])
        self.validate_reports(context,self.reports(dict(context=context,entries=entries)))
        with self.lock:return self.new(context,entries,{},'saved')

    def refresh(self,context,identity):
        with self.lock:
            manifest=self.read(context,identity,strict=True)
            if not manifest:return None
            original=json.dumps(manifest,sort_keys=True)
            for key,entry in manifest['entries'].items():
                if entry['state'] not in {'running','queued'}:continue
                job=self.backtesting.progress(entry.get('job_id'))
                if job is None:
                    entry.update(state='interrupted',message='This run was interrupted. Run another comparison to retry.')
                elif job['state']=='completed':
                    entry.update(state='completed',run_id=job['result']['run_id'],percent=100,message='Completed')
                else:
                    entry.update(state=job['state'],percent=job.get('percent',0),message=job.get('message',''))
            if json.dumps(manifest,sort_keys=True)!=original:self.save(manifest)
            return manifest

    def reports(self,manifest):
        context=manifest['context'];reports={}
        for key,entry in manifest['entries'].items():
            if entry.get('run_id'):
                report=self.backtesting.saved(context['sport'],context['group'],key,entry['run_id'])
                if report:reports[key]=report
        return reports

    @staticmethod
    def validate_reports(context,reports):
        if not reports:raise ValueError('No completed reports are available yet.')
        for key,report in reports.items():
            if any(report['context'].get(k)!=v for k,v in [*context.items(),('model',key)]):
                raise ValueError('Reports must belong to the selected sport, group and method.')
        if context['group']=='player-stats':
            markets={r['context'].get('market') for r in reports.values()}
            lines={r['context'].get('line') for r in reports.values()}
            if len(markets)!=1 or len(lines)!=1:
                raise ValueError('Player comparisons require the same statistic and reference line in every saved simulation.')


def record_identity(record,group):
    # Legacy player reports lack provider IDs. The exact recorded label includes
    # the player and matchup, so all methods still align the same appearance.
    return (str(record['game_id']),record.get('label','') if group=='player-stats' else '')


def compare_reports(context,reports,target='margin',basis='common',tolerance=5):
    Comparisons.validate_reports(context,reports)
    if target not in {'margin','total'} or basis not in {'common','all'}:raise ValueError('Choose a valid target and comparison sample.')
    if isinstance(tolerance,bool) or not isinstance(tolerance,(int,float)) or not math.isfinite(tolerance) or tolerance<0:
        raise ValueError('Error tolerance must be finite and nonnegative.')
    by_method={}
    for key,report in reports.items():
        indexed={}
        for record in evaluated_records(report,target):
            identity=record_identity(record,context['group'])
            if identity in indexed:raise ValueError('A saved report contains duplicate evaluated game/player identities.')
            indexed[identity]=record
        by_method[key]=indexed
    common=set.intersection(*(set(rows) for rows in by_method.values()))
    # Data revisions must not turn a common game into different actual outcomes.
    inconsistent={identity for identity in common if len({rows[identity]['actual'] for rows in by_method.values()})!=1}
    common-=inconsistent
    output={};summary=[]
    for model in methods(context['group']):
        if model.key not in by_method:continue
        key=model.key;available=by_method[key]
        identities=common if basis=='common' else set(available)
        rows=sorted([available[identity] for identity in identities],key=lambda r:(r['date'],record_identity(r,context['group'])))
        output[key]=rows
        metric=diagnostics(rows,tolerance)
        levels=sorted({r.get('interval_level') for r in rows if r.get('interval_level') is not None})
        summary.append(dict(method=key.upper(),label=model.label,evaluated=len(available),compared=len(rows),
            excluded=len(available)-len(rows),skipped=len(reports[key].get('skipped',[])),interval_level=levels[0] if len(levels)==1 else None,
            run_id=reports[key]['run_id'],**metric))
    return dict(summary=summary,records=output,common=len(common),conflicting_actuals=len(inconsistent),basis=basis)
