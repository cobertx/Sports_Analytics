"""Saved league inputs -> schedule-adjusted components -> frozen prop estimates."""
from dataclasses import asdict
from hashlib import sha256
import copy
import json
import math
from pathlib import Path
import numpy as np
import pandas as pd
from models.replay import replay_instance

from sports_analytics.data import DataSourceError
from sports_analytics.result_cache import load_json, save_json
from sports_analytics.nba_markets import NBA_TEAM_CODES
from sports_analytics.nfl_markets import NFL_TEAM_CODES
from models.Player_Stats.Usage_Opportunity.prediction_service import UsageMarketForecast
from models.Player_Stats.Usage_Opportunity.markets import market_spec,prop_key,quote_index
from models.Player_Stats.Usage_Opportunity.nba_model import simulate_nba
from models.Player_Stats.Usage_Opportunity.nfl_model import simulate_nfl
from .config import MODEL_VERSION,MatchupConfig
from .historical_data import MatchupHistoryRepository,eligible,match_player,identity_name
from .features import feature_rows
from .schedule_strength import ScheduleAdjustedDefense
from .opponent_adjustment import MatchupContext
from .distributions import MatchupForecast
from .market_comparison import compare_price


class MatchupMarketForecast(UsageMarketForecast):
    def detail(self,row):
        fitted=self.forecasts.get(prop_key(row))
        if fitted is None:
            return self.errors.get(prop_key(row),'OAM unavailable for this market.')
        d=fitted.diagnostics
        estimate=self.selection(row)
        price=compare_price(estimate,row['odds'])
        multiplier=d['opponent_multiplier']
        multiplier_text=f'{multiplier:.3f}' if multiplier is not None else 'undefined (zero neutral mean)'
        return (f'OAM neutral mean {d["neutral_mean"]:.2f}; adjusted mean {fitted.mean:.2f}; '
                f'net multiplier {multiplier_text}; SD {math.sqrt(fitted.variance):.2f}; '
                f'90% interval [{fitted.quantile(.05)}, {fitted.quantile(.95)}]; '
                f'{d["training_games"]} role games; defense {d["opponent"]}; '
                f'{fitted.simulations:,} simulations; latest input {d["last_game_date"]}. '
                + (f'EV/$1 {price["expected_net_per_dollar"]:+.3f}. ' if price['expected_net_per_dollar'] is not None else '')
                + 'Factors are estimated by component, not derived from rankings. No live injury, scheme, route or tracking feed.')


def implementation_hash():
    directory=Path(__file__).parent
    sources=[*directory.glob('*.py'),*(directory.parent/'Usage_Opportunity').glob('*.py'),
             *(directory.parent/'Weighted_Historical_Performance').glob('*.py'),directory.parents[1]/'season_weights.py']
    return sha256(b''.join(p.read_bytes() for p in sorted(sources))).hexdigest()


class OpponentAdjustedMarketService:
    def __init__(self,repository=None,config=None,scenarios=(),*,persist=True):
        self.repository=repository or MatchupHistoryRepository()
        self.config=config
        self.scenarios=list(scenarios)
        self.persist=persist

    def prepare(self,snapshot,refresh=False):
        return self.repository.prepare(snapshot['sport'],snapshot['commence_time'],
                                       [snapshot['home_team'],snapshot['away_team']],refresh)

    def forecast(self,snapshot):
        sport=snapshot['sport']
        if sport=='NCAAF':
            if self.scenarios:
                raise DataSourceError('College lineup scenarios are not connected; omit the scenario file.')
            from models.Player_Stats.Usage_Opportunity.ncaaf_model import CollegePlayerMarketService
            return CollegePlayerMarketService('OAM',self.repository,self.config,persist=self.persist).forecast(snapshot)
        if sport not in {'NBA','NFL'}:
            raise DataSourceError('Opponent-adjusted matchup is currently available for NBA and NFL only.')
        config=self.config or MatchupConfig.for_sport(sport)
        timestamps=[pd.Timestamp(snapshot[k]) for k in ['pulled_at','commence_time']]
        if any(pd.isna(t) or t.tzinfo is None for t in timestamps):
            raise DataSourceError('Snapshot and matchup start need timezone-aware timestamps.')
        cutoff=min(timestamps)
        names=[snapshot['home_team'],snapshot['away_team']]
        players,teams=self.repository.load(sport,snapshot['commence_time'],names)
        players,teams=eligible(players,cutoff,sport),eligible(teams,cutoff,sport)
        if players.empty or teams.empty:
            raise DataSourceError('OAM requires saved league-wide team/player history before this snapshot. Use --prepare.')
        codes=NBA_TEAM_CODES if sport=='NBA' else NFL_TEAM_CODES
        clubs=[codes.get(name,name) for name in names]
        if clubs[0]==clubs[1] or not set(clubs)<=set(teams.team):
            raise DataSourceError('Both matchup teams require uniquely mapped saved team histories.')
        opponents=dict(zip(clubs,clubs[::-1]))
        signature=implementation_hash()
        data_hash=sha256((players.to_json(orient='table',date_format='iso',index=False)+
                          teams.to_json(orient='table',date_format='iso',index=False)).encode()).hexdigest()
        identity=dict(version=MODEL_VERSION,implementation=signature,input_sha256=data_hash,
                      config=asdict(config),cutoff=cutoff.isoformat(),scenarios=self.scenarios,
                      snapshot={k:snapshot.get(k) for k in ['sport','game_id','home_team','away_team','pulled_at','commence_time','rows']})
        digest=sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        path=self.repository.root/'forecasts'/sport.lower()/f'{digest}.json'
        saved=load_json(path,self.repository.root,identity) if self.persist else None
        if saved is not None:
            try:
                if saved['identity']==identity:
                    return MatchupMarketForecast(
                        {tuple(r['key']):MatchupForecast(**r['forecast']) for r in saved['forecasts']},
                        {tuple(r['key']):r['error'] for r in saved['errors']})
            except (ValueError,KeyError,TypeError):
                pass
        fit_identity={k:v for k,v in identity.items() if k not in {'snapshot','scenarios'}}
        fit_path=self.repository.root/'defense'/sport.lower()/(sha256(json.dumps(fit_identity,sort_keys=True).encode()).hexdigest()+'.json')
        model=replay_instance(ScheduleAdjustedDefense,config)
        restored=False
        saved=load_json(fit_path,self.repository.root,fit_identity) if self.persist else None
        if saved is not None:
            try:
                if saved['identity']==fit_identity:
                    model.records=saved['records']
                    model.historical={tuple(r[:3]):r[3] for r in saved['historical']}
                    # A disk-restored fit has no continuation queues.
                    model._components={};model._fit_config=None
                    model._profile_records=None
                    restored=True
            except (ValueError,KeyError,TypeError):
                pass
        if not restored:
            model.fit(feature_rows(sport,players,teams))
            if self.persist:
                save_json(fit_path,dict(identity=fit_identity,records=model.records,
                                         historical=[[*key,value] for key,value in model.historical.items()]),self.repository.root)
        result,matched,failed_players=MatchupMarketForecast(),{},{}
        players['_whp_name']=players.entity_name.map(identity_name)
        for row in snapshot['rows']:
            if not row['market_key'].startswith('player_'):
                continue
            key=prop_key(row)
            if market_spec(sport,row['market_key']) is None:
                result.errors[key]='OAM has no connected red-zone/scorer-order model for touchdown props.'
                continue
            if row['subject'] in matched:
                continue
            if row['subject'] in failed_players:
                result.errors[key]=failed_players[row['subject']]
                continue
            try:
                history=match_player(players,snapshot,row['subject'],cutoff)
                club=str(history.iloc[-1].team)
                matched[row['subject']]=dict(team=club,history=history[history.team.eq(club)])
            except ValueError as exc:
                result.errors[key]=str(exc)
                failed_players[row['subject']]=str(exc)
        if matched:
            matched=dict(sorted(matched.items()))
            seed=int(sha256(f'{sport}:{snapshot["game_id"]}:{config.baseline.seed}'.encode()).hexdigest()[:16],16)
            context=MatchupContext(model,sport,opponents,config,cutoff,np.random.default_rng(seed+1),scenarios=self.scenarios)
            if not all(any(team==club for team,component in context.profiles) for club in clubs):
                raise DataSourceError('Not enough schedule-adjusted defensive history for both opponents. Prepare an earlier season too.')
            neutral=copy.copy(context)
            neutral.neutral=True
            neutral.draws={key:np.ones(config.baseline.simulations) for key in context.draws}
            neutral.fallbacks=set()
            team_data={club:rows for club,rows in teams[teams.team.isin(clubs)].groupby('team')}
            engine=simulate_nba if sport=='NBA' else simulate_nfl
            baseline=engine(matched,team_data,config.baseline,np.random.default_rng(seed),adjustment=neutral)
            adjusted=engine(matched,team_data,config.baseline,np.random.default_rng(seed),adjustment=context)
            for row in snapshot['rows']:
                key=prop_key(row)
                if key in result.forecasts or key in result.errors:
                    continue
                spec=market_spec(sport,row['market_key'])
                if spec is None:
                    continue
                subject=row['subject']
                if subject not in adjusted.players or subject not in baseline.players:
                    result.errors[key]=adjusted.errors.get(subject,'No complete player opportunity history before the cutoff.')
                    continue
                actual=adjusted.players[subject]
                if not set(spec.fields)<=set(actual):
                    result.errors[key]='Unsupported simulated outcome channel.'
                    continue
                values=sum(actual[field] for field in spec.fields)
                neutral_values=sum(baseline.players[subject][field] for field in spec.fields)
                neutral_mean=float(neutral_values.mean())
                club=matched[subject]['team']
                components=context.diagnostics(club)
                latest=max([adjusted.diagnostics[subject]['last_game_date'],
                            *[p['last_game_date'] for p in components.values()]])
                diagnostics={**adjusted.diagnostics[subject],'model_version':MODEL_VERSION,
                    'neutral_mean':neutral_mean,'opponent_multiplier':float(values.mean())/neutral_mean if neutral_mean else None,
                    'opponent':opponents[club],'components':components,'last_game_date':latest,'as_of':cutoff.isoformat(),
                    'config':asdict(config),'implementation':signature,'input_sha256':data_hash,
                    'neutral_fallbacks':[component for offense,component in sorted(context.fallbacks) if offense==club],
                    'limitations':'Box-score schedule adjustment. No inferred tactical, injury, route or tracking data. Sparse components remain neutral; NFL TD/order props are unsupported.'}
                result.forecasts[key]=MatchupForecast.from_samples(values,diagnostics)
        if not self.persist:
            return result
        evaluations=[]
        indexed_quotes=quote_index(snapshot['rows'])
        for row in snapshot['rows']:
            if not row['market_key'].startswith('player_'):
                continue
            price=result.selection(row)
            opposite={'Over':'Under','Under':'Over'}.get(row['selection'])
            quotes=indexed_quotes.get((*prop_key(row),row['line'],opposite),[])
            evaluations.append(dict(subject=row['subject'],market=row['market_key'],line=row['line'],selection=row['selection'],
                **compare_price(price,row['odds'],quotes[0] if len(quotes)==1 else None),
                win=price.win if price else None,loss=price.loss if price else None,push=price.push if price else None,
                detail=result.detail(row)))
        save_json(path,dict(identity=identity,
            forecasts=[dict(key=list(k),forecast=asdict(v)) for k,v in result.forecasts.items()],
            errors=[dict(key=list(k),error=v) for k,v in result.errors.items()],evaluations=evaluations),self.repository.root)
        return result
