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
from scripts.callback_fixtures import callback_functions
from sports_analytics.data import DataSourceError
from sports_analytics.dashboard import create_app
from sports_analytics.market_structure import MarketStructure, trade_summary, flow_summary, validate, BASE, DATA, CLOB
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
        self.service._trade_cutoff = (NOW,NOW-timedelta(days=90))
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

    def test_legacy_kalshi_zero_purchase_fields_recover_actual_direction_counts_and_values(self):
        market=self.source['providers']['Kalshi']['snapshot']['rows'][0]
        trades=trade_summary([kalshi_trade(),kalshi_trade('no','2',quantity='2.25',price='.4')],'Kalshi',market,NOW,NOW)
        trades.pop('accounting_version')
        for side in trades['sides'].values():
            side.update(buys=0,buy_dollars=0);side.pop('buy_contracts')
        original=deepcopy(trades);actual=flow_summary(trades,'Kalshi')
        self.assertEqual(actual['sides']['yes']['buys'],1);self.assertEqual(actual['sides']['no']['buys'],1)
        self.assertEqual(actual['sides']['yes']['buy_dollars'],6.3);self.assertEqual(actual['sides']['no']['buy_dollars'],1.35)
        self.assertEqual(actual['sides']['no']['buy_contracts'],2.25)
        self.assertEqual(trades,original)

    def test_flow_charts_keep_absolute_counts_money_and_sample_scope_separate(self):
        from sports_analytics.market_structure_view import activity,amount
        from check_kalshi import components
        market=self.source['providers']['Kalshi']['snapshot']['rows'][0]
        trades=dict(trade_summary([kalshi_trade(),kalshi_trade('no','2',quantity='2.25',price='.4')],'Kalshi',market,NOW,NOW),
                    complete=False,start=NOW.isoformat(),end=NOW.isoformat(),error='Page limit')
        record=dict(ticker='TEST',title='Test',labels=dict(yes='Over 47.5',no='Under 47.5'),trades=trades)
        rendered=activity(record,'Kalshi')
        charts=[c for c in components(rendered) if getattr(c,'className',None)=='structure-flow-chart']
        self.assertEqual(len(charts),2)
        counts=[c for c in components(charts[0]) if getattr(c,'className',None)=='structure-bar-row']
        money=[c for c in components(charts[1]) if getattr(c,'className',None)=='structure-bar-row']
        self.assertEqual([getattr(c,'data-value') for c in counts],['1.0','1.0'])
        self.assertEqual([getattr(c,'data-value') for c in money],['6.3','1.35'])
        fills=[c for c in components(charts[1]) if getattr(c,'className','').startswith('structure-bar-fill ')]
        self.assertAlmostEqual(float(fills[1].style['width'].rstrip('%')),100*1.35/6.3,places=5)
        self.assertIn('Returned sample only',str(charts));self.assertNotIn('Share of taker executions',str(rendered))
        self.assertIn('lower bounds',str(rendered));self.assertEqual(amount(.0001,True),'<$0.01')

    def test_empty_failed_or_unclassified_trade_samples_are_unavailable_not_zero(self):
        from sports_analytics.market_structure_view import activity
        market=self.source['providers']['Kalshi']['snapshot']['rows'][0]
        empty=dict(trade_summary([],'Kalshi',market,NOW,NOW),complete=False,start=NOW.isoformat(),end=NOW.isoformat(),error='Archive offline')
        for provider in ('Kalshi','Polymarket'):
            normalized=flow_summary(empty,provider)
            self.assertIsNone(normalized['sides']['yes']['buys']);self.assertIsNone(normalized['sides']['no']['buy_dollars'])
            shown=str(activity(dict(ticker='TEST',title='Test',labels=dict(yes='Yes',no='No'),trades=empty),provider))
            self.assertIn('Unavailable',shown);self.assertNotIn('$0.00',shown)
        empty.update(complete=True,unclassified=1)
        self.assertIsNone(flow_summary(empty,'Kalshi')['sides']['yes']['buys'])
        empty.update(unclassified=0)
        self.assertEqual(flow_summary(empty,'Kalshi')['sides']['yes']['buys'],0)

    def test_empty_trade_feed_with_positive_volume_does_not_claim_a_zero_distribution(self):
        market={**self.source['providers']['Kalshi']['snapshot']['rows'][0],'volume':100}
        self.service.client.json.side_effect=[dict(trades=[],cursor='')]*2
        result=self.service.pages('Kalshi',market,datetime(1970,1,1,tzinfo=timezone.utc),NOW)
        self.assertFalse(result['complete']);self.assertIn('positive reported volume',result['error'])
        self.assertIsNone(flow_summary(result,'Kalshi')['sides']['no']['buy_dollars'])

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
        self.assertIn('Direction-priced traded value',rendered)
        self.assertIn('Unavailable',rendered)
        source = self.source['providers']['Kalshi']['snapshot']['rows'][0]
        market['trades'] = dict(trade_summary([kalshi_trade('no',quantity='10',price='.7')],
            'Kalshi',source,NOW-timedelta(days=1),NOW),complete=False,error=None,start=NOW.isoformat(),end=NOW.isoformat())
        rendered = json.dumps(activity(market,'Kalshi'),cls=PlotlyJSONEncoder)
        self.assertIn('$3.00',rendered)
        self.assertIn('lower bounds',rendered)
        self.assertNotIn('Share of amount paid',rendered)
        self.assertNotIn('structure-share-bar',rendered)
        self.assertNotIn('Share of direction-priced value',rendered)

    def test_archive_history_is_combined_deduplicated_and_bounded(self):
        market = self.source['providers']['Kalshi']['snapshot']['rows'][0]
        cutoff = NOW-timedelta(days=90)
        old = {**kalshi_trade('no','old',quantity='5',price='.7'),'created_time':(cutoff-timedelta(seconds=1)).isoformat()}
        boundary = {**kalshi_trade(identity='boundary'),'created_time':cutoff.isoformat()}
        self.service.client.json.side_effect = [dict(trades=[kalshi_trade(),boundary],cursor=''),dict(trades=[boundary,old],cursor='')]
        result = self.service.pages('Kalshi',market,NOW-timedelta(days=120),NOW)
        self.assertTrue(result['complete']);self.assertEqual(result['scanned'],4)
        self.assertEqual(result['sides']['yes']['prints'],2)
        self.assertEqual(result['sides']['no']['buy_dollars'],1.5)
        self.assertEqual([r['time'] for r in result['price_history']],sorted(r['time'] for r in result['price_history']))
        calls=self.service.client.json.call_args_list
        self.assertEqual([c.args[0] for c in calls],[BASE+'/markets/trades',BASE+'/historical/trades'])
        self.assertEqual(calls[0].kwargs['params'],calls[1].kwargs['params'])
        self.service.client.json.side_effect=[dict(trades=[kalshi_trade()],cursor=''),DataSourceError('Archive offline')]
        result=self.service.pages('Kalshi',market,NOW-timedelta(days=120),NOW)
        self.assertFalse(result['complete']);self.assertIn('Archive offline',result['error'])
        self.assertEqual(result['sides']['yes']['prints'],1)
        self.service.client.json.side_effect=[dict(trades=[],cursor='')]*2
        # Only two routes with empty cursors are required, not four arbitrary pages.
        result=self.service.pages('Kalshi',market,NOW-timedelta(days=120),NOW)
        self.assertTrue(result['complete']);self.assertEqual(result['scanned'],0)
        self.service.client.json.side_effect=[dict(trades=[kalshi_trade(identity=str(i))],cursor=str(i)) for i in range(4)]
        result=self.service.pages('Kalshi',market,NOW-timedelta(days=120),NOW)
        self.assertFalse(result['complete']);self.assertIn('four-page',result['error'])

    def test_unknown_archive_cutoff_never_claims_complete_history(self):
        market=self.source['providers']['Kalshi']['snapshot']['rows'][0]
        self.service._trade_cutoff=None
        self.service.client.json.side_effect=[DataSourceError('Cutoff offline'),dict(trades=[kalshi_trade()],cursor='')]
        result=self.service.pages('Kalshi',market,NOW-timedelta(days=1),NOW)
        self.assertFalse(result['complete']);self.assertIn('Archive coverage unverified',result['error'])
        self.service.client.json.side_effect=[dict(trades_created_ts=(NOW-timedelta(days=90)).isoformat()),dict(trades=[],cursor='')]
        result=self.service.pages('Kalshi',market,NOW-timedelta(days=1),NOW)
        self.assertTrue(result['complete'])
        calls_before=self.service.client.json.call_count
        self.assertEqual(self.service.trade_cutoff(),NOW-timedelta(days=90))
        self.assertEqual(self.service.client.json.call_count,calls_before)

    def test_direction_conflicts_are_excluded_and_zero_shares_are_not_invented(self):
        from sports_analytics.market_structure_view import activity
        market=self.source['providers']['Kalshi']['snapshot']['rows'][0]
        rows=[{**kalshi_trade(),'taker_book_side':'ask'},
              {**kalshi_trade(identity='2'),'taker_outcome_side':'invalid'},
              {**kalshi_trade(identity='3'),'taker_outcome_side':None,'taker_side':'no'}]
        result=trade_summary(rows,'Kalshi',market,NOW,NOW)
        self.assertEqual(result['invalid'],2)
        self.assertEqual(result['sides']['no']['prints'],1)
        self.assertEqual(len(result['price_history']),1)
        empty=dict(trade_summary([],'Kalshi',market,NOW,NOW),complete=True,start=NOW.isoformat(),end=NOW.isoformat())
        record=dict(ticker='TEST',title='Test',labels=dict(yes='Yes',no='No'),trades=empty)
        rendered=json.dumps(activity(record,'Kalshi'),cls=PlotlyJSONEncoder)
        self.assertNotIn('structure-share-bar',rendered)
        self.assertIn('Trade-price history unavailable',rendered)

    def test_quotes_use_best_levels_complementary_asks_and_explicit_missing_prices(self):
        from sports_analytics.market_structure_view import quotes,price
        book=dict(sides=dict(yes=dict(levels=[dict(price=.5),dict(price=.6005)]),no=dict(levels=[dict(price=.3)])))
        bid,ask=quotes(book,'Kalshi')
        self.assertEqual(bid,dict(yes=.6005,no=.3));self.assertEqual(ask,dict(yes=.7,no=.3995))
        self.assertEqual(price(ask['no']),'39.95¢')
        bid,ask=quotes(dict(sides=dict(yes=dict(levels=[]),no=dict(levels=[dict(price=.3)]))),'Kalshi')
        self.assertIsNone(bid['yes']);self.assertIsNone(ask['no']);self.assertEqual(ask['yes'],.7)
        self.assertEqual(price(None),'Unavailable')
        self.assertEqual(quotes(None,'Kalshi'),(dict(yes=None,no=None),dict(yes=None,no=None)))
        # Polymarket asks come from the actual outcome book, not an opposite token.
        book['sides']['yes']['asks']=[dict(price=.72),dict(price=.71)]
        self.assertEqual(quotes(book,'Polymarket')[1]['yes'],.71)

    def test_selected_source_has_primary_placement_and_explicit_loading_state(self):
        data=dict(request=dict(provider='Kalshi'),providers={p:dict(pending=p=='Kalshi',data=None) for p in ('FanDuel','Kalshi','Polymarket')})
        result=render(data)
        self.assertIn('structure-source--primary',result.children[0].className)
        serialized=json.dumps(result.children[0],cls=PlotlyJSONEncoder)
        self.assertIn('Kalshi',serialized);self.assertIn('Loading activity',serialized)

    def test_trade_chart_uses_actual_yes_prices_and_keeps_explicit_units(self):
        import plotly.graph_objects as go
        from sports_analytics.market_structure_view import price_history
        market=self.source['providers']['Kalshi']['snapshot']['rows'][0]
        rows=[kalshi_trade('no','later',quantity='2.5',price='.7'),
              {**kalshi_trade(identity='earlier'),'created_time':(NOW-timedelta(minutes=1)).isoformat()}]
        trades=trade_summary(rows,'Kalshi',market,NOW-timedelta(days=1),NOW)
        view=price_history(trades,dict(yes='Over 47.5',no='Under 47.5'))
        graph=view.children[2];figure=go.Figure(graph.figure)
        self.assertEqual(list(figure.data[0].y),[60,70])
        self.assertEqual(list(figure.data[0].x),[(NOW-timedelta(minutes=1)).isoformat(),NOW.isoformat()])
        self.assertEqual(figure.data[0].mode,'markers')
        self.assertEqual(figure.layout.xaxis.title.text,'Trade time (UTC)')
        self.assertEqual(figure.layout.yaxis.title.text,'YES price (¢)')
        self.assertEqual(list(figure.layout.yaxis.range),[0,100])
        self.assertTrue(graph.responsive)

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
        self.assertFalse(hasattr(summary,'id'))
        self.assertEqual(regular.id['context'],getattr(summary,'data-structure-context'))
        self.assertEqual(regular.children,'Market Structure')
        app = create_app(structure_service=self.service)
        callbacks = callback_functions(app)
        with patch('sports_analytics.market_structure_view.ctx',SimpleNamespace(triggered_id=regular.id)):
            request,style,label = callbacks['structure_open']([1],0,0,'#nfl-game-123','NFL','market',[regular.id])
        self.assertEqual(style,{})
        self.assertIn('New Orleans',label)
        with patch('sports_analytics.market_structure_view.ctx',SimpleNamespace(triggered_id='structure-click')):
            opened=callbacks['structure_open']([],0,0,'#nfl-market-summary','NFL','market-summary',[],
                dict(context=getattr(summary,'data-structure-context'),nonce=1))
            self.assertEqual(opened,(request,style,label))
            invalid=callbacks['structure_open']([],0,0,'#nfl-market-summary','NFL','market-summary',[],dict(context='invalid',nonce=2))
            self.assertIsNone(invalid[0]);self.assertEqual(invalid[1],{'display':'none'})
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
            [[1],0,0,'#nfl-game-123','NFL','market',None])]
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
