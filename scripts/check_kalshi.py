"""Offline public-market schema, pagination, snapshot and dashboard regressions."""
import csv
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
from unittest import TestCase, main
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash.exceptions import PreventUpdate
from scripts.callback_fixtures import callback_functions,wire_value
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.kalshi import BASE, KalshiService, category, price, contract
from sports_analytics import kalshi_view as view

GAME = 'KXNFLGAME-26OCT25PITNO'
SPREAD = 'KXNFLSPREAD-26OCT25PITNO'
PASS = 'KXNFLPASSYDS-26OCT25PITNO'
NOW = datetime(2026, 10, 23, 20, tzinfo=timezone.utc)


def raw_market(event=GAME, **changes):
    row = dict(ticker=event + '-PIT', event_ticker=event, title='Pittsburgh wins', yes_sub_title='Pittsburgh', no_sub_title='New Orleans',
        yes_bid_dollars='0.4900', yes_ask_dollars='0.5100', no_bid_dollars='0.4900', no_ask_dollars='0.5100',
        last_price_dollars='0.5000', volume_fp='15.25', open_interest_fp='12.50', notional_value_dollars='1.0000',
        status='active', rules_primary='Settles on the official result.', rules_secondary='Ties settle at $0.50.')
    return {**row, **changes}


def components(component):
    if isinstance(component,(list,tuple)):
        for child in component: yield from components(child)
    elif hasattr(component,'to_plotly_json'):
        yield component
        yield from components(getattr(component,'children',None))


class KalshiChecks(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = NOW
        self.client = Mock()
        self.client.json.side_effect = self.api
        self.service = KalshiService(self.root / 'cache', self.root / 'archive', self.client, now=lambda: self.clock)
        self.game = dict(sport='NFL', id=GAME, title='Pittsburgh vs New Orleans', start='2026-10-25T13:30:00Z', events=[GAME, SPREAD, PASS])

    def api(self, url, params):
        self.assertEqual(url, BASE + '/events')
        if params.get('series_ticker'):
            return dict(events=[dict(event_ticker=GAME, title=self.game['title'])], cursor='', milestones=[dict(id='game-id',
                start_date=self.game['start'], related_event_tickers=[GAME, SPREAD, PASS, 'SIMNFLGAME'],
                title='PIT at NO', details=dict(status='live'))])
        return dict(events=[dict(event_ticker=t, title=t, markets=[raw_market(t)]) for t in params['tickers'].split(',')], cursor='')

    def app(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            app = create_app(kalshi_service=self.service)
        return app, callback_functions(app)

    def test_dollar_prices_fixed_point_volume_missing_and_legacy_fields(self):
        result = contract(raw_market(), dict(event_ticker=GAME, title='Game'), 'NFL')
        self.assertEqual((result['yes_bid'], result['yes_ask'], result['volume'], result['open_interest']), (.49, .51, 15.25, 12.5))
        self.assertEqual(price(dict(yes_bid=49), 'yes_bid'), .49)
        for value in ['NaN', 'Infinity', '-.1', '1.1', '', None]:
            self.assertIsNone(price(dict(yes_bid_dollars=value, yes_bid=49), 'yes_bid'))
        self.assertIsNone(price(dict(yes_ask_dollars='.6', yes_ask_size_fp='0.00'), 'yes_ask'))
        self.assertEqual(price(dict(yes_bid_dollars='0.0000'), 'yes_bid'), 0)
        self.assertEqual(result['rules_secondary'], 'Ties settle at $0.50.')

    def test_exact_game_links_and_category_specific_quote_requests(self):
        catalog = self.service.games('NFL')
        self.assertEqual(len(catalog['games']), 1)
        game = catalog['games'][0]
        self.assertEqual(game['events'], sorted([GAME, SPREAD, PASS]))
        self.assertEqual(game['start'], '2026-10-25T13:30:00+00:00')
        snapshot = self.service.quotes(game, 'Game Lines')
        params = self.client.json.call_args.kwargs['params']
        self.assertEqual(set(params['tickers'].split(',')), {GAME, SPREAD})
        self.assertEqual(len(snapshot['rows']), 2)
        self.assertTrue(all(r['category'] == 'Game Lines' for r in snapshot['rows']))
        self.assertEqual(category('NFL', PASS), 'Passing Props')
        self.assertEqual(category('NBA', 'KXNBAPRA-26OCT25AB'), 'Player Props')
        self.assertEqual(category('NCAAF', 'KXNCAAF1HTOTAL-26OCT25AB'), 'Periods & Team Props')
        self.assertEqual(self.service.quotes(game, 'Other Markets')['rows'], [])

    def test_pagination_deduplicates_and_filters_scheduled_times_not_close_times(self):
        second = 'KXNFLGAME-26NOV25AB'
        self.client.json.side_effect = [dict(events=[dict(event_ticker=GAME, title='A')], cursor='page-two',
            milestones=[dict(id='a', start_date=self.game['start'], related_event_tickers=[GAME])]),
            dict(events=[dict(event_ticker=GAME, title='A'), dict(event_ticker=second, title='B')], cursor='',
                 milestones=[dict(id='b', start_date='2026-11-25T12:00:00Z', related_event_tickers=[second])])]
        result = self.service.games('NFL', 7)
        self.assertEqual([g['id'] for g in result['games']], [GAME])
        self.assertEqual(self.client.json.call_args.kwargs['params']['cursor'], 'page-two')
        self.client.json.reset_mock(side_effect=True)
        self.client.json.return_value = dict(events=[dict(event_ticker=GAME, title='A')], cursor='')
        self.assertIsNone(self.service.games('NFL', refresh=True)['games'][0]['start'])

    def test_repeated_cursor_and_cross_game_responses_are_rejected(self):
        self.client.json.side_effect = None
        self.client.json.return_value = dict(events=[], cursor='same')
        with self.assertRaisesRegex(DataSourceError, 'pagination'):
            self.service.games('NFL')
        self.client.json.return_value = dict(events=[dict(event_ticker='KXNFLGAME-OTHER', title='Wrong', markets=[])], cursor='')
        self.clock += timedelta(seconds=3)
        with self.assertRaisesRegex(DataSourceError, 'outside'):
            self.service.quotes(self.game)
        with self.assertRaises(DataSourceError):
            contract(raw_market(event_ticker='OTHER'), dict(event_ticker=GAME), 'NFL')

    def test_shared_cache_refresh_failure_and_restart_are_explicit(self):
        first = self.service.quotes(self.game)
        count = self.client.json.call_count
        self.assertEqual(self.service.quotes(self.game), first)
        self.assertEqual(self.client.json.call_count, count)
        self.clock += timedelta(seconds=3)
        self.client.json.side_effect = DataSourceError('offline')
        second = self.service.quotes(self.game)
        self.assertEqual(first['rows'], second['rows'])
        self.assertEqual(first['fetched_at'], second['fetched_at'])
        self.assertIn('previously fetched', second['notices'][0])
        restarted = KalshiService(self.root / 'cache', self.root / 'archive', self.client, now=lambda: self.clock)
        self.assertEqual(restarted.quotes(self.game)['rows'], first['rows'])

    def test_invalid_schema_cannot_replace_a_good_cached_response(self):
        first = self.service.quotes(self.game)
        self.client.json.side_effect = None
        self.client.json.return_value = {'unexpected': []}
        changed = self.service.quotes(self.game, refresh=True)
        self.assertEqual(changed['rows'], first['rows'])
        self.assertTrue(changed['notices'])
        with closing(self.service._connect()) as db:
            data = json.loads(db.execute('SELECT payload FROM responses').fetchone()[0])
        self.assertIn('events', data)

    def test_bounded_response_memory_and_snapshot_archive(self):
        for i in range(35):
            self.service._request('/events', dict(series_ticker=str(i)), ttl=60)
        self.assertEqual(len(self.service._memory), 32)
        result = self.service.quotes(self.game, save=True)
        csv_path = Path(result['saved'])
        self.assertTrue(csv_path.exists()); self.assertTrue(csv_path.with_suffix('.json').exists())
        with csv_path.open(encoding='utf-8', newline='') as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(rows[0]['quote_fetched_at'], result['fetched_at'])
        self.assertEqual(rows[0]['game_id'], GAME)
        self.assertEqual(rows[0]['yes_bid'], '0.49')
        self.assertEqual(rows[0]['price_unit'], 'USD per contract')
        history = self.service.history(self.game, 'Game Lines')
        self.assertEqual(len(history), 1)
        self.assertEqual(self.service.saved(self.game, 'Game Lines', history[0])['rows'], result['rows'])
        with self.assertRaises(ValueError): self.service.saved(self.game, 'Game Lines', '../private.json')
        with self.assertRaises(ValueError): self.service.history(dict(sport='NFL', id='../private'), 'Game Lines')

    def test_navigation_and_hidden_or_paused_pages_never_fetch_quotes(self):
        app, cb = self.app()
        for sport in ['NBA', 'NFL', 'NCAAF']:
            self.assertEqual(cb['restore_game_route'](view.href(sport)), (sport, 'kalshi'))
            self.assertEqual(cb['navigate']('kalshi', sport)[0], {'display': 'none'})
        self.assertEqual(cb['restore_game_route'](view.href('NFL', GAME)), ('NFL', 'kalshi'))
        self.assertIsNone(view.route('#nba-kalshi-game-' + GAME))
        with self.assertRaises(PreventUpdate): cb['kalshi_games']('historical', 'NFL', 7, 0, 0)
        with self.assertRaises(PreventUpdate): cb['kalshi_quotes']('historical', 'NFL', self.game, 'Game Lines', 'live', 0, 0, 5)
        with patch('sports_analytics.kalshi_callbacks.ctx', SimpleNamespace(triggered_id='kalshi-quote-timer')):
            with self.assertRaises(PreventUpdate): cb['kalshi_quotes']('kalshi', 'NFL', self.game, 'Game Lines', 'live', 0, 0, 0)
            with self.assertRaises(PreventUpdate): cb['kalshi_quotes']('kalshi', 'NFL', self.game, 'Game Lines', 'saved.json', 0, 0, 5)
        self.client.json.assert_not_called()
        live = cb['kalshi_visibility']('kalshi', 'NFL', view.href('NFL', GAME), 2, 'live', self.game)
        self.assertEqual((live[-2], live[-1]), (False, 2000))
        self.assertTrue(cb['kalshi_visibility']('market', 'NFL', view.href('NFL', GAME), 5, 'live', self.game)[-2])
        with app.server.test_client() as client:
            for url in ['/', '/_dash-layout', '/_dash-dependencies', '/assets/kalshi.css']:
                with client.get(url) as response: self.assertEqual(response.status_code, 200)

    def test_price_polls_preserve_open_rules_and_clear_wrong_game_tables(self):
        _, cb = self.app()
        snapshot = self.service.quotes(self.game)
        shown = cb['kalshi_table'](snapshot, self.game, 'Game Lines', True, True)
        self.assertEqual(shown[0][0]['yes_bid'], 49)
        snapshot['rows'][0]['yes_bid'] = .48
        snapshot['rows'][0]['yes_ask'] = .52
        updated = cb['kalshi_table'](snapshot, self.game, 'Game Lines', True, True, shown[-1])
        operations=updated[0].to_plotly_json()['operations']
        self.assertIn(dict(operation='Assign',location=[0,'yes_bid'],params={'value':48}),operations)
        self.assertIs(updated[1], no_update); self.assertIs(updated[2], no_update)
        self.assertEqual(cb['kalshi_table'](snapshot, self.game, 'Passing Props'), ([], [], [], [], None))
        self.assertTrue(all('open' not in operation['location'] for operation in updated[3].to_plotly_json()['operations']))

    def test_cards_and_detail_headers_use_existing_logos_for_all_sports(self):
        app,_=self.app()
        for sport,title,names in [
            ('NBA','ATL Hawks at CHA Hornets',['Atlanta Hawks','Charlotte Hornets']),
            ('NFL','PIT Steelers at NO Saints',['Pittsburgh Steelers','New Orleans Saints']),
            ('NCAAF','Alabama at Texas',['Alabama Crimson Tide','Texas Longhorns'])]:
            with self.subTest(sport=sport):
                game={**self.game,'sport':sport,'id':f'KX{sport}GAME-26OCT25TEST',
                      'title':title,'matchup_title':title}
                cards=view.cards(dict(games=[game]))
                header=view.game_header(game)
                for rendered in [cards,header]:
                    images=[c for c in components(rendered) if c.__class__.__name__=='Img']
                    self.assertEqual([c.alt for c in images],[name+' logo' for name in names])
                    with app.server.test_client() as client:
                        for image in images:
                            response=client.get(image.src)
                            self.assertEqual(response.status_code,200)
                            self.assertTrue(response.data.startswith(b'\x89PNG'))
                hit=next(c for c in components(cards) if getattr(c,'className',None)=='game-card-hit')
                self.assertEqual(hit.href,view.href(sport,game['id']))
                self.assertEqual(view.matchup(game)[1],['Away','Home'])
        self.assertEqual(view.team_identity('Michigan St','NCAAF')['name'],'Michigan State Spartans')
        self.assertEqual(view.team_identity('Michigan','NCAAF')['name'],'Michigan Wolverines')
        self.assertEqual(view.team_identity('Portland','NBA')['name'],'Portland Trail Blazers')
        self.assertEqual(view.team_identity('Los Angeles','NBA')['name'],'Los Angeles')
        unknown={**self.game,'title':'Unknown Club vs Another Club'}
        self.assertFalse(any(c.__class__.__name__=='Img' for c in components(view.game_card(unknown))))
        self.assertEqual(view.matchup(unknown)[1],['Matchup','Matchup'])

    def test_market_sections_group_contracts_and_keep_exchange_prices_and_show_more(self):
        snapshot=self.service.quotes(self.game)
        total='KXNFLTOTAL-26OCT25PITNO'
        snapshot['rows'] += [contract(raw_market(total,ticker=total+f'-{i}',title=f'Over {i+.5} points',
            floor_strike=i+.5,yes_bid_dollars='0.0000',no_ask_dollars='0.0000'),
            dict(event_ticker=total,title='Pittsburgh vs New Orleans: Total points'),'NFL') for i in reversed(range(8))]
        sections=view.market_sections(snapshot)
        self.assertEqual([s.children[0].children[0].children for s in sections],
                         ['Game winner','Alternate spreads','Total points'])
        self.assertTrue(all(s.className=='sportsbook-market kalshi-market' for s in sections))
        self.assertEqual([s.open for s in sections],[True,True,False])
        from check_exchange_updates import expand_sections
        expand_sections(sections)
        outcomes=[c for c in components(sections) if getattr(c,'className',None)=='market-outcome market-line-comparison']
        self.assertEqual(len(outcomes),2*len(snapshot['rows']))
        self.assertEqual(outcomes[-16].children[0].children[0].children,'Over 0.5 points — Yes')
        prices=[c for c in components(outcomes[-16:-14]) if getattr(c,'className',None)=='market-price kalshi-price']
        self.assertEqual([p.children for p in prices],['-104','+∞'])
        self.assertIn('Market odds',str(sections));self.assertIn('Market implied probability',str(sections))
        more=[c for c in components(sections) if getattr(c,'className',None)=='market-load-more']
        self.assertEqual(len(more),1);self.assertEqual(more[0].style,{'display':'none'})
        snapshot['rows'][-1]['yes_ask']=None
        self.assertIn('—',str(view.market_sections(snapshot)))
        self.assertIn('No contracts',str(view.market_sections(dict(rows=[]))))

    def test_player_market_headers_keep_names_and_the_table_is_secondary(self):
        _,cb=self.app()
        snapshot=dict(sport='NFL',game_id=GAME,category='Passing Props',fetched_at=NOW.isoformat(),
            rows=[contract(raw_market(PASS,title='Player One: 200+ passing yards'),
                 dict(event_ticker=PASS,title='Player One: Passing yards'),'NFL')])
        shown=cb['kalshi_table'](snapshot,self.game,'Passing Props')
        self.assertEqual(shown[3][0].children[0].children[0].children,'Player One - Passing yards')
        layout=view.layout()
        table_panel=next(c for c in components(layout) if getattr(c,'className',None)=='panel kalshi-detail-table')
        self.assertIn('Detailed quote table & CSV export',str(table_panel))
        self.assertFalse(getattr(table_panel,'open',False))
        self.assertTrue(any(getattr(c,'id',None)=='kalshi-table' for c in components(table_panel)))
        self.client.json.assert_not_called()

    def test_kalshi_callback_dependencies_have_no_cross_callback_cycles(self):
        app, _ = self.app()
        callbacks = {key: m for key, m in app.callback_map.items() if m.get('callback')
            and m['callback'].__wrapped__.__name__.startswith('kalshi_')}
        owners = {}
        for key, meta in callbacks.items():
            outputs = meta['output'] if isinstance(meta['output'], list) else [meta['output']]
            for output in outputs: owners[(output.component_id_str(), output.component_property)] = key
        edges = {key: set() for key in callbacks}
        for key, meta in callbacks.items():
            for item in meta['inputs']:
                parent = owners.get((item['id'], item['property']))
                if parent and parent != key: edges[key].add(parent)
        while edges:
            ready = {key for key, parents in edges.items() if not parents}
            self.assertTrue(ready, 'Cross-callback dependency cycle: ' + str(edges))
            edges = {key: parents - ready for key, parents in edges.items() if key not in ready}

    def test_http_manual_pull_saves_then_reopens_without_network(self):
        app, _ = self.app()
        values = {'workspace-page': 'kalshi', 'sport': 'NFL', 'kalshi-context': self.game, 'kalshi-category': 'Game Lines',
                  'kalshi-snapshot': 'live', 'kalshi-quote-timer': 0, 'kalshi-pull': 1, 'kalshi-frequency': 5}
        values['market-url']=view.href('NFL',GAME)
        values.update({'kalshi-cache-poll':0,'kalshi-cache-state':None})
        key, meta = next((k, m) for k, m in app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__ == 'kalshi_quotes')
        def post(changed):
            return app.server.test_client().post('/_dash-update-component', json=dict(output=key,
                outputs=[dict(id=o.component_id, property=o.component_property) for o in meta['output']],
                inputs=[dict(**item, value=wire_value(item,values)) for item in meta['inputs']],
                state=[dict(**item, value=wire_value(item,values)) for item in meta['state']], changedPropIds=[changed]))
        response = post('kalshi-pull.n_clicks')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        saved = response.get_json()['response']['kalshi-quotes']['data']
        self.assertTrue(Path(saved['saved']).exists())
        values['kalshi-snapshot'] = self.service.history(self.game, 'Game Lines')[0]
        self.client.json.reset_mock()
        response = post('kalshi-snapshot.value')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn('SAVED SNAPSHOT', response.get_json()['response']['kalshi-quote-status']['children'])
        values.update({'kalshi-quotes':saved,'kalshi-rules-signature':None,'kalshi-detail-table':False,'kalshi-rules-panel':False})
        values.update({'kalshi-models':None,'kalshi-applied-criteria':None,'kalshi-game-method':'PPP','kalshi-player-method':'WHP'})
        key,meta=next((k,m) for k,m in app.callback_map.items() if m.get('callback')
                      and m['callback'].__wrapped__.__name__=='kalshi_table')
        response=post('kalshi-quotes.data')
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        sections=response.get_json()['response']['kalshi-market-sections']['children']
        self.assertEqual(len(sections),2)
        self.assertEqual(sections[0]['type'],'Details')
        self.assertEqual(sections[0]['props']['className'],'sportsbook-market kalshi-market')
        self.client.json.assert_not_called()


if __name__ == '__main__':
    main()
