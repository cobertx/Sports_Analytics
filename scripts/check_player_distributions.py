"""Player charts reuse fitted probabilities, with no model fitting or HTTP."""
from pathlib import Path
from statistics import NormalDist
from types import SimpleNamespace
import sys
from unittest import TestCase,main
from unittest.mock import patch

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import dcc
from models.Player_Stats.Weighted_Historical_Performance.core import WHPForecast
from models.Player_Stats.Usage_Opportunity.distributions import UsageOpportunityForecast
from models.Player_Stats.Opponent_Adjusted_Matchup.distributions import MatchupForecast
from models.Player_Stats.Pace_Game_Environment.distributions import PaceForecast
from sports_analytics.player_distribution import distribution
from sports_analytics.market_view import market_sections
from scripts.check_mispricing import components
from scripts import check_mispricing as mispricing_fixtures


class PlayerDistributionChecks(TestCase):
    def test_simulated_methods_preserve_each_integer_mass_gaps_and_cdf(self):
        for kind in [UsageOpportunityForecast,MatchupForecast,PaceForecast]:
            fitted=kind.from_samples([5,7,7,8])
            result=distribution(fitted)
            self.assertEqual(result['x'],list(range(9)))
            self.assertEqual(result['mass'],[0,0,0,0,0,.25,0,.5,.25])
            self.assertEqual(result['cumulative'],[0,0,0,0,0,.25,.25,.75,1])
            self.assertAlmostEqual(sum(result['mass']),1)
            for value,mass,cumulative in zip(result['x'],result['mass'],result['cumulative']):
                under=fitted.market(value,'Under')
                self.assertAlmostEqual(mass,under.push)
                self.assertAlmostEqual(cumulative,under.win+under.push)

    def test_normal_distribution_is_predictive_not_a_histogram_of_training_samples(self):
        for nonnegative in [True,False]:
            fitted=WHPForecast(6,4,'normal',nonnegative,[100,200],[.5,.5])
            result=distribution(fitted)
            normal=NormalDist(6,2)
            for x,p,cdf in zip(result['x'],result['mass'],result['cumulative']):
                expected=normal.cdf(x+.5)
                before=0 if nonnegative and x==0 else normal.cdf(x-.5)
                self.assertAlmostEqual(cdf,expected,places=13)
                self.assertAlmostEqual(p,expected-before,places=13)
            self.assertAlmostEqual(sum(result['mass'])+result['below']+result['above'],1)
            self.assertTrue(np.all(np.diff(result['cumulative'])>=-1e-15))
            self.assertEqual(result['x'][0]>=0,nonnegative)

    def test_count_and_weighted_empirical_forecasts_match_actual_settlement(self):
        for fitted in [WHPForecast(4,4,'count',True,[],[]),WHPForecast(4,10,'count',True,[],[]),
                       WHPForecast(4,4,'empirical',True,[2,5,2],[.2,.3,.5])]:
            result=distribution(fitted)
            for x,p,cdf in zip(result['x'],result['mass'],result['cumulative']):
                under=fitted.market(x,'Under')
                self.assertAlmostEqual(p,under.push,places=12)
                self.assertAlmostEqual(cdf,under.win+under.push,places=12)
            self.assertAlmostEqual(sum(result['mass'])+result['below']+result['above'],1)

    def test_signed_yards_deterministic_outcomes_and_far_quotes_keep_real_mass(self):
        signed=distribution(UsageOpportunityForecast.from_samples([-3,0,0,2]),[7.5])
        self.assertEqual(signed['x'],list(range(-3,9)))
        self.assertEqual(signed['mass'][0],.25)
        self.assertEqual(signed['cumulative'][-1],1)
        point=distribution(PaceForecast.from_samples([0,0,0]))
        self.assertEqual(point['x'],[0]);self.assertEqual(point['mass'],[1])
        bounded=distribution(PaceForecast.from_samples([5,5]),[1e15])
        self.assertEqual(bounded['x'][-1],5)

    def test_each_header_has_lazy_method_specific_payload_and_no_eager_graphs(self):
        rows=[dict(market_key='player_points_alternate',subject=player,group='Points',market='Alt Points',
            selection='Over',line=6.5,odds=-110) for player in ['Player A','Player B']]
        snapshot=dict(sport='NBA',game_id='123',pulled_at='2026-10-29T12:00:00Z',rows=rows)
        ids=[]
        for method,kind in [('WHP',None),('UO',UsageOpportunityForecast),('OAM',MatchupForecast),('PGE',PaceForecast)]:
            fitted=kind.from_samples([5,7,7,8]) if kind else WHPForecast(6,4,'normal',True,[],[])
            forecast=SimpleNamespace(forecasts={(row['subject'],'player_points'):fitted for row in rows},
                                     selection=lambda row:fitted.market(row['line'],row['selection']))
            with patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP')):
                content=market_sections(snapshot,'Points',forecast,method=method)
            nodes=list(components(content))
            stores=[node for node in nodes if isinstance(node,dcc.Store)]
            self.assertEqual(len(stores),2)
            self.assertFalse(any(isinstance(node,dcc.Graph) for node in nodes))
            self.assertTrue(all(store.data['method']==method for store in stores))
            self.assertEqual({store.data['player'] for store in stores},{'Player A','Player B'})
            ids.extend(store.id['key'] for store in stores)
        self.assertEqual(len(ids),len(set(ids)))
        unavailable=market_sections(snapshot,'Points',None,method='WHP',loading=True)
        self.assertFalse(any(isinstance(node,dcc.Store) for node in components(unavailable)))

    def test_selected_mispricing_method_reaches_market_header_charts(self):
        fixture=mispricing_fixtures.MispricingChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        for method,kind in [('WHP',None),('UO',UsageOpportunityForecast),('OAM',MatchupForecast),('PGE',PaceForecast)]:
            fitted=kind.from_samples([5,7,7,8]) if kind else WHPForecast(6,4,'normal',True,[],[])
            fitted.diagnostics['last_game_date']='2026-10-20'
            fitted_rows={(row['subject'],row['market_key'].removesuffix('_alternate')):fitted
                         for row in fixture.saved['rows'] if row['market_key'].startswith('player_')}
            fixture.player_services[method].forecast.return_value=SimpleNamespace(
                forecasts=fitted_rows,selection=lambda row:fitted.market(row['line'],row['selection']))
            shown,_=fixture.cb['show_saved_markets']('Passing',fixture.saved['pulled_at'],'#nfl-game-123',None,'PPP',method,None)
            payloads=[node.data for node in components(shown) if isinstance(node,dcc.Store)]
            self.assertEqual(len(payloads),2)
            self.assertTrue(all(payload['method']==method for payload in payloads))
            self.assertTrue(all(payload['mass']==distribution(fitted)['mass'] for payload in payloads))
            fixture.player_services[method].forecast.assert_called_once()


if __name__=='__main__':main()
