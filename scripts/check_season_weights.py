"""Offline cross-season weighting, sample safeguards and replay regressions."""
from dataclasses import asdict, replace
from pathlib import Path
import sys
from unittest import TestCase, main
from unittest.mock import Mock, patch
from copy import deepcopy
from types import SimpleNamespace
import json
import tempfile
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models.season_weights import season_factors, calendar_weights, apply_season_weights
from models.Player_Stats.Weighted_Historical_Performance.core import WHPConfig, WeightedHistoricalPerformance
from models.Player_Stats.Usage_Opportunity.config import UsageOpportunityConfig
from models.Player_Stats.Usage_Opportunity.opportunity_model import history_weights, draw_rows
from models.Player_Stats.Usage_Opportunity.efficiency_model import conversion
from models.Player_Stats.Opponent_Adjusted_Matchup.config import MatchupConfig
from models.Player_Stats.Opponent_Adjusted_Matchup.schedule_strength import ScheduleAdjustedDefense, record_weights
from models.Player_Stats.Pace_Game_Environment.config import PaceConfig
from models.Player_Stats.Pace_Game_Environment.pace_model import PaceModel
from sports_analytics.research_config import default_config, configured
from scripts.check_backtesting import team_history
from models.season_weights import POLICY_VERSION


def history():
    dates = pd.to_datetime(['2024-10-10', '2024-10-13', '2025-10-10', '2025-10-13'], utc=True)
    return pd.DataFrame(dict(game_id=list('abcd'), event_date=dates,
        available_at=dates+pd.Timedelta(hours=8), season=[2024,2024,2025,2025], value=[10,10,30,30]))


class SeasonWeightChecks(TestCase):
    def test_all_21_sport_model_configs_expose_validated_season_discount(self):
        for sport in ['NBA','NFL','NCAAF']:
            for group, methods in [('game-outcome',['ppp','epa','elo']),('player-stats',['whp','uo','oam','pge'])]:
                for method in methods:
                    cfg=default_config(sport,group,method)
                    base=getattr(cfg,'baseline',cfg)
                    self.assertEqual(base.prior_season_weight,.2,(sport,method))
                    settings={'prior_season_weight':1.} if not hasattr(cfg,'baseline') else {'baseline':{'prior_season_weight':1.}}
                    self.assertNotEqual(asdict(configured(cfg,settings)),asdict(cfg))
                    for invalid in [0,-.2,1.1,float('nan'),float('inf')]:
                        with self.assertRaises(ValueError): replace(base,prior_season_weight=invalid)
                    if method=='elo': self.assertEqual(cfg.season_carry,.2)

    def test_boundaries_postseason_and_source_season_override(self):
        for sport, dates, cutoff in [
            ('NBA',['2025-06-01','2025-10-01'],'2025-11-01'),
            ('NFL',['2026-01-10','2026-09-10'],'2026-10-01'),
            ('NCAAF',['2026-01-10','2026-09-10'],'2026-10-01')]:
            frame=pd.DataFrame(dict(event_date=pd.to_datetime(dates,utc=True)))
            np.testing.assert_allclose(season_factors(frame,sport,cutoff),[.2,1])
        # Delayed 2019-20 NBA playoffs belong to the explicitly sourced season.
        frame=pd.DataFrame(dict(event_date=pd.to_datetime(['2020-10-01','2021-01-01'],utc=True),season=[2019,2020]))
        np.testing.assert_allclose(season_factors(frame,'NBA','2021-02-01'),[.2,1])
        np.testing.assert_allclose(season_factors(history(),'NBA','2026-11-01'),[.04,.04,.2,.2])

    def test_whp_current_season_weight_and_discounted_older_prior(self):
        for sport in ['NBA','NFL','NCAAF']:
            cfg=replace(WHPConfig.for_sport(sport),minimum_games=2,window=4,weighting='uniform',prior_strength=0)
            point,_,w=WeightedHistoricalPerformance(cfg)._point(history(),pd.Timestamp('2025-11-01',tz='UTC'))
            np.testing.assert_allclose(w,[1/12,1/12,5/12,5/12])
            self.assertAlmostEqual(point['adjusted_mean'],80/3)
            self.assertGreater(point['adjusted_mean'],WeightedHistoricalPerformance(replace(cfg,prior_season_weight=1))._point(
                history(),pd.Timestamp('2025-11-01',tz='UTC'))[0]['adjusted_mean'])
        extra=history().iloc[:1].assign(game_id='older',event_date=pd.Timestamp('2024-10-01',tz='UTC'))
        frame=pd.concat([extra,history()],ignore_index=True)
        cfg=replace(cfg,window=2,prior_strength=8)
        point,_,_=WeightedHistoricalPerformance(cfg)._point(frame,pd.Timestamp('2025-11-01',tz='UTC'))
        self.assertAlmostEqual(point['alpha'],2/(2+8*.2))

    def test_future_rows_cannot_change_whp_forecast_and_sparse_history_stays_guarded(self):
        frame=pd.concat([history().assign(game_id=lambda f:f.game_id+str(i),
            event_date=lambda f:f.event_date+pd.Timedelta(days=3*i),
            available_at=lambda f:f.available_at+pd.Timedelta(days=3*i)) for i in range(3)],ignore_index=True)
        cfg=replace(WHPConfig(),minimum_games=2,minimum_calibration=2,window=6)
        model=WeightedHistoricalPerformance(cfg);cutoff='2025-11-01T00:00:00Z'
        expected=asdict(model.forecast(frame,cutoff))
        future=history().assign(game_id=lambda f:'future-'+f.game_id,value=99999,
            event_date=pd.Timestamp('2026-10-01',tz='UTC'),available_at=pd.Timestamp('2026-10-02',tz='UTC'))
        self.assertEqual(asdict(model.forecast(pd.concat([frame,future]),cutoff)),expected)
        with self.assertRaisesRegex(ValueError,'at least'): model.forecast(frame.iloc[:1],cutoff)

    def test_role_draws_and_conversion_use_the_same_season_discount_for_each_sport(self):
        frame=history().assign(success=[1,1,9,9],trial=10)
        for sport in ['NBA','NFL','NCAAF']:
            cfg=replace(UsageOpportunityConfig.for_sport(sport),minimum_games=2,simulations=10000,
                        role_half_life=1e9,efficiency_half_life=1e9)
            weights=history_weights(frame,cfg.role_half_life,cfg)
            self.assertAlmostEqual(weights[-1]/weights[0],5,places=6)
            drawn=draw_rows(frame,cfg,np.random.default_rng(42))
            self.assertGreater(drawn.season.eq(2025).mean(),.81)
            _,posterior,_=conversion(frame,'success','trial',cfg,np.random.default_rng(42))
            _,old,_=conversion(frame,'success','trial',replace(cfg,prior_season_weight=1),np.random.default_rng(42))
            self.assertGreater(posterior['mean'],old['mean']+.2)

    def test_defensive_profiles_and_pace_priors_favor_current_season(self):
        records=history().assign(defense='B',component='x',expected=10.,actual=[10,10,20,20]).to_dict('records')
        cfg=replace(MatchupConfig(),minimum_defense_games=2,defense_half_life=1e9)
        model=ScheduleAdjustedDefense(cfg);model.records=records
        self.assertAlmostEqual(model.profile('B','x')['raw'],11/6,places=7)
        self.assertAlmostEqual(record_weights(records,cfg.defense_half_life,cfg)[-1]/record_weights(records,cfg.defense_half_life,cfg)[0],5,places=6)
        rows=history().assign(team='A',volume=[60,60,100,100],allowed_volume=80.,margin=0.)
        pace=PaceModel(PaceConfig())
        expected=np.average(rows.volume,weights=history_weights(rows,2*pace.config.scenario_half_life,pace.config.baseline))
        self.assertAlmostEqual(pace._features(rows,'A','B')[0],expected)
        self.assertGreater(expected,90)

    def test_game_model_weights_and_nba_epv_sufficient_statistics(self):
        from sports_analytics.backtesting_service import game_model
        from models.Game_Outcome.EPA.NBA_EPA import FEATURES
        from types import SimpleNamespace
        frame=team_history()
        half=len(frame)//2
        frame.loc[frame.index[:half],'event_date']-=pd.DateOffset(years=1)
        frame.loc[frame.index[:half],'available_at']-=pd.DateOffset(years=1)
        frame['season']=np.where(frame.index<half,2024,2025)
        cutoff=pd.Timestamp('2025-12-01',tz='UTC')
        for sport in ['NBA','NFL','NCAAF']:
            for method in ['ppp','epa','elo']:
                model=game_model(sport,method)
                if method=='elo': w=model._weights(frame,cutoff)
                elif sport=='NBA': w=calendar_weights(frame,cutoff,model.config.half_life_days,sport)
                else: w=model._history(frame,cutoff).weight
                control=calendar_weights(frame,cutoff,model.config.half_life_days,sport,1)
                # _history sorts; compare by source index, not by row position.
                if isinstance(w,pd.Series): w=w.reindex(frame.index).to_numpy()
                np.testing.assert_allclose(np.asarray(w)/control.to_numpy(),np.where(frame.index<half,.2,1))
        epv=frame.assign(additional_points=0.,offensive_possessions=100.,defensive_possessions=100.)
        for name in FEATURES:
            for prefix in ['start_','def_start_']:epv[prefix+name]=float(name=='intercept')
        ids=epv.game_id.drop_duplicates().tolist()
        xy=np.zeros((len(ids),len(FEATURES)));xy[:,0]=np.where(np.arange(len(ids))<len(ids)//2,100.,200.)
        dataset=SimpleNamespace(game_ids=ids,state_xx=np.repeat((100*np.eye(len(FEATURES)))[None,:,:],len(ids),axis=0),state_xy=xy)
        model=game_model('NBA','epa');model.config=replace(model.config,minimum_training_games=2)
        fitted=model._fit_epv(epv,dataset,cutoff)
        games=epv.drop_duplicates('game_id')
        weights=calendar_weights(games,cutoff,model.config.half_life_days,'NBA')
        self.assertAlmostEqual(fitted['state_beta'][0],np.average(xy[:,0]/100,weights=weights))


class ProbabilityPolicyChecks(TestCase):
    def test_restored_parlay_keeps_quotes_and_discards_older_model_estimates(self):
        from sports_analytics.parlay import build_leg,clean_state,parlay_summary
        from models.Game_Outcome.PPP.PPP import MarketProbability
        from check_mispricing import snapshot
        saved=snapshot();leg=build_leg(saved,saved['rows'][0],0,'Player market',MarketProbability(.8,.1,.1))
        state=dict(version=1,legs=[leg],collapsed=False)
        self.assertAlmostEqual(clean_state(state)['legs'][0]['model_probability'],8/9)
        legacy=deepcopy(state);legacy['legs'][0].pop('weighting_policy')
        before=deepcopy(legacy);restored=clean_state(legacy)['legs'][0]
        self.assertEqual(restored['odds'],leg['odds']);self.assertEqual(restored['quote_key'],leg['quote_key'])
        for field in ['model_probability','model_odds','win','loss','push']:self.assertIsNone(restored[field])
        self.assertIn('recalculate',restored['error']);self.assertEqual(legacy,before)
        self.assertIsNone(parlay_summary([restored])['model_probability'])

    def test_exchange_calculation_replaces_older_browser_estimates(self):
        from check_kalshi_analysis import game,snapshot
        from sports_analytics.kalshi_analysis import calculate,decorate
        from models.Game_Outcome.PPP.PPP import PPPForecast
        forecast=PPPForecast('NO','PIT',30,20,12,10,diagnostics={'last_game_date':'2026-10-18'})
        engine=Mock(return_value=(forecast,None));player=Mock()
        args=(game(),snapshot(),'live','PPP','WHP',engine,player)
        record=calculate(*args);old=deepcopy(record);old['context'].pop(6)
        for value in old['predictions'].values():value['probability']=.999
        self.assertTrue(all(r['yes_probability'] is None for r in decorate(snapshot(),game(),'live','PPP','WHP',old)['rows']))
        engine.reset_mock();fresh=calculate(*args,previous=old)
        engine.assert_called_once();self.assertEqual(fresh['context'][6],POLICY_VERSION)
        self.assertNotEqual(next(iter(fresh['predictions'].values()))['probability'],.999)

    def test_summary_recalculates_legacy_results_and_hides_stale_cells(self):
        from check_market_summary import SummaryCallbackChecks
        from sports_analytics.market_summary import current_models
        fixture=SummaryCallbackChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        bundle,_=fixture.quotes()
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-quotes')):
            models,_=fixture.cb['summary_models'](bundle,'EPA','OAM',0,'market-summary','NFL','123',None)
        self.assertTrue(current_models(models,bundle,'EPA','OAM'))
        old=deepcopy(models);old.pop('weighting_policy')
        self.assertFalse(current_models(old,bundle,'EPA','OAM'))
        identity=dict(type='summary-model-cell',bundle=bundle['id'],provider='FanDuel',index=0,side='',metric='probability')
        values,_=fixture.cb['summary_model_cells'](old,'EPA','OAM',[identity],'market-summary','NFL',bundle,'123')
        self.assertNotIn('60.00%',str(values))
        fixture.engines['EPA'].forecast.reset_mock()
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='workspace-page')):
            fresh,_=fixture.cb['summary_models'](bundle,'EPA','OAM',0,'market-summary','NFL','123',old)
        fixture.engines['EPA'].forecast.assert_not_called()  # The current-policy forecast is already cached.
        self.assertTrue(current_models(fresh,bundle,'EPA','OAM'))

    def test_saved_filtering_labels_older_archive_and_only_shows_current_defaults(self):
        from check_filtered_results import source,ranking,CRITERIA,NOW
        from sports_analytics.filtered_results import FilteredResults
        with tempfile.TemporaryDirectory() as directory:
            service=FilteredResults(Path(directory)/'filter.sqlite3',now=lambda:NOW)
            run=service.save(source(),ranking(),CRITERIA)
            with service.connect() as connection:
                payload=json.loads(connection.execute('SELECT payload FROM quick_runs WHERE run_id=?',(run,)).fetchone()[0])
                payload.pop('weighting_policy')
                connection.execute('UPDATE quick_runs SET payload=? WHERE run_id=?',(json.dumps(payload),run))
            self.assertEqual(service.upcoming('NBA'),[])
            archived=service.upcoming('NBA','all');self.assertEqual(len(archived),3)
            self.assertTrue(all('Earlier weighting' in row['weighting'] for row in archived))
            service.save(source(),ranking(),CRITERIA,request={'sequence':2})
            self.assertTrue(all(row['weighting_policy']==POLICY_VERSION for row in service.upcoming('NBA')))
            self.assertEqual(len(service.upcoming('NBA','all')),6)

    def test_fanduel_request_and_render_scope_reject_legacy_calculations(self):
        from check_mispricing import MispricingChecks
        from models.Game_Outcome.PPP.PPP import PPPForecast
        from threading import Event
        fixture=MispricingChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        forecast=PPPForecast('BAL','CLE',25,20,12,15,
            diagnostics={'last_game_date':'2026-10-18','as_of':fixture.saved['pulled_at']})
        calculated=Event()
        def estimate(snapshot):
            calculated.set()
            return forecast
        fixture.game_services['PPP'].forecast.side_effect=estimate
        old=dict(context=['#nfl-game-123',fixture.saved['pulled_at']],view_key=['Game','PPP','WHP',None])
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-url')):
            _,history=fixture.cb['show_saved_markets']('Game',fixture.saved['pulled_at'],'#nfl-game-123',None,'PPP','WHP',old,'market')
        self.assertEqual(history['view_key'][-1],POLICY_VERSION)
        self.assertTrue(calculated.wait(2))
        fixture.game_services['PPP'].forecast.assert_called_once()
        request,_=fixture.cb['request_quick_filter'](1,75,5,['PPP'],['WHP'],'#nfl-game-123',fixture.saved['pulled_at'])
        self.assertEqual(request['weighting_policy'],POLICY_VERSION)


if __name__=='__main__': main()
