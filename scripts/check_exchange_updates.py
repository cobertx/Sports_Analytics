"""Live refresh equivalence, small patches, lazy panels and concurrent API reads."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash._utils import to_json
from dash.exceptions import PreventUpdate
import check_kalshi as fixtures
from check_kalshi import raw_market, GAME, NOW, contract
from sports_analytics import kalshi_view as view
from sports_analytics.exchange_render import render
from sports_analytics.kalshi_analysis import definition,identity


def normalized(value):return json.loads(to_json(value))


def apply_patch(value, patch):
    result=deepcopy(normalized(value))
    for operation in normalized(patch)['operations']:
        if operation['operation']!='Assign':raise AssertionError('Unexpected patch operation')
        location=operation['location'];parent=result
        for key in location[:-1]:parent=parent[key]
        parent[location[-1]]=operation['params']['value']
    return result


class ExchangeUpdateChecks(TestCase):
    def setUp(self):
        fixture=fixtures.KalshiChecks();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture;self.service=fixture.service;self.game=fixture.game
        self.snapshot=self.service.quotes(self.game)
        for i in range(12):
            self.snapshot['rows'].append(contract(raw_market(GAME,ticker=GAME+f'-LINE{i}',
                title=f'Test line {i}',floor_strike=i+.5),dict(event_ticker=GAME,title='Game'),'NFL'))

    def test_each_provider_displays_independent_american_odds_and_implied_probability(self):
        original=deepcopy(self.snapshot['rows'][0])
        for provider in ['Kalshi','Polymarket']:
            row={**original,'yes_ask':.5,'no_ask':1.2,'notional':2}
            shown=view.comparison_rows(row,self.snapshot,provider)
            self.assertEqual([side.children[1].children for side in shown],['+300','-150'])
            self.assertEqual([side.children[2].children for side in shown],['25.00%','60.00%'])
            rendered=str(view.market_sections({**self.snapshot,'rows':[row]},provider))
            self.assertIn('Market odds',rendered);self.assertIn('Market implied probability',rendered)
            self.assertNotIn('¢',rendered);self.assertNotIn('buy (ask)',rendered)
            for ask,odds,probability in [(None,'—','—'),(0,'+∞','0.00%'),(2,'−∞','100.00%'),(3,'—','—')]:
                side=view.comparison_rows({**row,'no_ask':ask},self.snapshot,provider)[1]
                self.assertEqual((side.children[1].children,side.children[2].children),(odds,probability))
            raw=view.table({**self.snapshot,'rows':[row]},provider=provider)[0][0]
            self.assertEqual((raw['yes_ask'],raw['no_ask']), (50,120))
            self.assertEqual(original,self.snapshot['rows'][0])

    def test_bid_and_last_trade_updates_only_patch_the_optional_details_table(self):
        for provider in ['Kalshi','Polymarket']:
            first=render(self.snapshot,table_open=True,provider=provider)
            revised=deepcopy(self.snapshot);revised['rows'][0].update(yes_bid=.4,last_price=.55,volume=999)
            after=render(revised,first[-1],table_open=True,provider=provider)
            self.assertIs(after[3],no_update)
            self.assertEqual(apply_patch(first[0],after[0]),normalized(view.table(revised,provider=provider)[0]))

    def test_closed_panels_are_not_constructed_and_unchanged_polls_leave_dom_alone(self):
        with patch.object(view,'table',wraps=view.table) as table:
            first=render(self.snapshot)
            self.assertEqual(first[:3],([],[],[]));table.assert_not_called()
            unchanged={**self.snapshot,'fetched_at':(NOW+timedelta(seconds=5)).isoformat()}
            second=render(unchanged,first[-1])
            self.assertTrue(all(value is no_update for value in second[:4]))
            table.assert_not_called()

    def test_quotes_render_over_http_while_forecasts_are_blocked_for_each_sport(self):
        app,callbacks=self.fixture.app()
        key,meta=next((k,m) for k,m in app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__=='kalshi_table')
        self.assertNotIn('kalshi-models',{item['id'] for item in meta['inputs']})
        entered,release=Event(),Event()
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(10):raise AssertionError('Test forecast was not released')
            return dict(context=identity(self.game,'live','PPP','WHP'),predictions={},errors=[])
        def post(values):
            with app.server.test_client() as client:
                return client.post('/_dash-update-component',json=dict(output=key,
                    outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                    inputs=[dict(**item,value=values[item['id']]) for item in meta['inputs']],
                    state=[dict(**item,value=values[item['id']]) for item in meta['state']],changedPropIds=['kalshi-quotes.data']))
        with patch('sports_analytics.kalshi_analysis_callbacks.calculate',side_effect=slow),ThreadPoolExecutor(max_workers=2) as pool:
            forecast=pool.submit(callbacks['kalshi_models'],self.snapshot,None,self.game,'PPP','WHP','live','kalshi','Game Lines',None)
            try:
                self.assertTrue(entered.wait(3))
                for sport in ['NBA','NFL','NCAAF']:
                    game={**self.game,'sport':sport,'id':GAME.replace('KXNFL','KX'+sport)}
                    rows=[{**self.snapshot['rows'][0],'ticker':game['id']+f'-{i}',
                           'event_ticker':game['id']} for i in range(102)]
                    snapshot={**self.snapshot,'sport':sport,'game_id':game['id'],'rows':rows}
                    values={'kalshi-quotes':snapshot,'kalshi-context':game,'kalshi-category':'Game Lines',
                        'kalshi-detail-table':False,'kalshi-rules-panel':False,'kalshi-rules-signature':None,
                        'workspace-page':'kalshi','market-url':view.href(sport,game['id']),'kalshi-models':None,
                        'kalshi-applied-criteria':None,'kalshi-game-method':'PPP','kalshi-player-method':'WHP','kalshi-snapshot':'live'}
                    response=pool.submit(post,values).result(timeout=3)
                    self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                    shown=response.get_json()['response']['kalshi-market-sections']['children']
                    self.assertIn('Market odds',str(shown));self.assertIn('Market implied probability',str(shown))
                    self.assertEqual(str(shown).count("'type': 'kalshi-comparison-row'"),204)
                    self.assertFalse(forecast.done())
            finally:release.set()
            forecast.result(timeout=3)

    def test_finished_forecasts_update_existing_rows_and_paused_export_table_over_http(self):
        app,_=self.fixture.app()
        key,meta=next((k,m) for k,m in app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__=='kalshi_model_rows')
        models=dict(context=identity(self.game,'live','PPP','WHP'),predictions={row['ticker']:
                    dict(definition=definition(row),probability=.8,method='PPP',detail='Historical fit') for row in self.snapshot['rows']})
        shown=view.market_sections({**self.snapshot,'game':self.game})
        identities=[c.id for c in fixtures.components(shown) if isinstance(getattr(c,'id',None),dict)
                    and c.id.get('type')=='kalshi-comparison-row']
        values={'kalshi-models':models,'kalshi-applied-criteria':dict(probability=.75,edge=.05),
                'kalshi-game-method':'PPP','kalshi-player-method':'WHP','kalshi-snapshot':'live',
                'kalshi-quotes':self.snapshot,'kalshi-context':self.game,'kalshi-category':'Game Lines',
                'workspace-page':'kalshi','market-url':view.href('NFL',GAME),'kalshi-detail-table':True}
        pattern=next(item['id'] for item in meta['inputs'] if item['id'].startswith('{'))
        values[pattern]=identities
        self.fixture.client.reset_mock()
        with app.server.test_client() as client:
            response=client.post('/_dash-update-component',json=dict(output=key,
                outputs=[[dict(id=identity,property=o.component_property) for identity in identities] for o in meta['output']],
                inputs=[dict(**item,value=values[item['id']]) for item in meta['inputs']],
                state=[dict(**item,value=values[item['id']]) for item in meta['state']],changedPropIds=['kalshi-models.data']))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        data=response.get_json()
        self.assertEqual(len(data['response']),len(identities));self.assertIn('80.00%',str(data['response']))
        self.assertIn('market-criteria-match',str(data['response']))
        self.assertNotIn('kalshi-market-sections',data['response'])
        self.assertEqual(data['sideUpdate']['kalshi-table']['data'][0]['yes_probability'],.8)
        self.fixture.client.json.assert_not_called()

    def test_price_patches_match_full_render_including_show_more_zero_missing_and_units(self):
        for provider in ['Kalshi','Polymarket']:
            with self.subTest(provider=provider):
                before=render(self.snapshot,table_open=True,rules_open=True,provider=provider)
                revised=deepcopy(self.snapshot);revised['fetched_at']=(NOW+timedelta(seconds=5)).isoformat()
                revised['rows'][0].update(yes_ask=0,no_bid=None,volume=456.7)
                revised['rows'][-1].update(yes_bid=.5555,last_price=None,volume_24h=100)
                after=render(revised,before[-1],table_open=True,rules_open=True,provider=provider)
                self.assertEqual(apply_patch(before[3],after[3]),normalized(view.market_sections(revised,provider)))
                self.assertEqual(apply_patch(before[0],after[0]),normalized(view.table(revised,provider=provider)[0]))
                self.assertIs(after[1],no_update);self.assertIs(after[2],no_update)
                for operation in normalized(after[3])['operations']:
                    self.assertNotIn('open',operation['location'])
                full_size=len(to_json(view.market_sections(revised,provider)))
                self.assertLess(len(to_json(after[3])),full_size/4)

    def test_opening_and_closing_secondary_panels_reads_current_snapshot_without_fetching(self):
        initial=render(self.snapshot)
        opened=render(self.snapshot,initial[-1],table_open=True)
        self.assertEqual(opened[0],view.table(self.snapshot)[0]);self.assertIs(opened[3],no_update)
        with_rules=render(self.snapshot,opened[-1],table_open=True,rules_open=True)
        self.assertIs(with_rules[0],no_update);self.assertTrue(with_rules[2]);self.assertIs(with_rules[3],no_update)
        closed=render(self.snapshot,with_rules[-1])
        self.assertEqual(closed[:3],([],[],[]));self.assertIs(closed[3],no_update)
        self.fixture.client.json.reset_mock()
        render(self.snapshot,closed[-1],rules_open=True)
        self.fixture.client.json.assert_not_called()

    def test_new_quotes_rules_and_order_rebuild_only_when_structure_changes(self):
        old=render(self.snapshot,table_open=True,rules_open=True)
        for change in ['new','rules','order','game']:
            with self.subTest(change=change):
                revised=deepcopy(self.snapshot)
                if change=='new':revised['rows'].append(dict(revised['rows'][0],ticker=GAME+'-NEW'))
                if change=='rules':revised['rows'][0]['rules_primary']='Changed settlement rules'
                if change=='order':revised['rows'].reverse()
                if change=='game':revised['game_id']='another'
                result=render(revised,old[-1],table_open=True,rules_open=True)
                self.assertIsInstance(result[3],list)
                self.assertEqual(normalized(result[3]),normalized(view.market_sections(revised)))
        corrupt={**old[-1],'values':[]}
        self.assertIsInstance(render(self.snapshot,corrupt)[3],list)

    def test_catalog_polls_keep_cards_header_and_tabs_when_game_metadata_is_unchanged(self):
        _,callbacks=self.fixture.app()
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-list-timer')):
            first=callbacks['kalshi_games']('kalshi','NFL',7,0,0)
            second=callbacks['kalshi_games']('kalshi','NFL',7,0,1,first[0])
        self.assertIs(second[0],no_update);self.assertIs(second[2],no_update)
        result=callbacks['kalshi_game'](first[0],'#nfl-kalshi-game-'+GAME,'NFL',first[0]['games'][0],'Game Lines')
        self.assertTrue(all(value is no_update for value in result))

    def test_dropdown_is_not_reemitted_when_already_selected(self):
        _,callbacks=self.fixture.app()
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-context')):
            result=callbacks['kalshi_quotes']('kalshi','NFL',self.game,'Game Lines','live',0,0,5)
            self.assertIs(result[3],no_update)
            empty=callbacks['kalshi_quotes']('kalshi','NFL',None,'Game Lines','live',0,0,5)
            self.assertIs(empty[3],no_update)

    def test_cache_checks_only_fetch_pending_active_game_and_publish_completion(self):
        _,callbacks=self.fixture.app()
        state=dict(game=self.game['id'],category='Game Lines',updating=True)
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-cache-poll')):
            with patch.object(self.service,'quotes',return_value=self.snapshot) as fetch:
                for active in [None,{**state,'updating':False},{**state,'game':'another'}]:
                    with self.assertRaises(PreventUpdate):
                        callbacks['kalshi_quotes']('kalshi','NFL',self.game,'Game Lines','live',0,0,5,None,1,active)
                fetch.assert_not_called()
                result=callbacks['kalshi_quotes']('kalshi','NFL',self.game,'Game Lines','live',0,0,5,None,1,state)
                self.assertIs(result[0],self.snapshot);fetch.assert_called_once()
            catalog=self.service.games('NFL');catalog.update(window_days=7,updating=True)
            updated=callbacks['kalshi_games']('kalshi','NFL',7,0,0,catalog,1)
            self.assertIsNot(updated[0],no_update);self.assertFalse(updated[0]['updating'])
            self.assertIs(updated[2],no_update)

    def test_slow_quote_poll_does_not_queue_more_polls_and_manual_save_still_works(self):
        _,callbacks=self.fixture.app();entered,release=Event(),Event()
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(5):raise AssertionError('Timed out waiting for test release')
            return self.snapshot
        context=SimpleNamespace(triggered_id='kalshi-quote-timer')
        with patch('sports_analytics.kalshi_callbacks.ctx',context),patch.object(self.service,'quotes',side_effect=slow) as fetch:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(callbacks['kalshi_quotes'],'kalshi','NFL',self.game,'Game Lines','live',0,0,2)
                try:
                    self.assertTrue(entered.wait(3))
                    with self.assertRaises(PreventUpdate):callbacks['kalshi_quotes']('kalshi','NFL',self.game,'Game Lines','live',1,0,2)
                    self.assertEqual(fetch.call_count,1)
                finally:release.set()
                self.assertEqual(first.result(timeout=3)[0],self.snapshot)
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-pull')):
            saved=callbacks['kalshi_quotes']('kalshi','NFL',self.game,'Game Lines','live',2,1,2)[0]
        self.assertTrue(Path(saved['saved']).exists())

    def test_slow_catalog_http_does_not_block_an_unrelated_quote_request(self):
        entered,release=Event(),Event();original=self.fixture.client.json.side_effect
        def api(url,params):
            if params.get('series_ticker'):
                entered.set()
                if not release.wait(5):raise AssertionError('Catalog test timed out')
            return original(url,params)
        self.fixture.client.json.side_effect=api
        # A cold, separate query avoids the fixture's already-warm game-line cache.
        with ThreadPoolExecutor(max_workers=2) as pool:
            catalog=pool.submit(self.service.games,'NFL')
            try:
                self.assertTrue(entered.wait(3))
                quotes=pool.submit(self.service.quotes,self.game,'Passing Props')
                self.assertTrue(quotes.result(timeout=2)['rows'])
            finally:release.set()
            catalog.result(timeout=3)
        self.assertEqual(self.service._requests,{})


if __name__=='__main__':main()
