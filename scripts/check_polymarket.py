"""Offline Polymarket discovery, token pricing, snapshots and shared UI checks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import csv
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
from unittest import TestCase, main
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash.exceptions import PreventUpdate
from scripts.callback_fixtures import callback_functions,wire_value
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.polymarket import GAMMA,CLOB,PolymarketService,book_prices,category
from sports_analytics import polymarket_view as view

NOW=datetime(2026,10,23,20,tzinfo=timezone.utc)


def market(identity='1',kind='moneyline',tokens=('11','12'),**changes):
    return {**dict(id=identity,slug='nba-bos-det-2026-10-25',question='Celtics vs. Pistons',
        sportsMarketType=kind,outcomes='["Celtics","Pistons"]',clobTokenIds=json.dumps(tokens),
        outcomePrices='["0.99","0.01"]',active=True,closed=False,enableOrderBook=True,acceptingOrders=True,
        description='Official result, including overtime. See cancellation rules.',resolutionSource='https://www.nba.com/',
        volume='123.45',volume24hr='12.34',version='v1'),**changes}


def components(component):
    if isinstance(component,(list,tuple)):
        for child in component:yield from components(child)
    elif hasattr(component,'to_plotly_json'):
        yield component
        yield from components(getattr(component,'children',None))


class PolymarketChecks(TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.clock=NOW
        self.client=Mock()
        self.client.json.side_effect=self.api
        self.client.session.post.side_effect=self.books
        self.service=PolymarketService(self.root/'cache',self.root/'archive',self.client,now=lambda:self.clock)
        self.events=[dict(id='100',gameId=77,slug='nba-bos-det-2026-10-25',title='Celtics vs. Pistons',
            startTime='2026-10-25T20:00:00Z',teams=[dict(name='Celtics',alias='Boston',ordering='away'),
            dict(name='Pistons',alias='Detroit',ordering='home')],markets=[market(),
            market('2','spreads',('13','14'),question='Celtics -3.5',line=-3.5)]),
            dict(id='101',gameId=77,slug='nba-bos-det-2026-10-25-player-props',title='Player props',
                startTime='2026-10-25T20:00:00Z',markets=[market('3','points',('15','16'),
                question='Player One: Points Over 20.5',outcomes='["Over","Under"]')])]

    def api(self,url,params):
        if url==GAMMA+'/sports':return [dict(sport=slug,series=str(i)) for i,slug in enumerate(['nba','nfl','cfb'],1)]
        self.assertEqual(url,GAMMA+'/events/keyset')
        return dict(events=deepcopy(self.events),next_cursor=None)

    def books(self,url,json,**kwargs):
        self.assertEqual(url,CLOB+'/books');self.assertLessEqual(len(json),100)
        return Mock(ok=True,json=lambda:[dict(asset_id=row['token_id'],timestamp='1792785600000',
            bids=[dict(price='.49',size='2'),dict(price='.40',size='9'),dict(price='.9',size='0')],
            asks=[dict(price='.60',size='10'),dict(price='.51',size='2')],last_trade_price='.5') for row in json])

    def game(self):return self.service.games('NBA')['games'][0]

    def app(self):
        with patch('requests.sessions.Session.request',side_effect=AssertionError('No external requests')):
            app=create_app(polymarket_service=self.service)
        return app,callback_functions(app)

    def test_discovery_uses_dynamic_series_and_groups_only_exact_provider_game_ids(self):
        game=self.game()
        self.assertEqual(game['events'],['100','101'])
        self.assertEqual(game['provider_game_id'],'77');self.assertEqual(game['series_id'],'1')
        self.assertEqual(game['matchup_title'],'Boston at Detroit')
        self.assertEqual(game['categories'],['Game Lines','Player Props'])
        self.client.session.post.assert_not_called()
        self.events.append(dict(self.events[0],id='999',gameId=88,startTime='2026-12-01T00:00:00Z'))
        self.assertEqual(len(self.service.games('NBA',refresh=True)['games']),1)
        # Event endDate/creation time never substitutes for the scheduled start.
        self.events[0]['startTime']=None;self.events[1]['startTime']=None
        self.assertEqual(self.service.games('NBA',refresh=True)['games'],[])

    def test_live_prices_use_both_actual_tokens_and_category_only_batched_books(self):
        game=self.game();snapshot=self.service.quotes(game)
        self.assertEqual(len(snapshot['rows']),2)
        row=snapshot['rows'][0]
        self.assertEqual((row['yes_label'],row['no_label']),('Celtics','Pistons'))
        self.assertEqual((row['yes_bid'],row['yes_ask'],row['no_bid'],row['no_ask']),(.49,.51,.49,.51))
        self.assertEqual(row['volume'],123.45)
        body=self.client.session.post.call_args.kwargs['json']
        self.assertEqual({r['token_id'] for r in body},{'11','12','13','14'})
        gets=self.client.json.call_count;posts=self.client.session.post.call_count
        self.service.quotes(game)
        self.assertEqual((self.client.json.call_count,self.client.session.post.call_count),(gets,posts))
        self.clock+=timedelta(seconds=3);self.service.quotes(game)
        self.assertEqual(self.client.json.call_count,gets)
        self.assertEqual(self.client.session.post.call_count,posts+1)
        props=self.service.quotes(game,'Player Props')
        self.assertEqual(props['rows'][0]['yes_label'],'Over')
        self.assertEqual({r['token_id'] for r in self.client.session.post.call_args.kwargs['json']},{'15','16'})

    def test_v2_position_ids_and_missing_or_paused_books_do_not_infer_prices(self):
        self.events[0]['markets']=[market(version='v2',positionIds=['21','22'])]
        game=self.game();snapshot=self.service.quotes(game)
        self.assertEqual({r['token_id'] for r in self.client.session.post.call_args.kwargs['json']},{'21','22'})
        self.assertEqual(snapshot['rows'][0]['yes_ask'],.51)
        self.clock+=timedelta(seconds=3)
        self.client.session.post.return_value=Mock(ok=True,json=lambda:[])
        self.client.session.post.side_effect=None
        missing=self.service.quotes(game)
        self.assertIsNone(missing['rows'][0]['yes_ask'])
        self.assertIn('unavailable',' '.join(missing['notices']))
        self.events[0]['markets'][0]['acceptingOrders']=False
        paused=self.service.quotes(game,refresh=True)
        self.assertEqual(paused['rows'][0]['status'],'Not accepting orders')
        self.assertIsNone(paused['rows'][0]['yes_ask'])

    def test_summary_reads_all_metadata_once_batches_books_and_archives_by_category(self):
        game=self.game();self.client.reset_mock()
        snapshot=self.service.all_quotes(game,refresh=True,save=True)
        self.assertEqual(len(snapshot['rows']),3)
        self.assertEqual({row['category'] for row in snapshot['rows']},{'Game Lines','Player Props'})
        self.assertEqual(self.client.json.call_count,1)
        self.assertEqual(self.client.session.post.call_count,1)
        self.assertEqual({row['token_id'] for row in self.client.session.post.call_args.kwargs['json']},
                         {'11','12','13','14','15','16'})
        for selected in game['categories']:
            filename=self.service.history(game,selected)[0]
            saved=self.service.saved(game,selected,filename)
            self.assertTrue(all(row['category']==selected for row in saved['rows']))
        requests=self.client.json.call_count;posts=self.client.session.post.call_count
        self.assertEqual(self.service.all_quotes(game)['rows'],snapshot['rows'])
        self.assertEqual((self.client.json.call_count,self.client.session.post.call_count),(requests,posts))

    def test_book_levels_empty_sides_zero_prices_and_invalid_values(self):
        self.assertEqual(book_prices(dict(bids=[dict(price='0',size='1')],asks=[])),(0.,None,None))
        for value in ['NaN','Infinity','-1','1.1']:
            with self.assertRaises(DataSourceError):book_prices(dict(bids=[dict(price=value,size='1')]))
        with self.assertRaises(DataSourceError):book_prices(dict(asks=[dict(price='.5',size='-1')]))

    def test_failed_and_invalid_refreshes_keep_previous_book_prices_and_timestamps(self):
        game=self.game();first=self.service.quotes(game)
        self.clock+=timedelta(seconds=3)
        self.client.session.post.side_effect=DataSourceError('offline')
        second=self.service.quotes(game)
        self.assertEqual(first['rows'],second['rows'])
        self.assertEqual(first['fetched_at'],second['fetched_at'])
        self.assertIn('previously fetched',' '.join(second['notices']))
        self.client.session.post.side_effect=None
        self.client.session.post.return_value=Mock(ok=True,json=lambda:[dict(asset_id='999',bids=[],asks=[])])
        bad=self.service.quotes(game,refresh=True)
        self.assertEqual(bad['rows'][0]['yes_ask'],.51)
        restarted=PolymarketService(self.root/'cache',self.root/'archive',self.client,now=lambda:self.clock)
        self.assertEqual(restarted.quotes(game)['rows'][0]['yes_ask'],.51)

    def test_cursor_pagination_and_cross_game_rejection(self):
        self.client.json.side_effect=[dict(events=[self.events[0]],next_cursor='next'),
                                      dict(events=[self.events[1]],next_cursor=None)]
        events,_,_=self.service._events({'series_id':'1'})
        self.assertEqual(len(events),2)
        self.assertEqual(self.client.json.call_args.kwargs['params']['after_cursor'],'next')
        self.client.json.side_effect=None
        self.client.json.return_value=dict(events=[],next_cursor='repeat')
        with self.assertRaisesRegex(DataSourceError,'cursor'):self.service._events({'series_id':'2'})
        self.client.json.side_effect=self.api
        game=self.game()
        self.events[0]['gameId']=888
        with self.assertRaisesRegex(DataSourceError,'outside'):self.service.quotes(game,refresh=True)

    def test_snapshots_round_trip_csv_and_json_without_requests(self):
        game=self.game();snapshot=self.service.quotes(game,save=True)
        path=Path(snapshot['saved']);self.assertTrue(path.exists())
        with path.open(encoding='utf-8',newline='') as file:rows=list(csv.DictReader(file))
        self.assertEqual(rows[0]['yes_ask'],'0.51');self.assertEqual(rows[0]['price_unit'],'USD per outcome share')
        self.client.reset_mock()
        filename=self.service.history(game,'Game Lines')[0]
        self.assertEqual(self.service.saved(game,'Game Lines',filename)['rows'],snapshot['rows'])
        self.client.json.assert_not_called();self.client.session.post.assert_not_called()
        with self.assertRaises(ValueError):self.service.saved(game,'Game Lines','../private.json')
        with self.assertRaises(ValueError):self.service.history({**game,'id':'../private'},'Game Lines')

    def test_provider_categories_and_bounded_cache(self):
        for kind,expected in [('passing_yards','Passing Props'),('receiving_yards','Receiving Props'),
            ('rushing_yards','Rushing Props'),('anytime_td','Touchdowns & Scoring'),
            ('first_half_spreads','Periods & Team Props'),('team_totals','Game Lines'),('custom','Other Markets')]:
            self.assertEqual(category({'sportsMarketType':kind}),expected)
        for i in range(35):self.service._request(GAMMA+'/sports',params={'test':i},ttl=3600)
        self.assertEqual(len(self.service._memory),32)

    def test_routes_logos_namespaces_and_inactive_or_paused_pages(self):
        app,cb=self.app();game=self.game()
        for sport in ['NBA','NFL','NCAAF']:
            self.assertEqual(cb['restore_game_route'](view.href(sport)),(sport,'polymarket'))
            self.assertEqual(cb['navigate']('polymarket',sport)[0],{'display':'none'})
        self.assertIsNone(view.route('#nba-polymarket-game-../123'))
        images=[c for c in components(view.cards(dict(games=[game]))) if c.__class__.__name__=='Img']
        self.assertEqual([c.alt for c in images],['Boston Celtics logo','Detroit Pistons logo'])
        ids=[c.id for c in components(app.layout) if hasattr(c,'id') and isinstance(c.id,str)]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertIn('polymarket-market-sections',ids);self.assertIn('kalshi-market-sections',ids)
        self.client.reset_mock()
        with self.assertRaises(PreventUpdate):cb['polymarket_quotes']('kalshi','NBA',game,'Game Lines','live',0,0,5)
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='polymarket-quote-timer')):
            with self.assertRaises(PreventUpdate):cb['polymarket_quotes']('polymarket','NBA',game,'Game Lines','live',0,0,0)
        self.client.json.assert_not_called();self.client.session.post.assert_not_called()
        self.assertTrue(cb['polymarket_visibility']('historical','NBA',view.href('NBA','100'),5,'live',game)[-2])
        self.assertEqual(cb['polymarket_visibility']('polymarket','NBA',view.href('NBA','100'),2,'live',game)[-2:],(False,2000))

    def test_college_logo_fallback_keeps_provider_school_identity_and_safe_image_urls(self):
        game={**self.game(),'sport':'NCAAF','teams':[
            dict(alias='Wyoming',name='Cowboys',abbreviation='wyo',ordering='away',
                 logo='https://polymarket-upload.s3.us-east-2.amazonaws.com/wyoming.png'),
            dict(alias='Texas',name='Longhorns',abbreviation='tex',ordering='home',logo='https://invalid.example/logo.png')]}
        images=[c for c in components(view.game_header(game)) if c.__class__.__name__=='Img']
        self.assertEqual(images[0].alt,'Wyoming logo') # Never match an unrelated school's shared mascot.
        self.assertTrue(images[0].src.startswith('https://polymarket-upload.'))
        self.assertEqual(images[1].alt,'Texas Longhorns logo')
        self.assertTrue(images[1].src.startswith('/images/ncaaf/'))
        game['teams'][0]['logo']='javascript:alert(1)'
        self.assertEqual(len([c for c in components(view.game_header(game)) if c.__class__.__name__=='Img']),1)

    def test_exchange_callbacks_have_unique_owners_and_no_dependency_cycles(self):
        app,_=self.app()
        callbacks={key:meta for key,meta in app.callback_map.items() if meta.get('callback') and
                   meta['callback'].__wrapped__.__name__.startswith(('kalshi_','polymarket_'))}
        owners={}
        for key,meta in callbacks.items():
            outputs=meta['output'] if isinstance(meta['output'],list) else [meta['output']]
            for output in outputs:
                prop=(output.component_id_str(),output.component_property)
                self.assertNotIn(prop,owners)
                owners[prop]=key
        edges={key:{owners[(item['id'],item['property'])] for item in meta['inputs']
                if (item['id'],item['property']) in owners and owners[(item['id'],item['property'])]!=key}
                for key,meta in callbacks.items()}
        while edges:
            ready={key for key,parents in edges.items() if not parents}
            self.assertTrue(ready,'Cross-callback cycle: '+str(edges))
            edges={key:parents-ready for key,parents in edges.items() if key not in ready}

    def test_http_pull_and_grouped_market_rendering(self):
        app,cb=self.app();game=self.game()
        values={'workspace-page':'polymarket','sport':'NBA','polymarket-context':game,'polymarket-category':'Game Lines',
                'polymarket-snapshot':'live','polymarket-quote-timer':0,'polymarket-pull':1,'polymarket-frequency':5,
                'polymarket-rules-signature':None}
        values.update({'polymarket-detail-table':False,'polymarket-rules-panel':False})
        values['market-url']=view.href('NBA',game['id'])
        values.update({'polymarket-cache-poll':0,'polymarket-cache-state':None})
        with app.server.test_client() as client:
            for name,changed in [('polymarket_quotes','polymarket-pull.n_clicks'),('polymarket_table','polymarket-quotes.data')]:
                key,meta=next((k,m) for k,m in app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__==name)
                response=client.post('/_dash-update-component',json=dict(output=key,
                    outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                    inputs=[dict(**item,value=wire_value(item,values)) for item in meta['inputs']],
                    state=[dict(**item,value=wire_value(item,values)) for item in meta['state']],changedPropIds=[changed]))
                self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                data=response.get_json()['response']
                if name=='polymarket_quotes':values['polymarket-quotes']=data['polymarket-quotes']['data']
                else:
                    self.assertIn('sportsbook-market',str(data));self.assertIn('Market odds',str(data))
                    self.assertIn('Market implied probability',str(data));self.assertNotIn('Outcome 1 buy',str(data))
                    self.assertIn('Celtics',str(data));self.assertNotIn('Yes buy',str(data))
        snapshot=values['polymarket-quotes']
        shown=cb['polymarket_table'](snapshot,game,'Game Lines')
        updated=cb['polymarket_table'](snapshot,game,'Game Lines',False,False,shown[-1])
        self.assertIs(updated[2],no_update)
        self.assertEqual(cb['polymarket_table'](snapshot,game,'Player Props'),([],[],[],[],None))


if __name__=='__main__':main()
