"""Offline consensus, explicit execution and scoped cross-provider results."""
from pathlib import Path
import json
import sys
from copy import deepcopy
from threading import Event
from unittest import TestCase,main
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash.exceptions import PreventUpdate
from dash import no_update
from models.Game_Outcome.PPP.PPP import MarketProbability
from sports_analytics.summary_filtering import calculate_filter
from app.sections.market_summary.market_summary_view import layout,QUICK
from check_market_summary import bundle,GameForecast,PlayerForecast
import check_market_summary as fixtures
from check_kalshi import components


class SummaryFilteringChecks(TestCase):
    def test_all_methods_reuse_one_quote_preparation(self):
        from sports_analytics import market_summary
        with patch.object(market_summary,'entries',wraps=market_summary.entries) as entries:
            result=self.run_filter(methods=['PPP','EPA','ELO'],players=['WHP','UO','OAM','PGE'])
        entries.assert_called_once()
        self.assertTrue(result['matches'])
        self.assertEqual(self.game.call_count,3)
        self.assertEqual(self.player.call_count,4)

    def run_filter(self,criteria=None,methods=None,players=None,game=None,player=None):
        self.game=game or Mock(return_value=(GameForecast(),None))
        self.player=player or Mock(return_value=(PlayerForecast(),None,{}))
        return calculate_filter(bundle(),methods or ['PPP'],players or ['WHP'],
            criteria or dict(probability=.5,edge=0),self.game,self.player)

    def test_columns_logos_and_category_tabs_have_explicit_units(self):
        nodes=list(components(layout()));tabs=next(c for c in nodes if getattr(c,'id',None)=='summary-category')
        self.assertEqual(tabs.value,'Game Lines')
        self.assertEqual([tab.value for tab in tabs.children],['Game Lines','Player Props',QUICK,'Other Markets'])

        self.assertEqual([tab.value for tab in tabs.children if tab.selected_className=='market-filter-tab-selected'],[QUICK])

    def test_settlement_rules_and_every_selected_method_are_preserved(self):
        ranked=self.run_filter(methods=['PPP','EPA'],players=['WHP','PGE'])
        self.assertEqual([call.args[1] for call in self.game.call_args_list],['PPP','EPA'])
        self.assertEqual([call.args[1] for call in self.player.call_args_list],['WHP','PGE'])
        quotes={(r['provider'],r['index'],r['side']):r for r in ranked['matches']}
        self.assertAlmostEqual(quotes['Kalshi',0,'yes']['probability'],.7)
        self.assertEqual(quotes['Kalshi',0,'yes']['method'],'PPP / EPA')
        self.assertEqual(len(quotes['Kalshi',0,'yes']['estimates']),2)
        self.assertTrue(all(r['edge']>0 and r['probability']>.5 for r in ranked['matches']))
        self.assertTrue(all(r['edge']>=ranked['matches'][i+1]['edge'] for i,r in enumerate(ranked['matches'][:-1])))

    def test_exact_thresholds_are_excluded(self):
        ranked=self.run_filter(dict(probability=.7,edge=0))
        self.assertFalse(any(r['provider']=='Kalshi' and r['index']==0 and r['side']=='yes' for r in ranked['matches']))

    def test_missing_method_cannot_qualify_and_other_market_types_continue(self):
        game=Mock(side_effect=lambda snapshot,method:(None,'Unavailable') if method=='EPA' else (GameForecast(),None))
        ranked=self.run_filter(methods=['PPP','EPA'],game=game)
        self.assertTrue(ranked['matches']);self.assertTrue(all(r['category']=='Player Props' for r in ranked['matches']))
        self.assertIn('EPA: Unavailable',ranked['errors']);self.assertGreater(ranked['unavailable'],0)

    def test_disagreeing_methods_use_conservative_consensus(self):
        class Low(GameForecast):
            def total(self,line,side):return MarketProbability(.55,.35,.1)
        game=Mock(side_effect=lambda snapshot,method:(Low() if method=='EPA' else GameForecast(),None))
        ranked=self.run_filter(dict(probability=.6,edge=0),methods=['PPP','EPA'],game=game)
        self.assertFalse(any(r['provider']=='Kalshi' and r['index']==0 for r in ranked['matches']))

    def fixture(self):
        availability=patch('app.sections.market_summary.summary_filter_callbacks.availability_revision',return_value='fixture-availability')
        availability.start();self.addCleanup(availability.stop)
        fixture=fixtures.SummaryCallbackChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.app._summary_quick_jobs.queue.shutdown)
        return fixture

    def test_only_button_submission_starts_work_and_controls_are_state(self):
        f=self.fixture();meta=next(m for m in f.app.callback_map.values() if m.get('callback') and m['callback'].__wrapped__.__name__=='summary_quick_submit')
        self.assertEqual([i['id'] for i in meta['inputs']],['summary-quick-run'])
        args=[['PPP'],['WHP'],50,0,f.data,'123',QUICK,'market-summary','NFL']
        with self.assertRaises(PreventUpdate):f.cb['summary_quick_submit'](0,*args)
        f.engines['PPP'].forecast.assert_not_called()
        request=f.cb['summary_quick_submit'](1,*args);f.app._summary_quick_jobs.queue.tasks.join()
        result=f.cb['summary_quick_results'](request,f.data,QUICK,'123','market-summary','NFL',1,None)
        self.assertEqual(result[2]['status'],'complete',result[1]);self.assertIn('Top 3 by edge',str(result[0]))
        self.assertEqual(getattr(result[0],'data-quick-sequence'),'1')
        f.fd.pull.assert_not_called();f.kalshi.all_quotes.assert_not_called();f.poly.all_quotes.assert_not_called()
        with self.assertRaises(PreventUpdate):f.cb['summary_quick_results'](request,f.data,QUICK,'123','market-summary','NFL',2,result[2])
        changed=f.cb['summary_quick_results'](request,{**f.data,'id':'new'},QUICK,'123','market-summary','NFL',3,result[2])
        self.assertEqual(changed[2]['status'],'changed')

    def test_invalid_thresholds_do_not_start_forecasts(self):
        f=self.fixture()
        for probability,edge in [(101,0),(50,float('nan')),(-1,0)]:
            request=f.cb['summary_quick_submit'](1,['PPP'],['WHP'],probability,edge,f.data,'123',QUICK,'market-summary','NFL')
            self.assertIn('error',request)
        f.engines['PPP'].forecast.assert_not_called()

    def test_summary_and_draftkings_are_tracked_and_restore_their_submitted_quotes(self):
        from check_dashboard_efficiency import post
        f=self.fixture()
        draft={**f.data,'id':'draft-fixture','mode':'draftkings',
               'providers':{'DraftKings':f.data['providers']['FanDuel']}}
        client=f.app.server.test_client()
        values={'summary-quick-run':1,'summary-quick-game-methods':['PPP'],'summary-quick-player-methods':['WHP'],
                'summary-quick-probability':10,'summary-quick-edge':-100,'summary-game':'123',
                'workspace-page':'market-summary','summary-category':QUICK,'sport':'NFL','summary-quotes':f.data}
        def submit(bundle):
            values['summary-quotes']=bundle
            return post(f.app,'summary_quick_submit',values,'summary-quick-run.n_clicks',client=client
                        )['response']['summary-quick-request']['data']
        summary=submit(f.data)
        values['summary-quick-run']=2
        draft_request=submit(draft)
        self.assertIs(f.app._summary_quick_jobs,f.app._filter_jobs.tasks)
        f.app._summary_quick_jobs.queue.tasks.join()
        monitor=post(f.app,'poll_quick_monitor',{'quick-monitor-timer':1},
                     'quick-monitor-timer.n_intervals',client=client)['response']
        self.assertEqual([(job['provider'],job['status']) for job in monitor['quick-monitor-state']['data']['jobs']],
                         [('DraftKings','complete'),('Market Summary','complete')])
        values.update({'summary-quotes':{**f.data,'id':'new-quote-bundle'},'summary-quick-request':draft_request})
        result=post(f.app,'summary_quick_results',values,'summary-quotes.data',client=client)['response']
        self.assertEqual(result['summary-quick-completed']['data']['job'],summary['job'])
        self.assertIn('Using the quotes and settings',str(result['summary-quick-status']))
        self.assertIn('Top 3 by edge',str(result['summary-quick-results']))

    def test_quick_player_row_opens_full_ladder_with_each_cached_method(self):
        f=self.fixture()
        quotes=f.data['providers']['FanDuel']['snapshot']['rows']
        quotes.append({**quotes[1],'line':205.5,'odds':-500})
        request=f.cb['summary_quick_submit'](1,['PPP'],['WHP','UO'],50,0,f.data,'123',QUICK,'market-summary','NFL')
        f.app._summary_quick_jobs.queue.tasks.join()
        results,_,done,_=f.cb['summary_quick_results'](request,f.data,QUICK,'123','market-summary','NFL',1,None)
        self.assertEqual(done['status'],'complete')
        self.assertEqual(json.loads(getattr(results,'data-quick-scope')),['#nfl-market-summary','123'])
        rows=fixtures.native_dom(results).find('tr')
        for row in rows:
            if 'data-prop-chart' in row.attrs:
                selection=row.children[1]
                actions=selection.find('div','market-line-actions')
                self.assertEqual(len(actions),1)
                self.assertEqual(list(map(str,actions[0].children)),['Historical Data','Market Structure'])
        selection=next(json.loads(row.attrs['data-prop-chart']) for row in rows if 'data-prop-chart' in row.attrs)
        self.assertEqual(selection['job'],request['job'])
        self.assertTrue(any(row.find('button','prop-chart-button') for row in rows))
        calls={method:f.players[method].forecast.call_count for method in ('WHP','UO')}

        output='summary-prop-result.children'
        meta=f.app.callback_map[output]
        values={'summary-prop-request':{**selection,'click':1},'summary-quotes':f.data,'summary-models':None,
                'summary-category':QUICK,'summary-game':'123','summary-game-method':'PPP','summary-player-method':'WHP',
                'summary-quick-request':request,'summary-quick-completed':done,'sport':'NFL','workspace-page':'market-summary'}
        with f.app.server.test_client() as client:
            response=client.post('/_dash-update-component',json=dict(output=output,
                outputs=dict(id='summary-prop-result',property='children'),changedPropIds=['summary-prop-request.data-selection'],
                inputs=[dict(item,value=values[item['id']]) for item in meta['inputs']],
                state=[dict(item,value=values[item['id']]) for item in meta['state']]))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        panel=response.get_json()['response']['summary-prop-result']['children']
        self.assertEqual(panel['props']['data-prop-chart-tab'],QUICK)
        graph=next(child for child in panel['props']['children'] if child and child['type']=='Graph')
        traces={trace['name']:trace for trace in graph['props']['figure']['data']}
        self.assertTrue({'WHP forecast','UO forecast','FanDuel implied'} <= set(traces))
        self.assertIn(205.5,traces['FanDuel implied']['x'])
        self.assertEqual(calls,{method:f.players[method].forecast.call_count for method in calls})
        f.fd.pull.assert_not_called()

    def test_latest_criteria_action_controls_chart_highlights_without_refitting(self):
        f=self.fixture()
        apply=f.cb['summary_chart_criteria']
        applied,_=apply(1,50,0,'123','NFL','market-summary','Game Lines')
        invalid,error=apply(2,101,0,'123','NFL','market-summary','Game Lines')
        self.assertIs(invalid,no_update);self.assertIn('Previous criteria remain applied',error)
        f.engines['PPP'].forecast.assert_not_called()
        with patch('app.sections.market_summary.market_summary_callbacks.ctx',Mock(triggered_id='summary-quotes')):
            models,_=f.cb['summary_models'](f.data,'PPP','WHP',0,'market-summary','NFL','123',None)
        quick=f.cb['summary_quick_submit'](1,['PPP','ELO'],['WHP'],50,0,f.data,'123',QUICK,'market-summary','NFL')
        f.app._summary_quick_jobs.queue.tasks.join()
        done=f.cb['summary_quick_results'](quick,f.data,'Game Lines','123','market-summary','NFL',1,None)[2]
        self.assertEqual(done['status'],'complete')
        callback=f.cb['summary_game_probabilities']
        args=(True,f.data,models,'Game Lines','123','PPP','WHP')
        scope=('NFL','market-summary',None)
        engine_counts={key:engine.forecast.call_count for key,engine in f.engines.items()}
        waiting,_=callback(*args,applied,quick,None,*scope)
        self.assertIn('Quick Filtering is running',str(waiting))
        completed,_=callback(*args,applied,quick,done,*scope)
        self.assertIn('Quick Filtering matches',str(completed))
        self.assertIn('PPP / ELO',str(completed))
        newer,_=apply(3,55,5,'123','NFL','market-summary','Game Lines')
        overridden,_=callback(*args,newer,quick,done,*scope)
        self.assertIn('Meets criteria',str(overridden))
        self.assertNotIn('Quick Filtering matches',str(overridden))
        changed,_=callback(True,{**f.data,'id':'new'},*args[2:],applied,quick,done,*scope)
        self.assertIn('Run Quick Filtering again',str(changed))
        self.assertEqual({key:engine.forecast.call_count for key,engine in f.engines.items()},engine_counts)
        f.fd.pull.assert_not_called()

    def test_slow_filter_does_not_hold_the_page_and_polling_never_restarts_it(self):
        from time import perf_counter
        f=self.fixture();release=Event();entered=Event();advance=Event();advanced=Event()
        self.addCleanup(release.set);self.addCleanup(advance.set)
        def blocked(*args):
            report=args[-1]
            report(15,'0 of 2 forecast methods completed')
            entered.set()
            if not advance.wait(5):raise AssertionError('First method never released')
            report(47,'1 of 2 forecast methods completed')
            advanced.set()
            if not release.wait(5):raise AssertionError('Filter never released')
            report(80,'2 of 2 forecast methods finished')
            report(85,'Applying thresholds and ranking selections')
            return dict(matches=[],evaluated=0,unavailable=0,errors=[])
        with patch('app.sections.market_summary.summary_filter_callbacks.calculate_filter',side_effect=blocked) as fit:
            start=perf_counter();request=f.cb['summary_quick_submit'](1,['PPP'],['WHP'],50,0,f.data,'123',QUICK,'market-summary','NFL')
            self.assertLess(perf_counter()-start,1);self.assertTrue(entered.wait(1))
            for tick in range(3):
                pending=f.cb['summary_quick_results'](request,f.data,QUICK,'123','market-summary','NFL',tick,None)
                self.assertIs(pending[2],no_update)
                self.assertEqual(pending[0].children[1].value,15)
                self.assertEqual(pending[1],'')
            advance.set();self.assertTrue(advanced.wait(1))
            next_stage=f.cb['summary_quick_results'](request,f.data,QUICK,'123','market-summary','NFL',3,None)
            self.assertEqual(next_stage[0].children[1].value,47)
            with self.assertRaises(PreventUpdate):f.cb['summary_quick_results'](request,f.data,'Game Lines','123','market-summary','NFL',4,None)
            self.assertEqual(fit.call_count,1);release.set();f.app._summary_quick_jobs.queue.tasks.join()
            finishing=f.cb['summary_quick_results'](request,f.data,QUICK,'123','market-summary','NFL',5,None,None,next_stage[3])
            self.assertIs(finishing[2],no_update)
            self.assertEqual(finishing[0].children[1].value,80)
            self.assertIn('2 of 2 forecast methods finished',str(finishing[0]))
            done=f.cb['summary_quick_results'](request,f.data,QUICK,'123','market-summary','NFL',6,None,None,finishing[3])
            self.assertEqual(done[2]['status'],'complete')
            self.assertIn('0 evaluated · 0 unavailable',str(done[0]))
            self.assertEqual(fit.call_count,1)

    def test_first_history_refresh_recalculates_once_then_rejects_later_changes(self):
        f=self.fixture();state={'revision':0}
        def fit(*args):
            state['revision']=1
            return dict(matches=[],evaluated=0,unavailable=0,errors=[])
        with patch('app.sections.market_summary.summary_filter_callbacks.availability_revision',side_effect=lambda sport:state['revision']),\
                patch('app.sections.market_summary.summary_filter_callbacks.calculate_filter',side_effect=fit) as compute:
            request=f.cb['summary_quick_submit'](1,['PPP'],['WHP'],50,0,f.data,'123',QUICK,'market-summary','NFL')
            f.app._summary_quick_jobs.queue.tasks.join();self.assertEqual(compute.call_count,2)
            result=f.cb['summary_quick_results'](request,f.data,QUICK,'123','market-summary','NFL',1,None)
            self.assertEqual(result[2]['status'],'complete')
            state['revision']=2
            changed=f.cb['summary_quick_results'](request,f.data,QUICK,'123','market-summary','NFL',2,result[2])
            self.assertEqual(changed[2]['status'],'changed')


if __name__=='__main__':main()
