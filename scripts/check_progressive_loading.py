"""Offline HTTP regressions for independently ready pages and background work."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from threading import Event
from time import perf_counter
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.callback_fixtures import callback_functions
from check_dashboard_efficiency import post
import check_kalshi_analysis as kalshi
import check_market_summary as summary
import check_performance as history


class ProgressiveLoadingChecks(TestCase):
    def fixture(self, kind):
        fixture=kind();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.app._page_tasks.queue.shutdown)
        self.addCleanup(fixture.app._career_tasks.queue.shutdown)
        return fixture

    def blocked(self, value):
        entered,release=Event(),Event();self.addCleanup(release.set)
        def work(*args,**kwargs):
            entered.set()
            if not release.wait(8):raise AssertionError('Controlled calculation was not released')
            return deepcopy(value)
        return work,entered,release

    def test_shell_and_primary_history_have_no_optional_result_dependencies(self):
        f=self.fixture(history.PagePerformanceChecks)
        inputs=lambda key:{item['id'] for item in f.app.callback_map[key]['inputs']}
        self.assertEqual(inputs('app-shell.className'),{'market-url','sport','workspace-page'})
        self.assertNotIn('career-trend',inputs('historical-render-scope.data'))
        self.assertNotIn('career-job',inputs('historical-render-scope.data'))
        self.assertNotIn('kalshi-models',inputs('kalshi-detail.className'))
        self.assertNotIn('summary-models',inputs('app-shell.className'))
        self.assertNotIn('summary-models',inputs('summary-market-sections.className'))
        self.assertNotIn('polymarket-quotes',inputs('kalshi-page.className'))
        from check_mispricing import components
        for component in components(f.app.layout):
            if getattr(component,'id',None) in ('career-poll','kalshi-model-poll','summary-model-poll'):
                self.assertEqual(component.n_intervals,0,'First browser request must use the background path')

    def test_back_to_unchanged_history_reveals_the_retained_result(self):
        from check_navigation_pipeline import node,run_client
        if not node():self.skipTest('Node unavailable')
        f=self.fixture(history.PagePerformanceChecks)
        route='#historical-player';scope=dict(route=route,sport='NBA',ready=True)
        self.assertIn('market-url',{item['id'] for item in f.app.callback_map['historical-results.className']['inputs']})
        result=run_client(f.app,steps=[],references=[
            dict(output='historical-results.className',args=[scope,route,'NBA'],expected='historical-results'),
            dict(output='historical-results.className',args=[scope,'#historical-team','NBA'],expected='historical-results history-view-pending'),
            dict(output='historical-results.className',args=[scope,route,'NFL'],expected='historical-results history-view-pending'),
            dict(output='summary-market-sections.className',args=[dict(id='v1',sport='NFL',game=dict(id='123')),['v1','PPP','WHP','All Markets'],'123','NFL','All Markets'],expected=''),
            dict(output='summary-market-sections.className',args=[dict(id='v1',sport='NFL',game=dict(id='123')),['v1','PPP','WHP','All Markets'],'456','NFL','All Markets'],expected='summary-quotes-pending'),
            dict(output='career-poll.disabled',args=['historical',dict(pending=True),'NBA'],expected=False),
            dict(output='career-poll.disabled',args=['historical',dict(pending=True),'RESEARCH'],expected=True),
            dict(output='kalshi-model-poll.disabled',args=['kalshi',dict(pending=True),'NBA'],expected=False),
            dict(output='kalshi-model-poll.disabled',args=['kalshi',dict(pending=True),'PNL'],expected=True),
            dict(output='summary-model-poll.disabled',args=['market-summary',dict(pending=False),'NFL'],expected=True)])
        self.assertEqual(result['checks'],10)

    def test_kalshi_http_returns_pending_without_waiting_and_reuses_exact_completed_result(self):
        f=self.fixture(kalshi.KalshiFilterCallbacks)
        route=kalshi.view.href('NFL',f.game['id'])
        expected=f.cb['kalshi_models'](f.snapshot,None,f.game,'PPP','WHP','live','kalshi','Game Lines',None,route)
        work,entered,release=self.blocked(expected)
        values={'workspace-page':'kalshi','sport':'NFL','market-url':route,'kalshi-context':f.game,
            'kalshi-quotes':f.snapshot,'kalshi-game-method':'PPP','kalshi-player-method':'WHP',
            'kalshi-snapshot':'live','kalshi-category':'Game Lines','kalshi-model-poll':0}
        with patch('sports_analytics.kalshi_analysis_callbacks.calculate',side_effect=work) as fit:
            start=perf_counter();reply=post(f.app,'kalshi_models',values,'kalshi-quotes.data')
            elapsed=(perf_counter()-start)*1000
            self.assertLess(elapsed,1000);self.assertTrue(entered.wait(1))
            pending=reply['response']['kalshi-models']['data'];self.assertTrue(pending['pending'])
            self.assertLess(len(json.dumps(pending)),2000);self.assertNotIn('predictions',pending)
            values['kalshi-models']=pending
            post(f.app,'kalshi_models',values,'kalshi-model-poll.n_intervals')
            self.assertEqual(fit.call_count,1)
            quotes=post(f.app,'kalshi_table',values,'kalshi-quotes.data')
            self.assertIn('kalshi-market-sections',quotes['response'])
            values['market-url']='#nfl-kalshi-game-KXNFLGAME-OTHER'
            self.assertFalse(post(f.app,'kalshi_models',values,'kalshi-model-poll.n_intervals').get('response'))
            values['market-url']=route;release.set();f.app._page_tasks.queue.tasks.join()
            final=post(f.app,'kalshi_models',values,'kalshi-model-poll.n_intervals')['response']['kalshi-models']['data']
            self.assertFalse(final['pending']);self.assertEqual(fit.call_count,1)
            self.assertEqual(final['predictions'],expected['predictions']);self.assertEqual(final['context'],expected['context'])
            print(f'Kalshi HTTP submit: {elapsed:.1f} ms while forecast blocked; quote HTML remained available.')

    def test_summary_quotes_render_while_models_are_blocked_and_recalculation_retries(self):
        f=self.fixture(summary.SummaryCallbackChecks)
        expected=f.cb['summary_models'](f.data,'PPP','WHP',0,'market-summary','NFL','123',None)[0]
        work,entered,release=self.blocked(expected)
        values={'workspace-page':'market-summary','sport':'NFL','market-url':'#nfl-market-summary',
            'summary-quotes':f.data,'summary-game':'123','summary-game-method':'PPP','summary-player-method':'WHP',
            'summary-recalculate':0,'summary-model-poll':0,'summary-category':'All Markets'}
        with patch('sports_analytics.market_summary_callbacks.analysis.calculate',side_effect=work) as fit:
            start=perf_counter();pending=post(f.app,'summary_models',values,'summary-quotes.data')['response']['summary-models']['data']
            elapsed=(perf_counter()-start)*1000
            self.assertLess(elapsed,1000);self.assertTrue(entered.wait(1));self.assertTrue(pending['pending'])
            self.assertNotIn('rows',pending);values['summary-models']=pending
            rendered=post(f.app,'summary_sections',values,'summary-quotes.data')
            self.assertIn('summary-market-sections',rendered['response'])
            post(f.app,'summary_models',values,'summary-model-poll.n_intervals');self.assertEqual(fit.call_count,1)
            release.set();f.app._page_tasks.queue.tasks.join()
            completed=post(f.app,'summary_models',values,'summary-model-poll.n_intervals')['response']['summary-models']['data']
            self.assertEqual(completed['rows'],expected['rows']);self.assertFalse(completed['pending'])
            print(f'Market Summary HTTP submit: {elapsed:.1f} ms while forecast blocked; quote sections remained available.')

    def test_career_http_is_independent_and_deduplicates_pending_work(self):
        f=self.fixture(history.PagePerformanceChecks);dataset=f.load()[0]
        career=SimpleNamespace(frame=pd.DataFrame(dict(season=[2024,2025],value=[10.,20.],team=['BOS','BOS'])),
            phase='regular',source='fixture',searched=[2024,2025],failed=[])
        work,entered,release=self.blocked(career);f.career.load.side_effect=work
        values={'workspace-page':'historical','sport':'NBA','market-url':'#historical-player','dataset':dataset,
            'entity':'001','metric':'pts','career-poll':0}
        start=perf_counter();reply=post(f.app,'analyze_career',values,'entity.value');elapsed=(perf_counter()-start)*1000
        self.assertLess(elapsed,1000);self.assertTrue(entered.wait(1))
        job=reply['sideUpdate']['career-job']['data'];self.assertTrue(job['pending']);values['career-job']=job
        self.assertTrue(reply['response']['career-trend']['figure']['layout']['meta']['pending'])
        post(f.app,'analyze_career',values,'career-poll.n_intervals');self.assertEqual(f.career.load.call_count,1)
        chart=f.cb['analyze'](dataset,'001','pts',3,None,'#historical-player','historical')[0]
        self.assertEqual(list(chart.data[0].y),list(range(20,28)))
        release.set();f.app._career_tasks.queue.tasks.join()
        final=post(f.app,'analyze_career',values,'career-poll.n_intervals')
        self.assertFalse(final['sideUpdate']['career-job']['data']['pending'])
        self.assertNotIn('pending',final['response']['career-trend']['figure']['layout']['meta'])
        self.assertEqual(f.career.load.call_count,1)
        print(f'Career HTTP submit: {elapsed:.1f} ms while source blocked; main performance chart remained ready.')

    def test_failed_career_stops_polling_and_explicit_retry_can_recover(self):
        f=self.fixture(history.PagePerformanceChecks);dataset=f.load()[0]
        f.career.load.side_effect=RuntimeError('Controlled source failure')
        values={'workspace-page':'historical','sport':'NBA','market-url':'#historical-player','dataset':dataset,
            'entity':'001','metric':'pts','career-poll':0,'career-retry':0}
        first=post(f.app,'analyze_career',values,'entity.value');values['career-job']=first['sideUpdate']['career-job']['data']
        f.app._career_tasks.queue.tasks.join()
        failed=post(f.app,'analyze_career',values,'career-poll.n_intervals')
        self.assertFalse(failed['sideUpdate']['career-job']['data']['pending'])
        self.assertEqual(failed['sideUpdate']['career-job']['data']['status'],'error')
        self.assertIn('Controlled source failure',failed['response']['career-status']['children'])
        f.career.load.side_effect=None
        f.career.load.return_value=SimpleNamespace(frame=pd.DataFrame(dict(season=[2025],value=[20.],team=['BOS'])),
            phase='regular',source='fixture',searched=[2025],failed=[])
        values['career-retry']=1
        pending=post(f.app,'analyze_career',values,'career-retry.n_clicks')
        values['career-job']=pending['sideUpdate']['career-job']['data'];f.app._career_tasks.queue.tasks.join()
        completed=post(f.app,'analyze_career',values,'career-poll.n_intervals')
        self.assertEqual(completed['sideUpdate']['career-job']['data']['status'],'complete')
        self.assertEqual(f.career.load.call_count,2)

    def test_summary_failure_is_terminal_and_recalculate_uses_a_new_job(self):
        f=self.fixture(summary.SummaryCallbackChecks)
        expected=f.cb['summary_models'](f.data,'PPP','WHP',0,'market-summary','NFL','123',None)[0]
        values={'workspace-page':'market-summary','sport':'NFL','market-url':'#nfl-market-summary','summary-quotes':f.data,
            'summary-game':'123','summary-game-method':'PPP','summary-player-method':'WHP',
            'summary-recalculate':0,'summary-model-poll':0}
        with patch('sports_analytics.market_summary_callbacks.analysis.calculate',side_effect=RuntimeError('Controlled fit failure')):
            first=post(f.app,'summary_models',values,'summary-quotes.data');values['summary-models']=first['response']['summary-models']['data']
            f.app._page_tasks.queue.tasks.join()
            failed=post(f.app,'summary_models',values,'summary-model-poll.n_intervals')['response']['summary-models']['data']
            self.assertFalse(failed['pending']);self.assertEqual(failed['status'],'error')
        values.update({'summary-models':failed,'summary-recalculate':1})
        retried=post(f.app,'summary_models',values,'summary-recalculate.n_clicks')['response']['summary-models']['data']
        self.assertNotEqual(failed['job'],retried['job']);values['summary-models']=retried
        f.app._page_tasks.queue.tasks.join()
        complete=post(f.app,'summary_models',values,'summary-model-poll.n_intervals')['response']['summary-models']['data']
        self.assertFalse(complete['pending']);self.assertEqual(complete['rows'],expected['rows'])


if __name__=='__main__':main()
