"""College opportunity simulation shared by UO, OAM and PGE.

NCAA sacks are already rushing attempts. CFBD boxes do not supply targets:
receivers share the observed completion pool, never an invented target pool.
Only explicit player channels enter; missing off-role counts are not zeros.
"""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import numpy as np
import pandas as pd

from sports_analytics.data import DataSourceError
from sports_analytics.ncaaf_model_data import NcaafModelData
from .config import UsageOpportunityConfig
from .distributions import UsageOpportunityForecast
from .efficiency_model import conversion,yards_per_event
from .errors import UsageOpportunityError
from .historical_data import eligible,match_player,atomic_json
from .markets import market_spec,prop_key
from .nfl_model import split_completions
from .opportunity_model import observed,share_rows,allocate
from .simulation import SimulationResult,team_states,integer_pool
from models.season_weights import season_ids

TEAM_COLUMNS=('attempts','completions','carries','passing_yards','rushing_yards')
CONVERSIONS={('completions','attempts'):'cfb:completion',
             ('interceptions','incompletions'):'cfb:int_rate'}
YARDS={'passing_yards':'cfb:rec_ypc','receiving_yards':'cfb:rec_ypc','rushing_yards':'cfb:rush_ypc'}


def defense_features(teams):
    frames=[]
    for component,actual,exposure in [
        ('cfb:pass',teams.attempts,teams.attempts+teams.carries),
        ('cfb:completion',teams.completions,teams.attempts),
        ('cfb:rec_ypc',teams.passing_yards,teams.completions),
        ('cfb:rush_ypc',teams.rushing_yards,teams.carries),
        ('cfb:int_rate',teams.get('interceptions',pd.Series(np.nan,index=teams.index)),teams.attempts-teams.completions)]:
        mask=np.isfinite(actual)&np.isfinite(exposure)&exposure.gt(0)
        part=teams.loc[mask,['game_id','team','opponent','event_date','available_at',
                             *(['season'] if 'season' in teams else [])]].rename(
            columns={'team':'offense','opponent':'defense'})
        part=part.assign(component=component,actual=actual[mask],exposure=exposure[mask])
        frames.append(part)
    return pd.concat(frames,ignore_index=True).sort_values(['component','event_date','game_id','offense'])


def college_context(model,opponents,config,cutoff,rng):
    from models.Player_Stats.Opponent_Adjusted_Matchup.opponent_adjustment import MatchupContext

    class CollegeContext(MatchupContext):
        def neutral_successes(self,frame,success,trial):
            return np.clip(frame[success].to_numpy(float)/self.historical(frame,CONVERSIONS[success,trial]),
                           0,frame[trial].to_numpy(float))

        def conversion(self,offense,frame,success,trial,draws):
            return np.clip(draws*self.current(offense,CONVERSIONS[success,trial]),0,1)

        def neutral_yards(self,frame,yards,count):
            return frame[yards].to_numpy(float)/self.historical(frame,YARDS[yards])

        def yards(self,offense,frame,yards,count,values):
            return np.rint(values*self.current(offense,YARDS[yards])).astype(int)

    return CollegeContext(model,'NCAAF',opponents,config,cutoff,rng)


def volume_features(teams):
    """Full-game NCAA box plays, including any OT, with paired actual margins."""
    teams=teams.assign(season=season_ids(teams,'NCAAF'))
    rows=[]
    for gid,game in teams.groupby('game_id',sort=False):
        if len(game)!=2:continue
        a,b=[r for _,r in game.iterrows()]
        if a.opponent!=b.team or b.opponent!=a.team or a.event_date!=b.event_date:continue
        values=[a.attempts+a.carries,b.attempts+b.carries]
        if not np.isfinite([*values,a.points,b.points]).all() or min(values)<=0:continue
        for i,r in enumerate([a,b]):
            rows.append(dict(game_id=str(gid),team=r.team,opponent=[b,a][i].team,event_date=r.event_date,
                available_at=max(a.available_at,b.available_at),volume=values[i],allowed_volume=values[1-i],
                margin=float(r.points-[b,a][i].points),extra=0.,duration=60.,season=r.season,
                source='NCAA box attempts + rushing attempts; full game, overtime unseparated'))
    if not rows:raise UsageOpportunityError('PGE needs paired college team boxes and scores.')
    return pd.DataFrame(rows).sort_values(['event_date','game_id','team'])


def simulate_college(players,teams,config,rng,*,adjustment=None,environment=None):
    result=SimulationResult()
    states=(team_states if environment is None else environment.team_states)(teams,config,rng,TEAM_COLUMNS)
    for club,state in states.items():
        if (state[['attempts','completions','carries']].lt(0).any().any()
                or state.completions.gt(state.attempts).any()):
            raise UsageOpportunityError('Invalid observed college pass/rush counts.')
        histories={name:info['history'] for name,info in players.items() if info['team']==club}
        roles={};used={};failures={};role_counts={}
        # Estimate each channel separately: a receiver need not have QB data.
        for channel,denominator,yards in [('attempts','attempts','passing_yards'),
                                          ('carries','carries','rushing_yards'),
                                          ('receptions','completions','receiving_yards')]:
            ids=[];shares=[]
            totals=teams[club].copy()
            if channel=='receptions':totals['receptions']=totals.completions
            for name,frame in histories.items():
                try:
                    share,sample=share_rows(frame,totals,[channel],config,rng,environment=environment)
                    columns=[yards,channel] if channel!='attempts' else ['passing_yards','completions','attempts']
                    efficiency=observed(frame,columns,config.minimum_games,config.efficiency_window)
                    if channel=='attempts' and (efficiency.completions.lt(0)|efficiency.completions.gt(efficiency.attempts)).any():
                        raise UsageOpportunityError('Player completions must be between zero and attempts.')
                    ids.append(name);shares.append(share[channel])
                    role_counts.setdefault(name,{})[channel]=len(sample)
                    used.setdefault(name,[]).extend([sample,efficiency])
                except UsageOpportunityError as exc:
                    failures[name,channel]=str(exc)
            roles[channel]=(ids,np.column_stack(shares) if shares else np.empty((config.simulations,0)))
        historical=(state.attempts+state.carries).to_numpy(float)
        if (historical<=0).any():raise UsageOpportunityError('College offensive plays must be positive.')
        # NCAA sacks are in carries; adding sacks here would double count plays.
        plays=integer_pool(historical if environment is None else environment.volume[club])
        p=state.attempts.to_numpy(float)/historical
        if adjustment is not None:p=adjustment.ratio(club,state,'cfb:pass',p).clip(0,1)
        attempts=rng.binomial(plays,p)
        carries=plays-attempts
        catch_p,_,_=conversion(teams[club],'completions','attempts',config,rng,adjustment=adjustment,team=club)
        completed=rng.binomial(attempts,catch_p)
        pools={'attempts':attempts,'carries':carries,'receptions':completed}
        allocations={key:allocate(pools[key],shares,rng) for key,(_,shares) in roles.items()}
        qb_completed=split_completions(allocations['attempts'],completed,rng)
        rec_yards=[]
        for channel,yards in [('receptions','receiving_yards'),('carries','rushing_yards')]:
            ids,_=roles[channel]
            for i,name in enumerate([*ids,'__other__']):
                frame=teams[club].copy() if name=='__other__' else histories[name]
                if name=='__other__' and channel=='receptions':
                    frame['receptions']=frame.completions;frame['receiving_yards']=frame.passing_yards
                outcomes,_,sample=yards_per_event(frame,yards,channel,allocations[channel][:,i],config,rng,
                                                adjustment=adjustment,team=club)
                if channel=='receptions':rec_yards.append(outcomes)
                if name!='__other__':
                    result.players.setdefault(name,{}).update({channel:allocations[channel][:,i],yards:outcomes})
                    used[name].append(sample)
        total_yards=np.column_stack(rec_yards).sum(axis=1)
        fractions=np.divide(qb_completed,completed[:,None],out=np.zeros_like(qb_completed,dtype=float),
                            where=completed[:,None]>0)
        passing=np.rint(fractions*total_yards[:,None]).astype(int)
        passing[:,-1]+=total_yards-passing.sum(axis=1)
        for i,name in enumerate(roles['attempts'][0]):
            values=result.players.setdefault(name,{})
            values.update(attempts=allocations['attempts'][:,i],completions=qb_completed[:,i],passing_yards=passing[:,i])
            frame=histories[name].copy();frame['incompletions']=frame.attempts-frame.completions
            try:
                prob,_,sample=conversion(frame,'interceptions','incompletions',config,rng,adjustment=adjustment,team=club)
                values['interceptions']=rng.binomial(values['attempts']-values['completions'],prob)
                used[name].append(sample)
            except UsageOpportunityError as exc:failures[name,'interceptions']=str(exc)
        for name in histories:
            if name not in result.players:
                result.errors[name]='No complete observed opportunity channel: '+failures.get((name,'attempts'),'insufficient history')
                continue
            frames=[*used[name],observed(teams[club],TEAM_COLUMNS,config.minimum_games,config.team_window)]
            result.diagnostics[name]=dict(training_games=max(role_counts[name].values()),role_games=role_counts[name],
                last_game_date=max(f.event_date.max() for f in frames).date().isoformat(),
                opportunity_projection={c:float(result.players[name][c].mean()) for c in pools if c in result.players[name]},
                channel_errors={c:msg for (who,c),msg in failures.items() if who==name})
        result.audit[club]=dict(plays=plays,attempts=attempts,carries=allocations['carries'],
            qb_attempts=allocations['attempts'],completions=qb_completed,receptions=allocations['receptions'],
            passing_yards=passing,receiving_yards=np.column_stack(rec_yards))
    return result


class CollegePlayerMarketService:
    def __init__(self,method,repository,config=None):
        self.method,self.repository,self.config=method,repository,config

    def forecast(self,snapshot):
        from .prediction_service import UsageMarketForecast

        class CollegeForecast(UsageMarketForecast):
            def detail(self,row):
                estimate=self.forecasts.get(prop_key(row))
                if estimate is None:return self.errors.get(prop_key(row),'College estimate unavailable.')
                d=estimate.diagnostics
                return (f'{d["method"]} mean {estimate.mean:.2f}; 90% interval '
                        f'[{estimate.quantile(.05)}, {estimate.quantile(.95)}]; '
                        f'{d["training_games"]} observed role games; latest input {d["last_game_date"]}. '+d['limitations'])

        times=[pd.Timestamp(snapshot[k]) for k in ['pulled_at','commence_time']]
        if any(pd.isna(t) or t.tzinfo is None for t in times):raise DataSourceError('Forecast timestamps need a timezone.')
        cutoff=min(times)
        config=self.config
        if config is None:
            if self.method=='OAM':
                from models.Player_Stats.Opponent_Adjusted_Matchup.config import MatchupConfig
                config=MatchupConfig.for_sport('NCAAF')
            elif self.method=='PGE':
                from models.Player_Stats.Pace_Game_Environment.config import PaceConfig
                config=PaceConfig.for_sport('NCAAF')
            else:config=UsageOpportunityConfig.for_sport('NCAAF')
        base=config.baseline if hasattr(config,'baseline') else config
        names=[snapshot['home_team'],snapshot['away_team']]
        data=NcaafModelData(directory=self.repository.players.college_directory)
        if not (data.directory/'schools.json').exists():
            raise DataSourceError('Prepare the college school directory and history before forecasting.')
        clubs=[data.resolve_school(name) for name in names]
        players,teams=self.repository.load('NCAAF',snapshot['commence_time'],names)
        players,teams=eligible(players,cutoff,'NCAAF'),eligible(teams,cutoff,'NCAAF')
        if players.empty or teams.empty:raise DataSourceError('No saved college history before the snapshot cutoff.')
        opponents=dict(zip(clubs,clubs[::-1]))
        club_data={club:rows for club,rows in teams[teams.team.isin(clubs)].groupby('team')}
        if set(club_data)!=set(clubs):raise DataSourceError('Both college teams need saved box scores.')
        signature=sha256(b''.join(p.read_bytes() for p in sorted([
            *Path(__file__).parent.glob('*.py'),
            *(Path(__file__).parent.parent/'Opponent_Adjusted_Matchup').glob('*.py'),
            *(Path(__file__).parent.parent/'Pace_Game_Environment').glob('*.py'),
            *(Path(__file__).parent.parent/'Weighted_Historical_Performance').glob('*.py'),
            Path(__file__).resolve().parents[3]/'sports_analytics/ncaaf_model_data.py',
            Path(__file__).resolve().parents[2]/'season_weights.py']))).hexdigest()
        identity=dict(method=self.method,config=asdict(config),implementation=signature,snapshot=snapshot,
            input_sha256=sha256((players.to_json(orient='table',date_format='iso',index=False)+
                                teams.to_json(orient='table',date_format='iso',index=False)).encode()).hexdigest())
        path=self.repository.root/'forecasts/ncaaf'/(sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()+'.json')
        if path.exists():
            try:
                saved=json.loads(path.read_text(encoding='utf-8'))
                if saved['identity']==identity:
                    return CollegeForecast({tuple(r['key']):UsageOpportunityForecast(**r['forecast']) for r in saved['forecasts']},
                                           {tuple(r['key']):r['error'] for r in saved['errors']})
            except (ValueError,KeyError,TypeError):pass
        result,matched=CollegeForecast(),{}
        for row in snapshot['rows']:
            if not row['market_key'].startswith('player_'):continue
            key=prop_key(row)
            if market_spec('NCAAF',row['market_key']) is None:
                result.errors[key]='No connected college red-zone/scorer-order model for this touchdown market.'
                continue
            try:
                history=match_player(players,snapshot,row['subject'],cutoff)
                club=history.iloc[-1].team
                if club not in clubs:raise ValueError('Latest observed team is outside this matchup.')
                matched[row['subject']]=dict(team=club,history=history[history.team.eq(club)])
            except ValueError as exc:result.errors[key]=str(exc)
        if matched:
            matched=dict(sorted(matched.items()))
            seed=int(sha256(f'NCAAF:{snapshot["game_id"]}:{base.seed}'.encode()).hexdigest()[:16],16)
            adjustment=environment=None;method_details={};latest=[]
            if self.method=='OAM':
                from models.Player_Stats.Opponent_Adjusted_Matchup.schedule_strength import ScheduleAdjustedDefense
                model=ScheduleAdjustedDefense(config).fit(defense_features(teams))
                adjustment=college_context(model,opponents,config,cutoff,np.random.default_rng(seed+1))
                if not all(any(club==c for c,component in adjustment.profiles) for club in clubs):
                    raise DataSourceError('OAM needs more pre-snapshot college defensive history; prepare prior seasons.')
                method_details['defense']={club:adjustment.diagnostics(club) for club in clubs}
                latest=[r['event_date'][:10] for r in model.records]
            if self.method=='PGE':
                from models.Player_Stats.Pace_Game_Environment.pace_model import PaceModel
                from models.Player_Stats.Pace_Game_Environment.game_environment import GameEnvironment
                volumes=volume_features(teams)
                model=PaceModel(config).fit(volumes)
                environment=GameEnvironment('NCAAF',model,clubs,config,np.random.default_rng(seed+1))
                valid=set(zip(volumes.game_id,volumes.team))
                club_data={club:frame[[(str(r.game_id),r.team) in valid for _,r in frame.iterrows()]]
                           for club,frame in club_data.items()}
                method_details['pace']={club:dict(**environment.projections[club],
                    mean_total_volume=float(environment.volume[club].mean()),volume_unit='full-game NCAA plays; OT unseparated',
                    validation=model.validation()) for club in clubs}
                latest=[model.history.event_date.max().date().isoformat()]
            simulation=simulate_college(matched,club_data,base,np.random.default_rng(seed),
                                        adjustment=adjustment,environment=environment)
            for row in snapshot['rows']:
                key=prop_key(row);spec=market_spec('NCAAF',row['market_key'])
                if key in result.forecasts or key in result.errors or spec is None:continue
                name=row['subject'];samples=simulation.players.get(name,{})
                if not set(spec.fields)<=set(samples):
                    result.errors[key]=simulation.errors.get(name,'Not enough observed college appearances for this opportunity channel.')
                    continue
                diagnostics={**simulation.diagnostics[name],**method_details, 'method':self.method,'sport':'NCAAF',
                    'last_game_date':max([simulation.diagnostics[name]['last_game_date'],*latest]),
                    'as_of':cutoff.isoformat(),'config':asdict(config),'input_sha256':identity['input_sha256'],
                    'limitations':'School-scoped CFBD boxes, current season + two prior seasons. Current-school role shares only; '
                    'completion shares replace unavailable receiver targets. NCAA sacks stay in rushing attempts. '
                    'No live injury, red-zone or scorer-order feed. '+
                    ('Defense priors use the saved school/opponent cohort, not all FBS teams. ' if self.method=='OAM' else '')+
                    ('Pace and score-script scenarios use full-game plays; overtime is not separately identified.' if self.method=='PGE' else '')}
                result.forecasts[key]=UsageOpportunityForecast.from_samples(sum(samples[f] for f in spec.fields),diagnostics)
        atomic_json(path,dict(identity=identity,forecasts=[dict(key=list(k),forecast=asdict(v)) for k,v in result.forecasts.items()],
                              errors=[dict(key=list(k),error=v) for k,v in result.errors.items()]))
        return result
