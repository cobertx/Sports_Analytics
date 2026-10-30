"""Offline saved-run catalog, deep links and result rendering for all sports."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash.exceptions import PreventUpdate
from sports_analytics.dashboard import create_app
from sports_analytics.backtesting_service import BacktestingService
from sports_analytics.backtesting_view import available_models,navigation,summary_route,summary_href
from sports_analytics.backtesting_summary import catalog
from check_backtesting import saved_game_report


class BacktestSummaryChecks(TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.service=BacktestingService(root=temporary.name)
        self.reports={}
        for sport in ['NBA','NFL','NCAAF']:
            for i,model in enumerate(available_models(sport)):
                result=saved_game_report(sha256(f'{sport}/{model.group}/{model.key}'.encode()).hexdigest(),f'2026-10-24T12:{i:02d}:00Z')
                result['context'].update(sport=sport,group=model.group,model=model.key)
                if model.group=='player-stats':
                    result['context'].update(entity='player-1',entity_label='Test Player',line=20.5,
                        market='player_points' if sport=='NBA' else 'player_pass_yds')
                    for row in result['records']:
                        row.update(actual=22,predicted=21,baseline=20,lower=10,upper=30,interval_level=.8,label='Test Player · Team A')
                if model.key=='uo':result['context']['workspace']='research'
                directory=self.service.root/sport.lower()/model.group/model.key
                directory.mkdir(parents=True)
                if model.key=='elo':
                    self.service.ensure_exports(result)  # Independent archive, with no cache file.
                else:
                    wrapper=deepcopy(result)
                    if model.key=='whp':wrapper.pop('run_id')  # Legacy cache filenames carry the ID.
                    (directory/(result['run_id']+'.json')).write_text(json.dumps(dict(result=wrapper)),encoding='utf-8')
                    if model.key=='ppp':self.service.ensure_exports(result)  # Duplicated cache/archive counts once.
                self.reports[sport,model.group,model.key]=result
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No external requests'))
        network.start();self.addCleanup(network.stop)
        self.app=create_app(backtesting_service=self.service)
        self.cb={meta['callback'].__wrapped__.__name__:meta['callback'].__wrapped__
                 for meta in self.app.callback_map.values() if meta.get('callback')}
        for method in ['start','run','options','game_inputs','player_inputs']:
            mocked=patch.object(self.service,method,side_effect=AssertionError('Opening saved results must not prepare inputs or run a model'))
            mocked.start();self.addCleanup(mocked.stop)

    def href(self,sport='NBA',group='game-outcome',key='ppp'):
        return f'#backtesting-summary-{sport.lower()}-{group}-{key}-'+self.reports[sport,group,key]['run_id']

    def post(self,name,values,changed):
        key,meta=next((k,m) for k,m in self.app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__==name)
        outputs=meta['output'];outputs=([dict(id=o.component_id,property=o.component_property) for o in outputs]
                if isinstance(outputs,list) else dict(id=outputs.component_id,property=outputs.component_property))
        with self.app.server.test_client() as client:
            response=client.post('/_dash-update-component',json=dict(output=key,outputs=outputs,
                inputs=[dict(**item,value=values[item['id']]) for item in meta['inputs']],
                state=[dict(**item,value=values[item['id']]) for item in meta['state']],changedPropIds=[changed]))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        return response.get_json()['response']

    def test_summary_navigation_routes_and_controls_for_each_sport(self):
        self.assertEqual(self.cb['restore_game_route']('#backtesting-summary'),(no_update,'backtesting'))
        for sport in ['NBA','NFL','NCAAF']:
            link=navigation(sport,self.href(sport))[0]
            self.assertEqual(link.children,'Backtest Summary');self.assertIn('active',link.className)
            self.assertEqual(self.cb['restore_game_route'](self.href(sport)),(sport,'backtesting'))
            _,_,content,style=self.cb['show_backtesting']('backtesting',sport,self.href(sport))
            self.assertEqual(content,[]);self.assertEqual(style,{'display':'none'})
            self.assertIsNone(self.cb['configure_backtest'](sport,self.href(sport),'backtesting',None)[0])
        for href in ['#backtesting-summary-nhl-game-outcome-ppp-'+'a'*64,
                     '#backtesting-summary-nba-player-stats-ppp-'+'a'*64,
                     '#backtesting-summary-nba-game-outcome-ppp-../report',self.href()+'?bad=1']:
            self.assertIsNone(summary_route(href))

    def test_comparison_links_initialize_the_selected_target_and_reject_invalid_scope(self):
        report=self.reports['NBA','game-outcome','elo']
        href=summary_href(report['context'],'elo',report['run_id'],'total',tolerance=7)
        values={'market-url':href}
        selected=self.post('backtest_summary_target',values,'market-url.hash')
        self.assertEqual(selected['backtest-summary-target']['value'],'total')
        self.assertEqual(self.cb['restore_game_route'](href),('NBA','backtesting'))
        for suffix in ('?target=bogus','?target=total&target=margin','?basis=other',
                       '?basis=common','?tolerance=nan','?tolerance=-1',
                       '?basis=all&reports=elo:'+report['run_id'],
                       '?basis=common&reports=elo:'+'f'*64):
            self.assertIsNone(summary_route(self.href(key='elo')+suffix))

    def test_all_methods_research_legacy_and_archives_are_listed_once_per_sport(self):
        for sport in ['NBA','NFL','NCAAF']:
            rows=catalog(self.service,sport)
            self.assertEqual(len(rows),7);self.assertEqual(len({r['open'] for r in rows}),7)
            self.assertEqual([r['created_at'] for r in rows],sorted([r['created_at'] for r in rows],reverse=True))
            self.assertEqual({r['group'] for r in rows},{'Game Outcome','Player Stats'})
            self.assertEqual(sum(r['workspace']=='Research' for r in rows),1)
            self.assertTrue(all('-'+sport.lower()+'-' in row['open'] for row in rows))
            self.assertTrue(all(r['evaluated']==2 for r in rows))
            self.assertIn('Test Player',[r['entity'] for r in rows])
            visible,listing,detail,data,message=self.cb['backtest_summary_list']('backtesting',sport,'#backtesting-summary',0,0)
            self.assertEqual(visible,{});self.assertEqual(listing,{});self.assertEqual(detail,{'display':'none'})
            self.assertEqual(data,rows);self.assertIn('7 saved '+sport,message)

    def test_saved_reports_reopen_original_charts_settings_and_targets_without_replay(self):
        before={str(path):path.read_bytes() for path in self.service.root.rglob('*') if path.is_file()}
        cases=[('NBA','game-outcome','elo'),('NFL','game-outcome','ppp'),('NCAAF','game-outcome','epa'),
               ('NBA','player-stats','uo'),('NFL','player-stats','whp'),('NCAAF','player-stats','pge')]
        for sport,group,key in cases:
            report=self.reports[sport,group,key]
            target='total' if group=='game-outcome' else 'margin'
            content,settings,status,disabled,style,_=self.cb['backtest_summary_result']('backtesting',sport,self.href(sport,group,key),target)
            grid=next(c for c in content.children if getattr(c,'className',None)=='backtest-chart-grid')
            timeline=grid.children[0].children[0].figure
            predicted=next(trace for trace in timeline.data if trace.name=='Predicted')
            self.assertEqual(list(predicted.y),[row['predicted_total'] if group=='game-outcome' else row['predicted'] for row in report['records']])
            self.assertEqual(json.loads(settings.children[1].children),dict(evaluation=report['context'],model_parameters=report['config']))
            self.assertIn('Original simulation settings',str(settings));self.assertIn('example',str(settings))
            self.assertIn(report['run_id'][:12],status);self.assertFalse(disabled)
            self.assertEqual(style,{} if group=='game-outcome' else {'display':'none'})
        self.assertEqual(before,{str(path):path.read_bytes() for path in self.service.root.rglob('*') if path.is_file()})

    def test_real_dash_http_list_click_open_target_switch_and_csv_export(self):
        values={'workspace-page':'backtesting','sport':'NFL','market-url':'#backtesting-summary',
                'backtest-summary-refresh':0,'backtest-result':0,'backtest-summary-target':'margin','backtest-summary-export':1,
                'backtest-summary-rendered':None}
        listed=self.post('backtest_summary_list',values,'market-url.hash')
        rows=listed['backtest-summary-table']['data']
        row=next(row for row in rows if 'game-outcome-ppp-' in row['open'])
        values['market-url']=row['open'].split('](',1)[1][:-1]
        opened=self.post('backtest_summary_result',values,'market-url.hash')
        self.assertIn('Forecast error distribution',str(opened));self.assertFalse(opened['backtest-summary-export']['disabled'])
        values['backtest-summary-target']='total'
        changed=self.post('backtest_summary_result',values,'backtest-summary-target.value')
        self.assertIn('total points',str(changed))
        downloaded=self.post('backtest_summary_export',values,'backtest-summary-export.n_clicks')['backtest-summary-download']['data']
        self.assertIn('_total_',downloaded['filename']);self.assertIn('predicted_total',downloaded['content'])

    def test_empty_sport_missing_report_inactive_pages_and_wrong_sport_are_safe(self):
        for page,sport,href in [('historical','NBA','#backtesting-summary'),('backtesting','RESEARCH','#backtesting-summary'),
                                ('backtesting','NBA','#backtesting-game-outcome-ppp')]:
            with patch('sports_analytics.backtesting_summary.catalog',side_effect=AssertionError('Inactive pages must not scan archives')):
                output=self.cb['backtest_summary_list'](page,sport,href,0,0)
                self.assertEqual(output[0],{'display':'none'});self.assertIs(output[3],no_update)
        with patch.object(self.service,'history',return_value=[]):
            listed=self.cb['backtest_summary_list']('backtesting','NCAAF','#backtesting-summary',1,0)
        self.assertEqual(listed[3],[]);self.assertIn('No completed NCAAF',listed[4])
        missing='#backtesting-summary-nba-game-outcome-ppp-'+'0'*64
        output=self.cb['backtest_summary_result']('backtesting','NBA',missing,'margin')
        self.assertTrue(output[3]);self.assertIn('unavailable',output[2])
        other=self.cb['backtest_summary_result']('backtesting','NFL',self.href(),'margin')
        self.assertTrue(all(value is no_update for value in other[:3]));self.assertTrue(other[3])
        with self.assertRaises(PreventUpdate):self.cb['backtest_summary_result']('historical','NBA',self.href(),'margin')

    def test_reports_survive_restart_and_refresh_picks_up_new_runs(self):
        restarted=BacktestingService(root=self.service.root)
        self.assertEqual(len(catalog(restarted,'NBA')),7)
        result=restarted.saved('NBA','game-outcome','elo',self.reports['NBA','game-outcome','elo']['run_id'])
        self.assertEqual(result,self.reports['NBA','game-outcome','elo'])
        extra=saved_game_report('f'*64,'2026-10-24T20:00:00Z');extra['context']['entity_label']='New team'
        restarted.ensure_exports(extra)
        rows=self.cb['backtest_summary_list']('backtesting','NBA','#backtesting-summary',1,0)[3]
        self.assertEqual(len(rows),8);self.assertEqual(rows[0]['entity'],'New team')

    def test_return_to_unchanged_report_preserves_charts_but_disk_edits_invalidate_them(self):
        show=self.cb['backtest_summary_result'];first=show('backtesting','NBA',self.href(),'margin')
        listed=show('backtesting','NBA','#backtesting-summary','margin',first[-1])
        self.assertIs(listed[0],no_update)
        with patch('sports_analytics.backtesting_summary.render',side_effect=AssertionError('Do not rebuild retained charts')):
            again=show('backtesting','NBA',self.href(),'margin',first[-1])
        self.assertIs(again[0],no_update);self.assertFalse(again[3])
        report=deepcopy(self.reports['NBA','game-outcome','ppp']);report['records'][0]['predicted_margin']+=3
        path=self.service.root/'nba/game-outcome/ppp'/(report['run_id']+'.json')
        path.write_text(json.dumps(dict(result=report)),encoding='utf-8')
        updated=show('backtesting','NBA',self.href(),'margin',first[-1])
        self.assertIsNot(updated[0],no_update);self.assertNotEqual(updated[-1],first[-1])


if __name__=='__main__':main()
