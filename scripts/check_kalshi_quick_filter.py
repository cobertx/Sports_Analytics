"""Quick Filtering lifecycle and independently checked binary consensus math."""
from itertools import count
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from models.Game_Outcome.PPP.PPP import MarketProbability
from sports_analytics.data import DataSourceError
from sports_analytics.kalshi_analysis import FILTER,quick_calculate
import check_kalshi_analysis as fixtures
from check_dashboard_efficiency import post
from check_navigation_pipeline import node


class QuickLifecycleChecks(TestCase):
    def setUp(self):
        self.f=fixtures.KalshiFilterCallbacks();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.revision=[1]
        revision=patch('sports_analytics.kalshi_analysis.availability_revision',side_effect=lambda sport:list(self.revision))
        revision.start();self.addCleanup(revision.stop)

    def submit(self,click=1,**changes):
        values=dict(probability=0,edge=-100,methods=['PPP'],players=['WHP'],selected='live')
        values.update(changes)
        scan,_=self.f.cb['kalshi_quick_scan'](click,values['probability'],values['edge'],values['methods'],values['players'],
            self.f.game,FILTER,values['selected'],'kalshi',fixtures.view.href('NFL',self.f.game['id']))
        return json.loads(json.dumps(scan))

    def ready(self):self.f.app._filter_jobs.tasks.queue.tasks.join()

    def display(self,scan,**changes):
        values={'kalshi-quick-scan':scan,'kalshi-context':self.f.game,'kalshi-quick-game-methods':['PPP'],
            'kalshi-quick-player-methods':['WHP'],'kalshi-quick-probability':0,'kalshi-quick-edge':-100,
            'kalshi-snapshot':'live','kalshi-category':FILTER,'workspace-page':'kalshi',
            'market-url':fixtures.view.href('NFL',self.f.game['id'])}
        values.update(changes)
        return post(self.f.app,'kalshi_quick_results',values,'kalshi-quick-poll.n_intervals')

    def assert_terminal(self,response,status):
        updates=response['sideUpdate']
        self.assertEqual(updates['kalshi-quick-state']['data']['status'],status)
        self.assertTrue(updates['kalshi-quick-poll']['disabled'])
        self.assertFalse(updates['kalshi-quick-filter']['disabled'])
        self.assertNotIn('calculating in the background',updates['kalshi-quick-status']['children'])

    def test_pending_job_survives_availability_revision_and_discovery_metadata_updates(self):
        entered,release=Event(),Event();self.addCleanup(release.set)
        def blocked(*args,**kwargs):
            entered.set()
            if not release.wait(10):raise AssertionError('Quote fixture was not released')
            return self.f.snapshot
        self.f.exchange.all_quotes.side_effect=blocked
        scan=self.submit();self.assertTrue(entered.wait(2))
        response=self.display(scan)
        self.assertEqual(response['sideUpdate']['kalshi-quick-state']['data']['status'],'pending')
        self.assertIn('calculating in the background',str(response))
        self.revision=[2]
        changed=dict(self.f.game,title='PIT at NO',milestone_id='new metadata')
        response=self.display(scan,**{'kalshi-context':changed})
        self.assertEqual(response['sideUpdate']['kalshi-quick-state']['data']['status'],'pending')
        release.set();self.ready()
        response=self.display(scan,**{'kalshi-context':changed})
        self.assert_terminal(response,'complete')
        self.assertIn('Top 3 by edge',str(response['response']))
        self.assertNotIn('Click Quick Filtering to compare',str(response['response']))
        self.assertNotIn('snapshot',scan);self.assertLess(len(json.dumps(scan)),3000)
        self.f.engines['PPP'].forecast.assert_called_once()

    def test_a_completed_job_is_not_discarded_at_the_five_minute_revision_boundary(self):
        scan=self.submit();self.ready();self.revision=[999]
        response=self.display(scan)
        self.assert_terminal(response,'complete')
        self.assertIn('16 evaluated',response['sideUpdate']['kalshi-quick-status']['children'])
        legacy=dict(scan);legacy.pop('scope')
        self.assert_terminal(self.display(legacy),'complete')

    def test_failure_is_visible_and_retry_does_not_display_the_previous_success_as_new(self):
        scan=self.submit();self.ready();self.assert_terminal(self.display(scan),'complete')
        self.f.exchange.all_quotes.side_effect=DataSourceError('offline fixture')
        failed=self.submit(2);self.ready()
        response=self.display(failed);self.assert_terminal(response,'error')
        self.assertIn('offline fixture',str(response));self.assertIn('retry',str(response))
        self.assertNotIn('Top 3 by edge',str(response['response']))
        self.f.exchange.all_quotes.side_effect=None
        retried=self.submit(3);self.ready();self.assert_terminal(self.display(retried),'complete')

    def test_settings_game_snapshot_and_prompt_changes_cannot_show_the_wrong_results(self):
        scan=self.submit();self.ready()
        for changes in [{'kalshi-quick-probability':10},{'kalshi-quick-edge':0},
                        {'kalshi-quick-game-methods':['EPA']},{'kalshi-quick-player-methods':['PGE']},
                        {'kalshi-context':dict(self.f.game,id='another')},{'kalshi-snapshot':'archive.json'}]:
            response=self.display(scan,**changes)
            self.assert_terminal(response,'changed')
            self.assertIn('Click Quick Filtering',str(response['response']))
            self.assertNotIn('Top 3 by edge',str(response['response']))

    def test_zero_matches_and_unsupported_contracts_are_completed_results_with_explanations(self):
        scan=self.submit(probability=100);self.ready()
        response=self.display(scan,**{'kalshi-quick-probability':100})
        self.assert_terminal(response,'complete');self.assertIn('No qualifying markets',str(response))
        self.assertIn('0 qualifying selections',str(response))
        self.f.snapshot['rows']=[dict(self.f.snapshot['rows'][0],market_type='scalar')]
        scan=self.submit(2);self.ready();response=self.display(scan)
        self.assert_terminal(response,'complete');self.assertIn('2 unavailable',str(response))

    def test_empty_quotes_with_no_timestamp_and_display_errors_terminate_cleanly(self):
        self.f.snapshot.update(rows=[],fetched_at=None)
        scan=self.submit();self.ready();response=self.display(scan)
        self.assert_terminal(response,'complete')
        self.assertIn('timestamp unavailable',str(response));self.assertIn('No qualifying markets',str(response))
        with patch('sports_analytics.kalshi_analysis_callbacks.results',side_effect=ValueError('display fixture')):
            response=self.display(scan)
        self.assert_terminal(response,'error');self.assertIn('display fixture',str(response))

    def test_job_finishing_between_the_state_and_value_reads_is_not_reported_as_expired(self):
        scan=self.submit();self.ready();tasks=self.f.app._filter_jobs.tasks
        record=tasks.peek(scan['job'])
        with tasks.lock:tasks.values.pop(scan['job'])
        def finished(key):
            tasks.put(key,record)
            return dict(pending=False,error=None)
        with patch.object(tasks.queue,'state',side_effect=finished):response=self.display(scan)
        self.assert_terminal(response,'complete')

    def test_cold_background_quotes_are_resolved_inside_the_worker_before_forecasts(self):
        self.f.exchange.background=True
        scan=self.submit();self.ready();response=self.display(scan)
        self.assert_terminal(response,'complete')
        self.f.exchange.all_quotes.assert_called_once_with(self.f.game,refresh=True,save=False)
        self.f.exchange.history.return_value=['archive.json']
        self.f.exchange.saved.return_value=self.f.snapshot
        scan=self.submit(2,selected='archive.json');self.ready()
        response=self.display(scan,**{'kalshi-snapshot':'archive.json'})
        self.assert_terminal(response,'complete')
        self.f.exchange.all_quotes.assert_called_once()
        self.f.exchange.saved.assert_called_once_with(self.f.game,'Game Lines','archive.json')

    def test_evicted_job_reports_expiry_and_client_polling_does_not_restart_terminal_jobs(self):
        scan=self.submit();self.ready()
        with self.f.app._filter_jobs.tasks.lock:self.f.app._filter_jobs.tasks.values.pop(scan['job'])
        response=self.display(scan);self.assert_terminal(response,'error');self.assertIn('expired',str(response))
        if not node():self.skipTest('Node runtime unavailable')
        meta=next(m for m in self.f.app._callback_list if isinstance(m['output'],str) and
                  'kalshi-quick-poll.disabled' in m['output'] and m.get('clientside_function'))
        config=dict(inline=self.f.app._inline_scripts,client=meta['clientside_function'])
        script=Path(__file__).with_name('check_kalshi_quick_lifecycle.js')
        result=subprocess.run([node(),str(script)],input=json.dumps(config),text=True,capture_output=True,check=True)
        self.assertIn('Passed',result.stdout)


class QuickProbabilityChecks(TestCase):
    def setUp(self):
        self.game=fixtures.game();self.snapshot=fixtures.snapshot()
        self.snapshot['rows']=[dict(self.snapshot['rows'][0],yes_ask=.5,no_ask=.125)]
        self.probabilities={'PPP':.875,'EPA':.75,'ELO':.9375}
        self.sources=[]
        def forecast(source,method):
            self.sources.append((source,method))
            p=self.probabilities[method]
            return SimpleNamespace(total=lambda line,side:MarketProbability(p,1-p,0),
                spread=lambda name,line:MarketProbability(p,1-p,0),home_team='NO',away_team='PIT',diagnostics={}),None
        self.forecast=Mock(side_effect=forecast)
        self.player=Mock(return_value=(SimpleNamespace(selection=lambda target:MarketProbability(.875,.125,0)),None,{}))

    def run_scan(self,criteria=None,methods=None,overrides=None):
        return quick_calculate(self.game,self.snapshot,'live',methods or ['PPP','EPA','ELO'],['WHP'],
            criteria or dict(probability=0,edge=-1),self.forecast,self.player,overrides)

    def test_consensus_uses_each_asks_own_probability_and_lowest_method_edge_with_strict_thresholds(self):
        ranked,errors=self.run_scan()
        yes=next(item for item in ranked['matches'] if item['side']=='yes')
        no=next(item for item in ranked['matches'] if item['side']=='no')
        self.assertEqual((yes['probability'],yes['implied'],yes['edge']),(.75,.5,.25))
        self.assertEqual((no['probability'],no['implied'],no['edge']),(.0625,.125,-.0625))
        self.assertEqual(len(yes['estimates']),3);self.assertEqual(errors,[])
        for criteria in [dict(probability=.75,edge=0),dict(probability=0,edge=.25)]:
            ranked,_=self.run_scan(criteria)
            self.assertFalse(any(item['side']=='yes' for item in ranked['matches']))
        self.assertTrue(all(source['pulled_at']=='2026-10-23T20:00:00+00:00' for source,_ in self.sources))

    def test_availability_changes_during_fitting_cannot_erase_calculated_probabilities(self):
        revisions=count()
        with patch('sports_analytics.kalshi_analysis.availability_revision',side_effect=lambda sport:[next(revisions)]) as revision:
            ranked,errors=self.run_scan()
        self.assertEqual(ranked['evaluated'],2);self.assertEqual(len(ranked['matches']),2)
        self.assertEqual(revision.call_count,1);self.assertEqual(errors,[])

    def test_unavailable_asks_and_missing_selected_methods_are_excluded(self):
        self.snapshot['rows'][0]['yes_ask']=None
        ranked,_=self.run_scan()
        self.assertEqual(ranked['evaluated'],1);self.assertEqual(ranked['unavailable'],1)
        self.assertTrue(all(item['side']=='no' for item in ranked['matches']))
        self.forecast.side_effect=lambda source,method:(None,'Missing EPA history') if method=='EPA' else (
            SimpleNamespace(total=lambda line,side:MarketProbability(.875,.125,0),diagnostics={}),None)
        ranked,errors=self.run_scan()
        self.assertEqual(ranked['matches'],[]);self.assertEqual(ranked['unavailable'],2)
        self.assertIn('EPA: Missing EPA history',errors)

    def test_player_integer_thresholds_and_spread_sides_retain_binary_equality_mass(self):
        player=dict(self.snapshot['rows'][0],ticker='PLAYER',event_ticker='KXNFLPASSYDS-PLAYER',
            event_title='Player One: Passing yards',category='Passing Props',line=20,strike_type='greater_or_equal')
        spread=dict(self.snapshot['rows'][0],ticker='SPREAD',event_ticker='KXNFLSPREAD-GAME',
            yes_label='Pittsburgh wins by over 3.5 points',line=3.5)
        self.snapshot['rows']=[player,spread]
        ranked,errors=self.run_scan(methods=['PPP'])
        values={(item['row']['ticker'],item['side']):item['probability'] for item in ranked['matches']}
        self.assertEqual(values,{('PLAYER','yes'):.875,('PLAYER','no'):.125,('SPREAD','yes'):.875,('SPREAD','no'):.125})
        target=self.player.call_args.args[0]['rows'][0]
        self.assertEqual((target['line'],target['selection']),(19.5,'Over'))
        self.assertEqual(self.forecast.call_args.args[0]['rows'][0]['line'],-3.5)
        self.player.assert_called_once();self.forecast.assert_called_once();self.assertEqual(errors,[])


if __name__=='__main__':main()
