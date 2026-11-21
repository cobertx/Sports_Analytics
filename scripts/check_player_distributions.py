"""Player charts reuse fitted probabilities, with no model fitting or HTTP."""
from pathlib import Path
from statistics import NormalDist
from types import SimpleNamespace
import sys
import json
from unittest import TestCase,main
from unittest.mock import patch

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import dcc
from models.Player_Stats.Weighted_Historical_Performance.core import WHPForecast
from models.Player_Stats.Usage_Opportunity.distributions import UsageOpportunityForecast
from models.Player_Stats.Opponent_Adjusted_Matchup.distributions import MatchupForecast
from models.Player_Stats.Pace_Game_Environment.distributions import PaceForecast
from app.sections.markets.player_distribution import distribution
from app.sections.markets.market_view import market_sections
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

    def test_each_header_selects_its_ladder_without_mounting_eager_charts(self):
        rows=[dict(market_key='player_points_alternate',subject=player,group='Points',market='Alt Points',
            selection='Over',line=6.5,odds=-110) for player in ['Player A','Player B']]
        snapshot=dict(sport='NBA',game_id='123',pulled_at='2026-10-29T12:00:00Z',rows=rows)
        for method,kind in [('WHP',None),('UO',UsageOpportunityForecast),('OAM',MatchupForecast),('PGE',PaceForecast)]:
            fitted=kind.from_samples([5,7,7,8]) if kind else WHPForecast(6,4,'normal',True,[],[])
            forecast=SimpleNamespace(forecasts={(row['subject'],'player_points'):fitted for row in rows},
                                     selection=lambda row:fitted.market(row['line'],row['selection']))
            with patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP')):
                content=market_sections(snapshot,'Points',forecast,method=method)
            nodes=list(components(content))
            self.assertFalse(any(isinstance(node,dcc.Store) for node in nodes))
            self.assertFalse(any(isinstance(node,dcc.Graph) for node in nodes))
            headers=[json.loads(getattr(node,'data-prop-chart')) for node in nodes
                     if node.__class__.__name__=='Summary' and getattr(node,'data-prop-chart',None)]
            self.assertEqual({action['key'][2] for action in headers},{'player a','player b'})
            from app.sections.markets.prop_probability import _views
            for action in headers:
                result=_views[action['view']](action['key'],action)
                graph=next(node.figure for node in components(result) if isinstance(node,dcc.Graph))
                self.assertTrue(any(trace.name==method+' calculated distribution' for trace in graph.data))
                self.assertTrue(any(trace.name=='FanDuel implied' for trace in graph.data))
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
            headers=[json.loads(getattr(node,'data-prop-chart')) for node in components(shown)
                     if node.__class__.__name__=='Summary' and getattr(node,'data-prop-chart',None)]
            self.assertTrue(headers)
            self.assertFalse(any(isinstance(node,dcc.Graph) for node in components(shown)))
            from app.sections.markets.prop_probability import _views
            for action in headers:
                result=_views[action['view']](action['key'],action)
                graph=next(node.figure for node in components(result) if isinstance(node,dcc.Graph))
                trace=next(trace for trace in graph.data if trace.name==method+' calculated distribution')
                value=trace.y[list(trace.x).index(6.5)]
                self.assertAlmostEqual(value,fitted.market(6.5,'Over' if action['key'][4]=='gt' else 'Under').win)
            fixture.player_services[method].forecast.assert_called_once()

    def test_selected_player_chart_uses_new_model_when_the_quote_view_finishes(self):
        from app.sections.markets.prop_probability import sportsbook_line
        fixture=mispricing_fixtures.MispricingChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        category='Passing';location='#nfl-game-123';selected=fixture.saved['pulled_at']
        row=next(row for row in fixture.saved['rows'] if row['group']==category and sportsbook_line(row))
        key=sportsbook_line(row)
        fitted=UsageOpportunityForecast.from_samples([55,66,78])
        fixture.player_services['WHP'].forecast.return_value=SimpleNamespace(
            forecasts={(row['subject'],key[1]):fitted},
            selection=lambda row:fitted.market(row['line'],row['selection']))
        click=dict(key=key,click=1,context=[location,selected,category])
        callback=fixture.cb['market_prop_probabilities']
        pending=callback(click,None,location,selected,category,'WHP','market')
        self.assertIn('Calculating the selected forecast',str(pending))
        self.assertIn('FanDuel implied',str(pending))
        _,history=fixture.cb['show_saved_markets'](category,selected,location,None,'PPP','WHP',None)
        completed=callback(click,history,location,selected,category,'WHP','market')
        self.assertIn('WHP calculated distribution',str(completed))
        self.assertIn('FanDuel implied',str(completed))
        self.assertIsNone(callback(click,history,location,selected,'Game','WHP','market'))


if __name__=='__main__':main()
