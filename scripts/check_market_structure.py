"""Offline provider matching, activity accounting, cache and dialog regressions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from threading import Barrier, Event
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.check_market_summary import bundle, binary, exchange, game
from sports_analytics.data import DataSourceError
from sports_analytics.dashboard import create_app
from sports_analytics.market_structure import MarketStructure, trade_summary, validate, BASE, DATA, CLOB
from sports_analytics.market_structure_link import button, context
from sports_analytics.market_structure_view import render
from plotly.utils import PlotlyJSONEncoder
from dash.exceptions import PreventUpdate

NOW = datetime(2026,10,25,12,tzinfo=timezone.utc)
CONDITION = '0x'+'a'*64


def fixture():
    source = bundle()
    source['providers']['FanDuel']['snapshot']['rows'] = [dict(group='Game',market='Total',market_key='alternate_totals',
        subject=side,selection=side,line=47.5,odds=100) for side in ('Over','Under')]
    source['providers']['Kalshi']['snapshot']['rows'] = [{**binary(), 'ticker':'KXNFLTOTAL-TEST-T47'}]
    source['providers']['Polymarket']['snapshot']['rows'] = [{**binary(provider='Polymarket'),
        'condition_id':CONDITION,'yes_token':'11','no_token':'22','ticker':'100'}]
    return source


def target(source=None, provider='FanDuel'):
    source = source or fixture();record = source['providers'][provider]
    return context({**record['snapshot'], 'game':record['game']}, record['snapshot']['rows'][0], provider)


def kalshi_trade(side='yes', identity='1', quantity='10.50', price='.6'):
    return dict(ticker='KXNFLTOTAL-TEST-T47', trade_id=identity, count_fp=quantity,
        yes_price_dollars=price, taker_outcome_side=side, created_time=NOW.isoformat())


def poly_trade(token='11', action='BUY', price=.6, size=10):
    return dict(condition_id=CONDITION, token_id=token, side=action, price=price, size=size,
                timestamp=int(NOW.timestamp()), transaction_hash='same-transaction')


class StructureChecks(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.service = MarketStructure(directory=self.temp.name, client=Mock(), now=lambda:NOW)
        self.addCleanup(self.service.queue.shutdown)
        self.source = fixture()
        for name, service in [('Kalshi',self.service.kalshi), ('Polymarket',self.service.polymarket)]:
            record = self.source['providers'][name]
            service.games = Mock(return_value=dict(games=[record['game']]))
            service.quotes = Mock(return_value=record['snapshot'])

    def test_equivalent_entry_points_have_one_shared_cache_and_all_providers(self):
        requests = [validate(target(self.source, name)) for name in ('FanDuel','Kalshi','Polymarket')]
        self.assertTrue(all(request['focus'] == requests[0]['focus'] for request in requests))
        self.assertEqual(len({self.service.key(request,'Kalshi') for request in requests}),1)
        with patch.object(self.service,'load',return_value=dict(markets=[],fetched_at=NOW.isoformat())):
            result = self.service.read(requests[0]);self.service.queue.tasks.join()
            result = self.service.read(requests[1])
        self.assertEqual(set(result['providers']), {'FanDuel','Kalshi','Polymarket'})
        self.assertIn('not available',result['providers']['FanDuel']['data']['notice'])
        self.assertEqual(len(render(result).children),3)
        self.assertNotIn('taker_only',str(render(result)))

    def test_fractional_kalshi_volume_direction_and_yes_price_basis(self):
        market = self.source['providers']['Kalshi']['snapshot']['rows'][0]
        rows = [kalshi_trade(),kalshi_trade(),kalshi_trade('no','2',quantity='2.25',price='.4'),
                {**kalshi_trade(identity='3'),'taker_outcome_side':None,'taker_book_side':'ask'},
                {**kalshi_trade(identity='4'),'yes_price_dollars':'NaN'},
                {**kalshi_trade(identity='5'),'created_time':(NOW-timedelta(days=2)).isoformat()}]
        result = trade_summary(rows,'Kalshi',market,NOW-timedelta(days=1),NOW)
        self.assertEqual(result['sides']['yes']['prints'],1)
        self.assertEqual(result['sides']['yes']['contracts'],10.5)
        self.assertEqual(result['sides']['yes']['dollars'],6.3)
        self.assertEqual(result['sides']['no']['prints'],2)
        self.assertEqual(result['sides']['no']['dollars'],7.2)
        self.assertEqual(result['sides']['no']['buy_dollars'],5.55)
        self.assertEqual(result['sides']['yes']['buys'],1)
        self.assertEqual(result['sides']['no']['buys'],2)
        self.assertEqual(result['sides']['no']['buy_contracts'],12.75)
        self.assertEqual((result['invalid'],result['outside']),(1,1))
        with self.assertRaises(DataSourceError):
            trade_summary([{**kalshi_trade(),'ticker':'ANOTHER-MARKET'}],'Kalshi',market,NOW,NOW)

    def test_polymarket_fills_keep_buys_sells_and_same_transaction_separate(self):
        market = self.source['providers']['Polymarket']['snapshot']['rows'][0]
        rows = [poly_trade(),poly_trade(),poly_trade('22','SELL',.4,5),poly_trade('22','BUY',.3,2)]
        result = trade_summary(rows,'Polymarket',market,NOW-timedelta(days=1),NOW)
        self.assertEqual(result['sides']['yes']['prints'],2)
        self.assertEqual(result['sides']['yes']['buy_dollars'],12)
        self.assertEqual(result['sides']['no']['sell_dollars'],2)
        self.assertEqual(result['sides']['no']['buy_dollars'],.6)
        self.assertEqual(result['sides']['no']['buys'],1)
        self.assertEqual(result['sides']['no']['buy_contracts'],2)
        with self.assertRaises(DataSourceError):
            trade_summary([{**rows[0],'condition_id':'another'}],'Polymarket',market,NOW,NOW)

    def test_real_polymarket_spread_prefix_matches_both_sides_without_reversing_the_line(self):
        fd = self.source['providers']['FanDuel']['snapshot']
        fd['rows'] = [dict(group='Game',market='Spread',market_key='alternate_spreads',
            subject='New Orleans Saints',selection='New Orleans Saints',line=-7.5,odds=-110)]
        row = self.source['providers']['Polymarket']['snapshot']['rows'][0]
        row.update(sports_market_type='spreads',title='Spread: New Orleans Saints (-7.5)',line=-7.5,
                   yes_label='New Orleans Saints',no_label='Pittsburgh Steelers')
        request = validate(target(self.source))
        with patch.object(self.service,'pages',return_value={}),patch.object(self.service,'orderbook',return_value={}), \
                patch.object(self.service,'holders',return_value={}),patch.object(self.service,'interest',return_value={}):
            record = self.service.load(request,'Polymarket')
            self.assertEqual(len(record['markets']),1)
            self.assertEqual(record['markets'][0]['match'],'equivalent')
            self.assertEqual(record['markets'][0]['labels'],
                dict(yes='New Orleans Saints -7.5',no='Pittsburgh Steelers +7.5'))
            fd['rows'][0].update(subject='Pittsburgh Steelers',selection='Pittsburgh Steelers',line=7.5)
            self.assertEqual(len(self.service.load(validate(target(self.source)),'Polymarket')['markets']),1)
            fd['rows'][0]['line'] = 6.5
            self.assertEqual(self.service.load(validate(target(self.source)),'Polymarket')['markets'],[])

    def test_windows_use_distinct_caches_and_do_not_silently_discard_older_purchases(self):
        request = validate(target(self.source))
        self.assertEqual(request['window'],'all')
        recent = validate({**request,'window':'24h'})
        self.assertNotEqual(self.service.key(request,'Kalshi'),self.service.key(recent,'Kalshi'))
        with self.assertRaises(ValueError):validate({**request,'window':'forever-and-future'})
        market = self.source['providers']['Kalshi']['snapshot']['rows'][0]
        self.service.client.json.return_value = dict(trades=[
            {**kalshi_trade(),'created_time':(NOW-timedelta(days=3)).isoformat()}],cursor='')
        all_trades = self.service.pages('Kalshi',market,datetime(1970,1,1,tzinfo=timezone.utc),NOW)
        recent_trades = self.service.pages('Kalshi',market,NOW-timedelta(days=1),NOW)
        self.assertEqual(all_trades['sides']['yes']['buys'],1)
        self.assertEqual(recent_trades['sides']['yes']['buys'],0)
        self.assertTrue(all_trades['complete'])

    def test_missing_trade_data_is_not_zero_and_partial_counts_are_not_total_splits(self):
        from sports_analytics.market_structure_view import activity
        market = dict(ticker='TEST',title='Test',labels=dict(yes='Over 47.5',no='Under 47.5'),errors=[])
        rendered = json.dumps(activity(market,'Kalshi'),cls=PlotlyJSONEncoder)
        self.assertIn('Amount paid for this outcome',rendered)
        self.assertIn('Unavailable',rendered)
        source = self.source['providers']['Kalshi']['snapshot']['rows'][0]
        market['trades'] = dict(trade_summary([kalshi_trade('no',quantity='10',price='.7')],
            'Kalshi',source,NOW-timedelta(days=1),NOW),complete=False,error=None,start=NOW.isoformat(),end=NOW.isoformat())
        rendered = json.dumps(activity(market,'Kalshi'),cls=PlotlyJSONEncoder)
        self.assertIn('$3.00',rendered)
        self.assertIn('lower bounds',rendered)
        self.assertNotIn('Share of amount paid',rendered)

    def test_window_control_callback_loads_new_scope_and_paused_polling_stops(self):
        app = create_app(structure_service=self.service)
        callback = next(m['callback'].__wrapped__ for m in app.callback_map.values()
                        if m.get('callback') and m['callback'].__wrapped__.__name__ == 'structure_load')
        request = validate(target(self.source))
        recent = {**request,'window':'24h'}
        data = dict(request=recent,providers={p:dict(pending=False,data=dict(markets=[]))
                                             for p in ('FanDuel','Kalshi','Polymarket')})
        with patch.object(self.service,'read',return_value=data) as read, \
                patch('sports_analytics.market_structure_view.ctx',SimpleNamespace(triggered_id='structure-window')):
            result,_ = callback(request,0,0,'24h',None,[])
            self.assertEqual(result['request']['window'],'24h')
            read.assert_called_once_with(recent,refresh=False)
        with patch.object(self.service,'read') as read, \
                patch('sports_analytics.market_structure_view.ctx',SimpleNamespace(triggered_id='structure-poll')):
            with self.assertRaises(PreventUpdate):callback(request,0,1,'24h',data,[])
            read.assert_not_called()

    def test_paginated_trades_report_caps_and_partial_failure_honestly(self):
        market = self.source['providers']['Kalshi']['snapshot']['rows'][0]
        self.service.client.json.side_effect = [dict(trades=[kalshi_trade(identity=str(i))],cursor=str(i)) for i in range(4)]
        result = self.service.pages('Kalshi',market,NOW-timedelta(days=1),NOW)
        self.assertFalse(result['complete']);self.assertEqual(result['scanned'],4)
        self.assertEqual(self.service.client.json.call_args.kwargs['params']['cursor'],'2')
        self.service.client.json.side_effect = [dict(trades=[kalshi_trade()],cursor='next'), DataSourceError('Offline')]
        partial = self.service.pages('Kalshi',market,NOW-timedelta(days=1),NOW)
        self.assertEqual(partial['sides']['yes']['prints'],1)
        self.assertFalse(partial['complete']);self.assertEqual(partial['error'],'Offline')
        self.service.client.json.side_effect = None
        self.service.client.json.return_value = dict(data=[poly_trade()], pagination=dict(next_cursor=None))
        poly = self.source['providers']['Polymarket']['snapshot']['rows'][0]
        result = self.service.pages('Polymarket',poly,NOW-timedelta(days=1),NOW)
        self.assertTrue(result['complete'])
        self.assertEqual(self.service.client.json.call_args.kwargs['params']['taker_only'],'true')

    def test_books_use_correct_price_units_and_do_not_invent_zero_depth(self):
        market = self.source['providers']['Kalshi']['snapshot']['rows'][0]
        self.service.client.json.return_value = dict(orderbook_fp=dict(yes_dollars=[['.6','10.5']],no_dollars=[['.3','20']]),
            orderbook=dict(yes=[[90,999]],no=[[90,999]]))
        result = self.service.orderbook('Kalshi',market)
        self.assertEqual(result['sides']['yes']['dollars'],6.3)
        self.assertEqual(result['sides']['no']['dollars'],6)
        self.service.client.json.return_value = dict(orderbook=dict(yes=[[60,10]],no=[]))
        self.assertEqual(self.service.orderbook('Kalshi',market)['sides']['yes']['dollars'],6)
        self.service.client.json.return_value = dict(orderbook_fp={})
        with self.assertRaises(DataSourceError):self.service.orderbook('Kalshi',market)

    def test_holders_are_gross_returned_wallets_and_interest_is_separate(self):
        market = self.source['providers']['Polymarket']['snapshot']['rows'][0]
        self.service.client.json.return_value = dict(data=[dict(token_id=token,holders=[dict(proxy_wallet='wallet',
            amount=10,entry_cost_usdc=cost,current_value=7)]) for token,cost in [('11',6),('22',4)]],
            pagination=dict(next_cursor='more',has_more=True))
        result = self.service.holders(market)
        self.assertFalse(result['complete'])
        self.assertEqual([result['sides'][s]['wallets'] for s in ('yes','no')],[1,1])
        self.assertEqual(result['sides']['no']['entry_cost'],4)
        self.assertEqual(self.service.client.json.call_args.kwargs['params']['include_pnl'],'true')
        self.service.client.json.return_value = dict(data=[dict(condition_id=CONDITION,value=42)])
        self.assertEqual(self.service.interest(market)['dollars'],42)

    def test_load_matches_exact_line_rejects_push_rules_and_checks_game(self):
        for provider in ('Kalshi','Polymarket'):
            with patch.object(self.service,'pages',return_value={}),patch.object(self.service,'orderbook',return_value={}), \
                    patch.object(self.service,'holders',return_value={}),patch.object(self.service,'interest',return_value={}):
                record = self.service.load(validate(target(self.source)),provider)
                self.assertEqual(len(record['markets']),1)
                changed = validate(target(self.source));changed['focus'][3] = 47;changed['focus'][5] = True;changed['reference'] = None
                self.assertEqual(self.service.load(changed,provider)['markets'],[])
            record = self.source['providers'][provider]
            original = record['snapshot']['game_id'];record['snapshot']['game_id'] = 'wrong-game'
            with self.assertRaises(DataSourceError):self.service.load(validate(target()),provider)
            record['snapshot']['game_id'] = original
        self.service.kalshi.games.return_value['games'].append(deepcopy(exchange()))
        self.assertEqual(self.service.load(validate(target()),'Kalshi')['markets'],[])

    def test_related_winner_contract_retains_its_distinct_refund_rules(self):
        fd = self.source['providers']['FanDuel']['snapshot']
        fd['rows'] = [dict(group='Game',market='Winner',market_key='alternate_spreads',
            subject='Pittsburgh Steelers',selection='Pittsburgh Steelers',line=0,odds=100)]
        market = self.source['providers']['Kalshi']['snapshot']['rows'][0]
        market.update(event_ticker=exchange()['id'],title='Pittsburgh wins',yes_label='Pittsburgh',no_label='New Orleans',
            line=None,rules_primary='Ties settle at $0.50.')
        request = validate(target(self.source))
        with patch.object(self.service,'pages',return_value={}),patch.object(self.service,'orderbook',return_value={}):
            result = self.service.load(request,'Kalshi')
        self.assertEqual(len(result['markets']),1)
        self.assertEqual(result['markets'][0]['match'],'related')
        self.assertIn('$0.50',result['markets'][0]['rules'])

    def test_background_reads_overlap_and_failure_keeps_timestamped_disk_cache(self):
        entered = Barrier(3);release = Event();request = target()
        def load(request,provider):
            entered.wait(5)
            if not release.wait(5):raise RuntimeError('Timed out')
            return dict(markets=[],notice=provider,fetched_at=NOW.isoformat())
        with patch.object(self.service,'load',side_effect=load):
            try:
                first = self.service.read(request)
                entered.wait(5)
                self.assertTrue(all(first['providers'][p]['pending'] for p in ('Kalshi','Polymarket')))
                self.assertEqual(len(render(first).children),3)
            finally:
                release.set();self.service.queue.tasks.join()
        with patch.object(self.service,'load',side_effect=DataSourceError('Source unreachable')):
            self.service.read(request,refresh=True);self.service.queue.tasks.join()
            failed = self.service.read(request)
        self.assertEqual(failed['providers']['Kalshi']['data']['fetched_at'],NOW.isoformat())
        self.assertEqual(failed['providers']['Kalshi']['error'],'Source unreachable')
        restored = MarketStructure(directory=self.temp.name, client=Mock(), now=lambda:NOW)
        self.addCleanup(restored.queue.shutdown)
        with patch.object(restored,'load',side_effect=AssertionError('Should use local data')):
            saved = restored.read(target(provider='Polymarket'))
        self.assertEqual(saved['providers']['Kalshi']['data']['notice'],'Kalshi')
        restored.client.json.assert_not_called()

    def test_buttons_and_dialog_callbacks_preserve_origin_and_render_all_sources(self):
        snapshot = self.source['providers']['FanDuel']['snapshot'];row = snapshot['rows'][0]
        regular = button(snapshot,row);summary = button(snapshot,row,surface='summary')
        self.assertNotEqual(regular.id,summary.id)
        self.assertEqual(regular.children,'Market Structure')
        app = create_app(structure_service=self.service)
        callbacks = {m['callback'].__wrapped__.__name__:m['callback'].__wrapped__ for m in app.callback_map.values() if m.get('callback')}
        with patch('sports_analytics.market_structure_view.ctx',SimpleNamespace(triggered_id=regular.id)):
            request,style,label = callbacks['structure_open']([1],0,0,'#nfl-game-123','NFL','market',[regular.id])
        self.assertEqual(style,{})
        self.assertIn('New Orleans',label)
        with patch('sports_analytics.market_structure_view.ctx',SimpleNamespace(triggered_id='structure-close')):
            self.assertEqual(callbacks['structure_open']([1],1,0,'#nfl-game-123','NFL','market',[regular.id])[:2],(None,{'display':'none'}))
        with patch('sports_analytics.market_structure_view.ctx',SimpleNamespace(triggered_id=None)):
            with self.assertRaises(PreventUpdate):
                callbacks['structure_open']([0],0,0,'#nfl-game-123','NFL','market',[regular.id])
        metadata = next(m for m in app.callback_map.values() if m.get('callback') and m['callback'].__wrapped__.__name__ == 'structure_open')
        self.assertEqual([out.component_id for out in metadata['output']],['structure-request','structure-modal','structure-prompt'])
        with self.assertRaises(ValueError):validate({**request,'game':{**request['game'],'start':None}})

    def test_http_dialog_open_and_full_activity_tables(self):
        app = create_app(structure_service=self.service)
        source = self.source['providers']['FanDuel']['snapshot']
        clicked = button(source,source['rows'][0]).id
        output, metadata = next((key,m) for key,m in app.callback_map.items()
            if m.get('callback') and m['callback'].__wrapped__.__name__ == 'structure_open')
        inputs = [dict(item,value=value) for item,value in zip(metadata['inputs'],
            [[1],0,0,'#nfl-game-123','NFL','market'])]
        response = app.server.test_client().post('/_dash-update-component', json=dict(output=output,
            outputs=[dict(id=o.component_id,property=o.component_property) for o in metadata['output']],
            inputs=inputs,state=[dict(metadata['state'][0],value=[clicked])],
            changedPropIds=[json.dumps(clicked,sort_keys=True,separators=(',',':'))+'.n_clicks']))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        payload = response.get_json()['response']
        self.assertEqual(payload['structure-modal']['style'],{})
        self.assertNotIn('market-url',payload)
        request = payload['structure-request']['data']
        providers = {'FanDuel':dict(pending=False,data=dict(markets=[],notice='No licensed FanDuel splits feed.'))}
        for provider, rows in [('Kalshi',[kalshi_trade()]), ('Polymarket',[poly_trade(),poly_trade('22','SELL',.4,4)])]:
            market = self.source['providers'][provider]['snapshot']['rows'][0]
            record = dict(ticker=market['ticker'],title=market['title'],labels=dict(yes='Over',no='Under'),
                trades=dict(trade_summary(rows,provider,market,NOW-timedelta(days=1),NOW),complete=False,
                    start=(NOW-timedelta(days=1)).isoformat(),end=NOW.isoformat(),error=None),
                book=dict(fetched_at=NOW.isoformat(),sides={s:dict(contracts=10,dollars=6,
                    levels=[dict(price=.6005,size=10)]) for s in ('yes','no')}),
                holders=dict(complete=False,fetched_at=NOW.isoformat(),sides={s:dict(wallets=100,
                    shares=1000,entry_cost=600,current_value=650) for s in ('yes','no')}) if provider=='Polymarket' else None,
                errors=[])
            providers[provider] = dict(pending=False,data=dict(fetched_at=NOW.isoformat(),markets=[record]))
        serialized = json.dumps(render(dict(request=request,providers=providers)),cls=PlotlyJSONEncoder)
        for text in ('FanDuel','Kalshi','Polymarket','Partial sample','Amount paid for this outcome','Reported entry cost',
                     'Resting buy liquidity','$0.6005','Unavailable'):
            self.assertIn(text,serialized)


if __name__ == '__main__':
    main()
