"""Offline request identity, archived return, and scoped failure recovery."""
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest import TestCase, main
from unittest.mock import patch

import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sports_analytics.market_navigation import remembered_market
from sports_analytics.history_scope import analysis_scope
from sports_analytics.data import DataSourceError
from sports_analytics.page_cache import PageTasks
from models.season_weights import POLICY_VERSION
from scripts.callback_fixtures import callback_functions
from dash import no_update
import check_performance as history_fixtures
import check_kalshi as exchange_fixtures
import check_mispricing as filter_fixtures


class ScopedHistoryChecks(TestCase):
    def setUp(self):
        self.f=history_fixtures.PagePerformanceChecks();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_history_identity_covers_query_revision_and_controls_without_changing_observations(self):
        d=self.f.load()[0];route='#historical-player'
        original=self.f.cb['analyze'](d,'001','pts',3,None,route,'historical')
        changed=self.f.cb['analyze'](d,'001','pts',5,None,route,'historical')
        self.assertEqual(list(original[0].data[0].y),list(changed[0].data[0].y))
        self.assertEqual(original[0].layout.meta['analysis_scope'],analysis_scope(d,'001','pts',3,None,route))
        self.assertNotEqual(original[0].layout.meta['analysis_scope'],changed[0].layout.meta['analysis_scope'])
        self.assertEqual(original[0].layout.uirevision,changed[0].layout.uirevision)
        self.assertEqual(original[0].layout.meta['analysis_scope']['revision'],d['revision'])

    def test_career_results_have_independent_identity_and_keep_actual_values(self):
        d=self.f.load()[0]
        self.f.career.load.return_value=SimpleNamespace(frame=pd.DataFrame(dict(season=[2024,2025],value=[10.,20.],team=['BOS','BOS'])),
            phase='regular',source='fixture',searched=[],failed=[])
        figure,status=self.f.cb['analyze_career'](d,'001','pts','historical','#historical-player','NBA')
        self.assertEqual(list(figure.data[0].y),[10.,20.])
        scope=figure.layout.meta['career_scope']
        self.assertEqual((scope['entity'],scope['metric'],scope['phase']),('001','pts','regular'))
        self.assertNotIn('window',scope)
        self.f.career.load.side_effect=DataSourceError('offline fixture')
        failed,status=self.f.cb['analyze_career'](d,'001','pts','historical','#historical-player','NBA')
        self.assertEqual(failed.layout.meta['career_scope'],scope)
        self.assertIn('offline fixture',status)


class ProviderReturnChecks(TestCase):
    def test_nine_scopes_and_legacy_state_are_validated_independently(self):
        entries={}
        for sport in ['NBA','NFL','NCAAF']:
            for provider in ['market','kalshi','polymarket']:
                route='#'+sport.lower()+('-game-123' if provider=='market' else '-'+provider+'-game-'+('KX'+sport+'GAME-TEST' if provider=='kalshi' else '456'))
                entries[provider+':'+sport]=dict(route=route,snapshot='saved',category='Game Lines')
        state=dict(v=2,entries=entries)
        for key,entry in entries.items():
            provider,sport=key.split(':')
            self.assertEqual(remembered_market(state,sport,entry['route'],provider),entry)
            self.assertIsNone(remembered_market(state,sport,'#different',provider))
        legacy=dict(NFL=dict(route='#nfl-game-123',snapshot='saved'))
        self.assertEqual(remembered_market(legacy,'NFL'),legacy['NFL'])
        self.assertIsNone(remembered_market(legacy,'NFL',provider='kalshi'))
        entries['kalshi:NFL']=dict(route='#nfl-kalshi-game-KXNBAGAME-WRONG')
        self.assertIsNone(remembered_market(state,'NFL',provider='kalshi'))

    def setUp(self):
        self.f=exchange_fixtures.KalshiChecks();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.app,self.cb=self.f.app()

    def test_return_reads_the_selected_archive_without_pulling_live_quotes(self):
        saved=self.f.service.quotes(self.f.game,save=True)
        filename=self.f.service.history(self.f.game,'Game Lines')[0]
        route='#nfl-kalshi-game-'+self.f.game['id']
        state=dict(v=2,entries={'kalshi:NFL':dict(route=route,snapshot=filename,category='Game Lines')})
        with (patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='nav-kalshi')),
              patch.object(self.f.service,'quotes',side_effect=AssertionError('No new live quote call'))):
            result=self.cb['kalshi_quotes']('kalshi','NFL',self.f.game,'Game Lines','live',0,0,5,
                route,None,None,None,None,state)
        self.assertEqual(result[0]['rows'],saved['rows'])
        self.assertEqual(result[3],filename)
        self.assertIn('SAVED SNAPSHOT',result[1])

    def test_failure_keeps_only_matching_quotes_and_terminates_pending_state(self):
        route='#nfl-kalshi-game-'+self.f.game['id']
        previous={**self.f.service.quotes(self.f.game),'selection':'live'}
        with (patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-quote-timer')),
              patch.object(self.f.service,'quotes',side_effect=DataSourceError('offline fixture'))):
            result=self.cb['kalshi_quotes']('kalshi','NFL',self.f.game,'Game Lines','live',1,0,5,route,None,None,previous)
            self.assertEqual(result[0]['rows'],[{**row,'quote_stale':True,'quote_fetched_at':previous['fetched_at']} for row in previous['rows']])
            self.assertFalse(result[0]['updating'])
            self.assertIn('previously loaded',result[1])
            different={**previous,'game_id':'different'}
            result=self.cb['kalshi_quotes']('kalshi','NFL',self.f.game,'Game Lines','live',1,0,5,route,None,None,different)
            self.assertEqual(result[0]['rows'],[])
            rendered=self.cb['kalshi_table'](result[0],self.f.game,'Game Lines',False,False,None,'kalshi',route)
            self.assertEqual(rendered[-1]['context'],['NFL',self.f.game['id'],'Game Lines'])
            self.assertIn('could not be loaded',str(rendered[3]))
            model=self.cb['kalshi_models'](result[0],None,self.f.game,'PPP','WHP','live','kalshi','Game Lines',None,route)
            self.assertEqual(model['context'][0:2],['NFL',self.f.game['id']])
            self.assertIn('offline fixture',model['errors'][0])

    def test_failed_calculation_is_a_scoped_terminal_result(self):
        snapshot=self.f.service.quotes(self.f.game)
        with patch('sports_analytics.kalshi_analysis_callbacks.calculate',side_effect=DataSourceError('model input offline')):
            model=self.cb['kalshi_models'](snapshot,None,self.f.game,'PPP','WHP','live','kalshi','Game Lines',None)
        self.assertEqual(model['predictions'],{})
        self.assertIn('model input offline',model['errors'])
        self.assertEqual(model['context'][-3:],['live','PPP','WHP'])


class RetainedRefreshChecks(TestCase):
    def test_same_selection_pending_refresh_retains_completed_content_without_refitting(self):
        f=filter_fixtures.MispricingChecks();f.setUp();self.addCleanup(f.doCleanups)
        callback=callback_functions(f.app)['show_saved_markets']
        route='#nfl-game-123';selection=f.saved['pulled_at']
        previous=dict(context=[route,selection],view_key=['Game','PPP','WHP',None,POLICY_VERSION],rendered=True)
        with (patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-applied-criteria')),
              patch.object(PageTasks,'read',return_value=(None,True,None))):
            content,state=callback('Game',selection,route,None,'PPP','WHP',previous,'market')
        self.assertIs(content,no_update)
        self.assertTrue(state['pending'] and state['keep_visible'])
        f.game_services['PPP'].forecast.assert_not_called()


if __name__=='__main__':main()
