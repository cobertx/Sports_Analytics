"""Elo paper formulas, time ordering, independent caches and market integration."""

from dataclasses import asdict, replace
from pathlib import Path
from statistics import NormalDist
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from Models.Game_Outcome.ELO import ELOConfig, ELOError, ELOForecast, ELOModel, NBAELOForecast, elo_probability, elo_update, reconcile_scores
from Models.Game_Outcome.PPP.PPP import PPPError, implied_probability
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.elo_service import ELOMarketService, NBAELOMarketService
from sports_analytics.service import DataService
from test_ppp import observations as nfl_observations, snapshot as nfl_snapshot
from test_nba_markets import observations as nba_observations, snapshot as nba_snapshot


def completed_nba_observations(days):
    frame = nba_observations(days)
    return frame[frame.points.ne(frame.points_allowed)].copy()


class ELOMathTests(unittest.TestCase):
    def test_paper_logistic_update_accounting_and_settlement(self):
        self.assertAlmostEqual(elo_probability(1600, 1500), .6400649998028851)
        self.assertAlmostEqual(elo_probability(1570, 1510, 45), .646669, places=6)
        home, away = elo_update(1500, 1500, 24, 17)
        self.assertEqual((home, away), (1510, 1490))
        self.assertEqual(sum(elo_update(1530, 1470, 20, 20)), 3000)
        scores = reconcile_scores(47.4, 6.6)
        np.testing.assert_allclose(scores, [27, 20.4])
        result = ELOForecast('SF', 'WSH', *scores, 13, 13.5)
        self.assertAlmostEqual(NormalDist(6.6, 13).cdf(0), 1-.694165, places=5)
        self.assertAlmostEqual(result.spread('SF', -3).probability+result.spread('WSH', 3).probability, 1)
        self.assertGreater(result.total(47).push, 0)
        self.assertAlmostEqual(result.total(47.5).probability+result.total(47.5, 'Under').probability, 1)
        self.assertEqual(NBAELOForecast('GSW','LAL',115,110,13,18).spread('GSW',0).push,0)
        np.testing.assert_allclose(reconcile_scores(10,100),[10,0])
        for kwargs in [dict(sport='NCAAF'),dict(elo_scale=0),dict(season_carry=1.1),dict(k_factor=float('nan'))]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ELOError):
                ELOConfig(**kwargs)

    def test_replay_is_zero_sum_and_offseason_retains_a_fraction_of_strength(self):
        frame=completed_nba_observations(15)
        model=ELOModel(ELOConfig.nba(minimum_team_games=1,home_elo=0))
        history=model._history(frame,'2025-11-01T00:00:00Z')
        before=model._elo(history,pd.Timestamp('2026-09-01T00:00:00Z'))
        after=model._elo(history,pd.Timestamp('2026-10-20T00:00:00Z'))
        self.assertAlmostEqual(sum(before.values()),1500*len(before))
        for team in before:
            self.assertAlmostEqual(after[team]-1500,(before[team]-1500)*.75)

    def test_future_rows_and_unpublished_results_cannot_affect_nfl_or_nba_forecasts(self):
        cases=[('NFL',nfl_observations(35),'SF','WSH','2025-05-01T00:00:00Z'),
               ('NBA',completed_nba_observations(100),'GSW','LAL','2026-01-15T00:00:00Z')]
        for sport,frame,home,away,cutoff in cases:
            config=(ELOConfig.nba() if sport=='NBA' else ELOConfig())
            config=replace(config,minimum_training_games=12,minimum_team_games=3,
                           minimum_calibration_games=12,maximum_calibration_games=20)
            model=ELOModel(config)
            result=model.predict(frame,home,away,cutoff)
            self.assertIsInstance(result,NBAELOForecast if sport=='NBA' else ELOForecast)
            self.assertEqual(result.diagnostics['validation_games'],20)
            self.assertGreater(result.margin_sd,0)
            self.assertTrue(0<=result.diagnostics['elo_blend_weight']<=1)
            future=frame.copy()
            future['game_id']='future_'+future.game_id
            future['event_date']='2027-01-01T00:00:00Z'
            future['available_at']='2027-01-03T00:00:00Z'
            future['points']=9999
            if sport=='NFL':future['offensive_points']=9999
            with self.subTest(sport=sport):
                poisoned=model.predict(pd.concat([frame,future],ignore_index=True),home,away,cutoff)
                self.assertEqual(asdict(result),asdict(poisoned))
                future['event_date']='2024-01-01T00:00:00Z'
                unpublished=model.predict(pd.concat([frame,future],ignore_index=True),home,away,cutoff)
                self.assertEqual(asdict(result),asdict(unpublished))
                with self.assertRaises(PPPError):model.predict(frame,'MISSING',away,cutoff)
                with self.assertRaises(PPPError):model.predict(frame.iloc[:4],home,away,cutoff)
                duplicate=pd.concat([frame,frame.iloc[:1]],ignore_index=True)
                with self.assertRaises(PPPError):model.predict(duplicate,home,away,cutoff)

    def test_total_depends_on_scoring_observations_not_just_elo_wins(self):
        frame=completed_nba_observations(70)
        config=ELOConfig.nba(minimum_training_games=10,minimum_team_games=2,
                            minimum_calibration_games=10,maximum_calibration_games=15)
        model=ELOModel(config)
        cutoff='2026-01-01T00:00:00Z'
        history=model._history(frame,cutoff)
        higher=frame.copy()
        higher['points']+=20;higher['points_allowed']+=20
        other=model._history(higher,cutoff)
        fitted=model._fit(history,cutoff)
        self.assertAlmostEqual(float(fitted['power'][1:].sum()),0)
        a=model._raw(history,fitted,'GSW','LAL',cutoff)
        b=model._raw(other,model._fit(other,cutoff),'GSW','LAL',cutoff)
        self.assertEqual(a['elo_probability'],b['elo_probability'])
        self.assertAlmostEqual(a['power_margin'],b['power_margin'])
        self.assertGreater(b['total'],a['total']+30)


class ELOIntegrationTests(unittest.TestCase):
    def test_archives_restore_correct_sport_without_network_or_another_fit(self):
        for service_type,config,result,snapshot in [
            (ELOMarketService,ELOConfig(),ELOForecast('SF','WSH',30,20,13,15,{}),nfl_snapshot()),
            (NBAELOMarketService,ELOConfig.nba(),NBAELOForecast('GSW','LAL',120,105,13,18,{}),nba_snapshot())]:
            with tempfile.TemporaryDirectory() as directory:
                data=Mock(directory=Path(directory));data.prepare.return_value=(pd.DataFrame(),[])
                model=Mock(config=config);model.predict.return_value=result
                with patch('requests.sessions.Session.request',side_effect=AssertionError('Offline test')):
                    first=service_type(data,model).forecast(snapshot)
                    second=service_type(data,model).forecast(snapshot)
                self.assertEqual(asdict(first),asdict(second))
                self.assertIsInstance(second,type(result))
                self.assertEqual(len(list((Path(directory)/'forecasts').glob('elo_*.json'))),1)
                model.predict.assert_called_once()
                self.assertEqual(data.prepare.call_args.args,([2025,2026],))

    def test_nfl_and_nba_selector_updates_columns_edge_charts_and_failure_without_paid_calls(self):
        for sport,saved,result in [('NFL',nfl_snapshot(),ELOForecast('SF','WSH',40,10,13,15,{'as_of':nfl_snapshot()['pulled_at']})),
                                  ('NBA',nba_snapshot(),NBAELOForecast('GSW','LAL',130,100,13,18,{'as_of':nba_snapshot()['pulled_at']}))]:
            with tempfile.TemporaryDirectory() as directory:
                markets,nba_markets,elo,nba_elo,ppp,epa,odds=[Mock() for _ in range(7)]
                (nba_markets if sport=='NBA' else markets).history.return_value=[saved]
                selected=nba_elo if sport=='NBA' else elo
                selected.forecast.return_value=result
                app=create_app(DataService(Path(directory)/'stats.sqlite3'),Mock(),Mock(),odds,markets,
                               ppp_service=ppp,epa_service=epa,nba_market_service=nba_markets,
                               elo_service=elo,nba_elo_service=nba_elo)
                key=next(k for k in app.callback_map if 'saved-market-sections.children' in k)
                config=app.callback_map[key];client=app.server.test_client()
                values={'market-category':'Game','market-snapshot':saved['pulled_at'],
                        'market-url':f'#{sport.lower()}-game-123','market-forecast-method':'ELO',
                        'market-applied-criteria':{'probability':.75,'edge':.05}}
                def render():
                    response=client.post('/_dash-update-component',json={'output':key,'outputs':{'id':'saved-market-sections','property':'children'},
                        'inputs':[{**item,'value':values[item['id']]} for item in config['inputs']],
                        'state':[],'changedPropIds':['market-forecast-method.value']})
                    self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                    return str(response.json)
                text=render()
                for label in ['ELO probability','ELO fair odds','ELO forecast:','ELO probability >','market-criteria-match']:
                    self.assertIn(label,text)
                self.assertNotIn('PPP probability',text)
                self.assertIn(f"{100*(result.spread(result.home_team,-3.5).probability-implied_probability(-110)):+.2f} pp",text)
                self.assertEqual(text.count("'type': 'Graph'"),4)
                selected.forecast.side_effect=DataSourceError('ELO historical data missing')
                failed=render()
                self.assertIn('ELO historical data missing',failed)
                self.assertIn('ELO fair odds',failed)
                ppp.forecast.assert_not_called();epa.forecast.assert_not_called()
                odds.get.assert_not_called();markets.pull.assert_not_called();nba_markets.pull.assert_not_called()


if __name__=='__main__':
    unittest.main()
