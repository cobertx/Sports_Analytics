"""Saved qualifying selections, concurrent runs, upcoming scopes and Dash wiring."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import tempfile
from unittest import TestCase,main
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from dash.exceptions import PreventUpdate
from sports_analytics.dashboard import create_app
from sports_analytics.filtered_results import FilteredResults,route
from sports_analytics.filtered_results_view import rows
from check_mispricing import FILTER_CATEGORY,components
import check_mispricing as mispricing_checks
import check_kalshi_analysis as kalshi_checks

NOW=datetime(2026,10,21,12,tzinfo=timezone.utc)


def source(sport='NBA',game_id='123',hours=24):
    return dict(sport=sport,game_id=game_id,away_team='Indiana Pacers',home_team='Brooklyn Nets',
        commence_time=(NOW+timedelta(hours=hours)).isoformat(),pulled_at=NOW.isoformat())


def ranking(probabilities=(.90,.91,.90),edges=(.4,.05,.3)):
    return dict(matches=[dict(row=dict(market_key='player_points_alternate',market='Alt Points',group='Points',
        subject=f'Player {i}',selection='Over',line=20.5,odds=-110),method='WHP, UO',probability=p,implied=p-e,edge=e,
        estimates=[dict(method='WHP',probability=p+.01,edge=e+.01),dict(method='UO',probability=p,edge=e)])
        for i,(p,e) in enumerate(zip(probabilities,edges))])


CRITERIA=dict(probability=.75,edge=.01)


class SavedFilteringChecks(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=FilteredResults(Path(self.temp.name)/'filtered.sqlite3',now=lambda:NOW)
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP'))
        self.network.start();self.addCleanup(self.network.stop)

    def test_reopen_persists_all_qualifying_markets_and_orders_probability_before_edge(self):
        self.store.save(source(),ranking(),CRITERIA)
        restored=FilteredResults(self.store.path,now=lambda:NOW)
        data=restored.upcoming('NBA')
        self.assertEqual(len(data),3)
        self.assertEqual([(r['probability'],r['edge']) for r in data],[(.91,.05),(.9,.4),(.9,.3)])
        self.assertEqual(data[0]['method'],'WHP, UO')
        self.assertEqual(data[0]['criteria'],CRITERIA)
        rendered,tooltips=rows(data)
        self.assertEqual(rendered[0]['probability'],.91)
        self.assertEqual(rendered[0]['edge'],5)
        self.assertIn('UO: 91.00%',tooltips[0]['method']['value'])
        self.assertIn('#nba-game-123',rendered[0]['open'])

    def test_latest_empty_run_replaces_stale_matches_while_all_runs_stay_saved(self):
        first=self.store.save(source(),ranking(),CRITERIA,request={'sequence':1})
        self.assertEqual(first,self.store.save(source(),ranking(),CRITERIA,request={'sequence':1}))
        self.assertEqual(len(self.store.upcoming('NBA','all')),3)
        self.store.save(source(),dict(matches=[]),CRITERIA,request={'sequence':2})
        self.assertFalse(self.store.upcoming('NBA'))
        self.assertEqual(len(self.store.upcoming('NBA','all')),3)
        self.store.save(source(),ranking((.8,),(.1,)),CRITERIA,request={'sequence':3})
        self.assertEqual(len(self.store.upcoming('NBA')),1)
        self.assertEqual(len(self.store.upcoming('NBA','all')),4)

    def test_upcoming_cutoff_sport_and_source_are_independent(self):
        for sport in ('NBA','NFL','NCAAF'):
            self.store.save(source(sport),ranking(),CRITERIA)
        self.store.save(source(hours=0,game_id='started'),ranking(),CRITERIA)
        self.store.save(source(hours=-1,game_id='past'),ranking(),CRITERIA)
        exchange=dict(sport='NBA',id='KXNBAGAME-TEST',title='Indiana Pacers at Brooklyn Nets',start=source()['commence_time'])
        snapshot=dict(sport='NBA',game_id=exchange['id'],fetched_at=NOW.isoformat())
        match=dict(row=dict(ticker='KXNBATOTAL-TEST',title='Over 220.5',category='Game Lines',yes_label='Over',no_label='Under'),
                   side='no',probability=.82,implied=.5,edge=.32,method='PPP')
        self.store.save(snapshot,dict(matches=[match]),CRITERIA,provider='Kalshi',game=exchange)
        self.assertEqual(len(self.store.upcoming('NBA')),4)
        self.assertEqual(len(self.store.upcoming('NFL')),3)
        self.assertEqual(len(self.store.upcoming('NCAAF')),3)
        self.assertEqual({row['provider'] for row in self.store.upcoming('NBA')},{'FanDuel','Kalshi'})
        self.store.now=lambda:NOW+timedelta(hours=24)
        self.assertFalse(self.store.upcoming('NBA','all'))

    def test_concurrent_saves_are_atomic_and_duplicate_quotes_do_not_repeat(self):
        ranked=ranking();ranked['matches'].append(deepcopy(ranked['matches'][0]))
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(self.store.save,source(game_id=str(i)),ranked,CRITERIA) for i in range(12)]
            for future in futures:future.result()
        self.assertEqual(len(self.store.upcoming('NBA')),36)
        self.assertFalse(self.store.upcoming('NFL'))

    def test_updated_start_time_cannot_resurrect_an_older_upcoming_report(self):
        self.store.save(source(),ranking(),CRITERIA)
        self.store.save(source(hours=-1),ranking(),CRITERIA)
        self.assertFalse(self.store.upcoming('NBA'))
        self.assertFalse(self.store.upcoming('NBA','all'))

    def test_page_routes_table_order_empty_state_and_off_page_guard(self):
        self.store.save(source(),ranking(),CRITERIA)
        app=create_app(filtered_results_service=self.store)
        callbacks={m['callback'].__wrapped__.__name__:m['callback'].__wrapped__
                   for m in app.callback_map.values() if m.get('callback')}
        table=next(c for c in components(app.layout) if getattr(c,'id',None)=='filtered-results-table')
        self.assertEqual(table.sort_by,[dict(column_id='probability',direction='desc'),dict(column_id='edge',direction='desc')])
        for sport in ('NBA','NFL','NCAAF'):
            href=f'#{sport.lower()}-filtered-results'
            self.assertEqual(route(href),sport)
            self.assertEqual(callbacks['restore_game_route'](href,None,None),(sport,'filtered-results'))
            self.assertEqual(callbacks['filtered_visibility']('filtered-results',sport),
                ({'display':'grid'},href,'sport-page-link active',False))
            self.assertEqual(callbacks['navigate']('filtered-results',sport)[0],{'display':'none'})
        values=callbacks['show_filtered_results']('filtered-results','NBA','latest',0,0)
        self.assertEqual(len(values[0]),3)
        self.assertIn('No saved qualifying selections',callbacks['show_filtered_results']('filtered-results','NFL','latest',0,0)[2])
        self.assertTrue(callbacks['filtered_visibility']('market','NBA')[3])
        with self.assertRaises(PreventUpdate):callbacks['show_filtered_results']('market','NBA','latest',0,0)
        self.assertIsNone(route('#nba-filtered-results-extra'))

    def test_fanduel_callback_saves_top_three_and_remaining_without_additional_forecasts(self):
        fixture=mispricing_checks.MispricingChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        cb=fixture.cb;selection=fixture.saved['pulled_at'];location='#nfl-game-123'
        request,_=cb['request_quick_filter'](1,75,5,['PPP'],['WHP'],location,selection)
        cb['quick_filter_markets'](request,FILTER_CATEGORY,location,selection,['PPP'],['WHP'],75,5,None)
        saved=FilteredResults(fixture.source.path.with_name('filtered_results.sqlite3'),now=lambda:NOW)
        self.assertEqual(len(saved.upcoming('NFL')),5)
        fixture.game_services['PPP'].forecast.assert_called_once()
        fixture.player_services['WHP'].forecast.assert_called_once()
        with patch.object(FilteredResults,'save',side_effect=OSError('Disk is read-only')):
            rendered,_=cb['quick_filter_markets'](request,FILTER_CATEGORY,location,selection,['PPP'],['WHP'],75,5,None)
            self.assertIn('5 qualifying selections',str(rendered))
            self.assertIn('report was not saved',str(rendered))

    def test_saved_page_http_callback_serializes_numeric_sort_values_and_game_links(self):
        self.store.save(source(),ranking(),CRITERIA)
        app=create_app(filtered_results_service=self.store)
        key,meta=next((key,meta) for key,meta in app.callback_map.items() if meta.get('callback')
                      and meta['callback'].__wrapped__.__name__=='show_filtered_results')
        values=['filtered-results','NBA','latest',0,0]
        response=app.server.test_client().post('/_dash-update-component',json=dict(output=key,
            outputs=[dict(id=out.component_id,property=out.component_property) for out in meta['output']],
            inputs=[dict(item,value=value) for item,value in zip(meta['inputs'],values)],state=[],
            changedPropIds=['workspace-page.value']))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        data=response.get_json()['response']['filtered-results-table']['data']
        self.assertEqual([row['probability'] for row in data],[.91,.9,.9])
        self.assertEqual([row['edge'] for row in data],[5,40,30])
        self.assertIn('#nba-game-123',data[0]['open'])

    def test_kalshi_callback_saves_both_outcomes_and_original_snapshot(self):
        fixture=kalshi_checks.KalshiFilterCallbacks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        scan,_=fixture.quick(methods=['PPP'],players=['WHP'])
        saved=FilteredResults(fixture.source.path.with_name('filtered_results.sqlite3'),now=lambda:NOW)
        records=saved.upcoming('NFL')
        self.assertEqual(len(records),len(scan['ranked']['matches']))
        self.assertTrue(records)
        self.assertTrue(all(row['provider']=='Kalshi' for row in records))
        self.assertTrue(all(row['quote_time']==fixture.snapshot['fetched_at'] for row in records))
        self.assertTrue(all('-kalshi-game-' in row['href'] for row in records))


if __name__=='__main__':main()
