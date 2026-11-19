"""Cross-provider scope, probability semantics, navigation and Dash integration."""
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import tempfile
from threading import Barrier
from time import perf_counter,sleep
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update
from dash._utils import to_json
from dash.exceptions import PreventUpdate
from models.Game_Outcome.PPP.PPP import MarketProbability,PPPForecast
from scripts.callback_fixtures import callback_functions,wire_value
from sports_analytics.dashboard import create_app
from sports_analytics.data import DataSourceError
from sports_analytics.market_summary import calculate,entries,matching_game,polymarket_target,route
from sports_analytics.market_summary_view import sections,comparison_rows,selection_table,pull_estimate,SUMMARY_BATCH,SUMMARY_STEP,visible_comparisons,shared_estimate
from sports_analytics.lazy_markets import markets as lazy_markets
from sports_analytics.mispricing import GAME_METHODS,PLAYER_METHODS
from check_kalshi import components


class NativeNode:
    def __init__(self,tag='',attrs=()):self.tag=tag;self.attrs=dict(attrs);self.children=[]
    def find(self,tag=None,css=None):
        found=[]
        for child in self.children:
            if isinstance(child,NativeNode):
                if (tag is None or child.tag==tag) and (css is None or css in child.attrs.get('class','').split()):found.append(child)
                found.extend(child.find(tag,css))
        return found
    def __str__(self):return ''.join(map(str,self.children))


class NativeParser(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.root=NativeNode();self.stack=[self.root]
    def handle_starttag(self,tag,attrs):
        node=NativeNode(tag,attrs);self.stack[-1].children.append(node)
        if tag not in {'img','br','hr','input','wbr'}:self.stack.append(node)
    def handle_startendtag(self,tag,attrs):self.handle_starttag(tag,attrs)
    def handle_endtag(self,tag):
        if self.stack[-1].tag==tag:self.stack.pop()
    def handle_data(self,data):self.stack[-1].children.append(data)


def native_dom(value):
    parser=NativeParser()
    for component in components(value):
        if getattr(component,'data-markup',None):parser.feed(getattr(component,'data-markup'))
    return parser.root


def table_states(value):
    tables=[c for c in components(value) if isinstance(getattr(c,'id',None),dict) and c.id.get('type')=='summary-model-table']
    return [table.id for table in tables],[getattr(table,'data-rows') for table in tables]

START='2026-10-25T13:30:00Z'
STAMP='2026-10-24T12:00:00Z'
KID='KXNFLGAME-26OCT25PITNO'


def game():
    return dict(id='123',start=START,away=dict(name='Pittsburgh Steelers',abbreviation='PIT'),
                home=dict(name='New Orleans Saints',abbreviation='NO'),neutral=False)


def exchange(provider='Kalshi'):
    return dict(sport='NFL',id=KID if provider=='Kalshi' else '100',start=START,
                title='Pittsburgh at New Orleans',events=[KID],categories=['Game Lines','Player Props'])


def binary(player=False,provider='Kalshi'):
    row=dict(ticker='player' if player else 'total',event_ticker='KXNFLPASSYDS-TEST' if player else 'KXNFLTOTAL-TEST',
        event_title='Test Quarterback: Passing Yards' if player else 'Game total',
        title='Test Quarterback: Passing Yards Over 200.5' if player else 'Over 47.5 points',
        category='Player Props' if player else 'Game Lines',yes_label='Over',no_label='Under',
        yes_ask=.5,no_ask=.4,notional=1,line=200 if player else 47,strike_type='greater',cap_strike=None,
        market_type='binary',rules_primary='Official full-game result, including overtime.',rules_secondary='',status='active')
    if provider=='Polymarket':row.update(line=200.5 if player else 47.5,sports_market_type='passing_yards' if player else 'totals',status='Open')
    return row


def bundle():
    fd=dict(sport='NFL',game_id='123',pulled_at='2026-10-23T12:00:00Z',commence_time=START,
        home_team='New Orleans Saints',away_team='Pittsburgh Steelers',rows=[
            dict(group='Game',market='Spread',market_key='alternate_spreads',subject='New Orleans Saints',selection='New Orleans Saints',line=-3.5,odds=100),
            dict(group='Passing',market='Passing yards',market_key='player_pass_yds_alternate',subject='Test Quarterback',selection='Over',line=200,odds=100)])
    records={'FanDuel':dict(game=game(),snapshot=fd)}
    for provider in ['Kalshi','Polymarket']:
        g=exchange(provider)
        records[provider]=dict(game=g,snapshot=dict(sport='NFL',game_id=g['id'],fetched_at=STAMP,
            metadata_fetched_at=STAMP,rows=[binary(False,provider),binary(True,provider)],notices=[]))
    return dict(id='bundle-1',sport='NFL',game=game(),providers=records)


class GameForecast:
    home_team='NO';away_team='PIT';diagnostics={'last_game_date':'2026-10-18'}
    def spread(self,team,line):
        if team not in {'NO','PIT'}:raise ValueError('Unknown team')
        return MarketProbability(.6,.3,.1)
    def total(self,line,side):return MarketProbability(.7,.2,.1)


class PlayerForecast:
    forecasts={}
    def selection(self,row):return MarketProbability(.65,.25,.1)


class SummaryAnalysisChecks(TestCase):
    def test_draftkings_main_lines_align_with_sportsbook_push_treatment(self):
        source=bundle()
        fanduel=source['providers']['FanDuel']['snapshot']
        common=dict(group='Game',market='Total',market_key='alternate_totals',subject='Over',selection='Over',line=47,odds=-110)
        fanduel['rows']=[common]
        source['providers']['DraftKings']=dict(game=game(),snapshot={**fanduel,'bookmaker':'draftkings',
            'rows':[{**common,'odds':100},{**common,'line':47.5,'odds':110}]})
        model=PPPForecast('NO','PIT',27,21,11,10)
        result=calculate(source,'PPP','WHP',lambda *_:(model,None),lambda *_:(None,None,{}))
        ranked=[r for r in result['rows'] if r['provider']=='DraftKings']
        self.assertAlmostEqual(ranked[0]['probability'],model.total(47,'Over').probability)
        self.assertAlmostEqual(ranked[1]['probability'],model.total(47.5,'Over').win)
        aligned=comparison_rows(result['rows'],'Game Lines')['Game Lines','Game Total']
        self.assertTrue(any(set(r['quotes'])=={'FanDuel','DraftKings'} for r in aligned))
        self.assertTrue(any({'DraftKings','Kalshi','Polymarket'}<=set(r['quotes']) for r in aligned))
        from sports_analytics.summary_probability import panel
        charts=panel(source,result['rows'],'PPP')[-1].children
        self.assertTrue(any(trace.name=='DraftKings implied' for chart in charts for trace in chart.children[1].figure.data))

    def test_game_line_charts_use_source_quotes_and_settlement_matched_forecasts(self):
        from sports_analytics.summary_probability import panel
        source=bundle()
        fanduel=source['providers']['FanDuel']['snapshot']['rows']
        fanduel.extend(dict(group='Game',market='Total',market_key='alternate_totals',subject='Over',
                            selection='Over',line=line,odds=odds) for line,odds in [(47.5,-120),(47.5,100),(48,-110)])
        model=PPPForecast('NO','PIT',27,21,11,10)
        with patch('sports_analytics.market_summary.availability_revision',return_value='fixture'):
            result=calculate(source,'PPP','WHP',lambda *args:(model,None),lambda *args:(None,None,{}))
        rendered=panel(source,result['rows'],'PPP')
        charts={c.children[0].children.children:c.children[1].figure for c in rendered[-1].children}
        self.assertEqual(set(charts),{'Spread · New Orleans Saints','Game total · Over','Game total · Under'})
        traces={trace.name:trace for trace in charts['Game total · Over'].data}
        self.assertEqual(set(traces),{'PPP forecast','FanDuel implied','Kalshi implied','Polymarket implied'})
        self.assertEqual(list(traces['PPP forecast'].x),[47.5,48])
        self.assertAlmostEqual(traces['PPP forecast'].y[0],model.total(47.5,'Over').win)
        self.assertAlmostEqual(traces['PPP forecast'].y[1],model.total(48,'Over').probability)
        self.assertEqual(list(traces['FanDuel implied'].x),[47.5,48])
        self.assertEqual(traces['FanDuel implied'].y[0],.5)
        for provider in ['Kalshi','Polymarket']:
            quote=traces[provider+' implied']
            self.assertEqual(list(quote.x),[47.5]);self.assertEqual(list(quote.y),[.5])
        market_only=panel(source,entries(source),'PPP',waiting=True)
        self.assertIn('Calculating the selected forecast',market_only[1].children)
        self.assertTrue(all(trace.name.endswith('implied') for c in market_only[-1].children for trace in c.children[1].figure.data))

    def test_probability_highlights_skip_exact_thresholds_and_keep_unqualified_gaps(self):
        from sports_analytics.summary_probability import panel
        source=bundle()
        source['providers']={'FanDuel':source['providers']['FanDuel']}
        source['providers']['FanDuel']['snapshot']['rows']=[
            dict(group='Game',market='Total',market_key='alternate_totals',subject='Over',selection='Over',
                 line=line,odds=100) for line in (45.5,46.5,47.5)]
        rows=entries(source)
        for row,probability in zip(rows,(.8,.7,.83)):
            row.update(probability=probability,edge=probability-row['implied'],method='PPP')
        criteria=dict(probability=.7,edge=.1)
        charts=panel(source,rows,'PPP',criteria=criteria)[-1].children
        matched=charts[0].children[1].figure.data[-1]
        self.assertEqual(matched.name,'Meets criteria')
        self.assertEqual(list(matched.y),[.8,None,.83])
        self.assertFalse(matched.connectgaps)
        self.assertEqual(matched.customdata[0][1:3],['FanDuel','PPP'])
        ranked=[{**rows[2],'probability':.76,'edge':.26,'method':'PPP / ELO'}]
        quick=panel(source,rows,'PPP',criteria=criteria,quick_rows=ranked)[-1].children[0].children[1].figure.data[-1]
        self.assertEqual(quick.name,'Quick Filtering matches')
        self.assertEqual(list(quick.y),[None,None,.76])
        self.assertEqual(quick.customdata[2][2],'PPP / ELO')

    def test_spreads_group_each_team_and_sort_signed_lines_including_show_more(self):
        source=bundle();source['sport']='NBA';source['providers']={'FanDuel':source['providers']['FanDuel']}
        source['game'].update(away=dict(name='Boston Celtics'),home=dict(name='Denver Nuggets'))
        snapshot=source['providers']['FanDuel']['snapshot']
        snapshot.update(sport='NBA',away_team='Boston Celtics',home_team='Denver Nuggets')
        snapshot['rows']=[dict(group='Game',market='Spread',market_key='alternate_spreads',subject=team,selection=team,line=line,odds=-110)
            for line in reversed([i+.5 for i in range(-33,33)]) for team in ('Denver Nuggets','Boston Celtics')]
        rows=entries(source);original=deepcopy(rows)
        expected=[f'{team} {line:+g}' for team in ('Boston Celtics','Denver Nuggets') for line in [i+.5 for i in range(-33,33)]]
        items=comparison_rows(rows,'Game Lines')['Game Lines','Spread / Winner']
        self.assertEqual([item['outcome']['label'] for item in items],expected)
        shown=sections(source,rows,'Game Lines')
        body=next(c for c in components(shown) if isinstance(getattr(c,'id',None),dict) and c.id.get('type')=='lazy-market-body')
        for pages,count in [(0,SUMMARY_BATCH),(1,SUMMARY_BATCH+SUMMARY_STEP),(10_000,len(expected))]:
            rendered,*_=lazy_markets.read(body.id,pages)
            self.assertEqual([str(node) for node in native_dom(rendered).find('th','summary-selection')],expected[:count])
        self.assertEqual(rows,original)

    def test_totals_group_all_overs_before_unders_for_every_provider(self):
        for provider in ('FanDuel','Kalshi','Polymarket'):
            source=bundle();source['providers']={provider:source['providers'][provider]}
            snapshot=source['providers'][provider]['snapshot']
            if provider=='FanDuel':
                snapshot['rows']=[dict(group='Game',market='Total',market_key='alternate_totals',subject=side,selection=side,line=line+.5,odds=-110)
                    for line in (49,47,48) for side in ('Under','Over')]
            else:
                snapshot['rows']=[{**binary(provider=provider),'ticker':f'TOTAL-{line}','line':line+(.5 if provider=='Polymarket' else 0)} for line in (49,47,48)]
            rows=entries(source);original=deepcopy(rows)
            items=comparison_rows(rows,'Game Lines')['Game Lines','Game Total']
            expected=[f'{side} {line+.5:g}' for side in ('Over','Under') for line in (47,48,49)]
            self.assertEqual([item['outcome']['label'] for item in items],expected,provider)
            self.assertEqual([str(node) for node in native_dom(sections(source,rows,'Game Lines')).find('th','summary-selection')],expected,provider)
            self.assertEqual(rows,original)

    def test_fanduel_alternate_lines_cannot_push_all_kalshi_quotes_out_of_view(self):
        source=bundle();source['providers'].pop('Polymarket')
        source['providers']['FanDuel']['snapshot']['rows']=[dict(group='Game',market='Total',market_key='alternate_totals',
            subject=side,selection=side,line=line+.5,odds=-110) for line in range(0,71) for side in ('Over','Under')]
        source['providers']['Kalshi']['snapshot']['rows']=[{**binary(),'ticker':f'TOTAL-{line}','line':line} for line in range(20,63,3)]
        items=comparison_rows(entries(source),'Game Lines')['Game Lines','Game Total']
        self.assertTrue(all(set(item['quotes'])=={'FanDuel'} for item in items[:SUMMARY_BATCH]))
        shown=visible_comparisons(items,SUMMARY_BATCH)
        self.assertEqual(len(shown),SUMMARY_BATCH)
        self.assertGreaterEqual(sum('Kalshi' in item['quotes'] for item in shown),2)
        self.assertEqual([item['outcome']['line'] for item in shown],sorted(item['outcome']['line'] for item in shown))
        models=calculate(source,'PPP','WHP',lambda *a:(GameForecast(),None),lambda *a:(PlayerForecast(),None,{}))
        view=sections(source,models['rows'],'Game Lines')
        prices=[c for c in native_dom(view).find('td','summary-implied-cell') if c.attrs.get('data-provider')=='Kalshi']
        self.assertGreaterEqual(len(prices),2);self.assertTrue(all('%' in str(c) for c in prices))
        self.assertEqual(visible_comparisons(items,len(items)),items)
        self.assertEqual(visible_comparisons(items,0),[])

    def test_disjoint_source_ladders_all_have_prices_in_the_initial_window(self):
        items=[dict(outcome=dict(line=i),quotes={provider:dict(implied=0 if i==0 else .5)})
               for provider,lower in [('FanDuel',0),('Kalshi',100),('Polymarket',200)] for i in range(lower,lower+20)]
        shown=visible_comparisons(items,SUMMARY_BATCH)
        for provider in ('FanDuel','Kalshi','Polymarket'):
            self.assertEqual(sum(provider in item['quotes'] for item in shown),2)
        expanded=visible_comparisons(items,SUMMARY_BATCH+SUMMARY_STEP)
        self.assertTrue(all(any(item is current for current in expanded) for item in shown))
        self.assertEqual(len(expanded),SUMMARY_BATCH+SUMMARY_STEP)

    def test_large_summary_mounts_bounded_rows_and_keeps_every_selection_available(self):
        source=bundle();source['providers']={'Kalshi':source['providers']['Kalshi']}
        snapshot=source['providers']['Kalshi']['snapshot']
        snapshot['rows']=[{**binary(),'ticker':f'TOTAL-{i}','line':i} for i in range(80)]
        for name in ('First Player','Second Player'):
            snapshot['rows'].extend({**binary(True),'ticker':f'{name}-{i}','line':i,
                'event_title':name+': Passing Yards','title':name+': Passing Yards Over '+str(i+.5)} for i in range(60))
        rows=entries(source)
        for row in rows:
            row.update(probability=.625,model_odds=-167,edge=.125,method='WHP',detail='Recorded model result')
        expected=comparison_rows(rows,'All Markets')
        shown=sections(source,rows,'All Markets')
        tables=native_dom(shown).find('tbody')
        self.assertEqual(sum(len(t.children) for t in tables),2*SUMMARY_BATCH)
        self.assertLess(len(to_json(shown)),500_000)
        bodies=[c for c in components(shown) if isinstance(getattr(c,'id',None),dict) and c.id.get('type')=='lazy-market-body']
        self.assertEqual(len(bodies),len(expected))
        snapshots={'Kalshi':{**snapshot,'game':source['providers']['Kalshi']['game']}}
        # Every deferred outcome, quote, model value and history/structure link
        # must be identical to an eager rendering of that complete group.
        for body in bodies:
            (group,title),items=list(expected.items())[body.id['section']]
            full,ready,more,_=lazy_markets.read(body.id,10_000)
            self.assertEqual(to_json(full),to_json(selection_table(source,items,title,snapshots,len(items))))
            self.assertEqual(ready,'true');self.assertEqual(more,{'display':'none'})
            self.assertIn('62.50%',str(full))
        self.assertEqual(sum(len(items) for items in expected.values()),len(rows))

    def test_lazy_batches_do_not_change_thresholds_prices_or_model_values(self):
        source=bundle();source['providers']={'Kalshi':source['providers']['Kalshi']}
        snapshot=source['providers']['Kalshi']['snapshot']
        snapshot['rows']=[{**binary(),'ticker':f'TOTAL-{i}','line':i} for i in range(75)]
        rows=entries(source);original=deepcopy(rows)
        shown=sections(source,rows,'All Markets')
        body=next(c for c in components(shown) if isinstance(getattr(c,'id',None),dict) and c.id.get('type')=='lazy-market-body')
        first,_,_,_=lazy_markets.read(body.id,0)
        second,_,_,_=lazy_markets.read(body.id,1)
        def count(value):return len(native_dom(value).find('tr'))-2*len(native_dom(value).find('thead'))
        self.assertEqual(count(first),SUMMARY_BATCH);self.assertEqual(count(second),SUMMARY_BATCH+SUMMARY_STEP)
        self.assertEqual(rows,original)

    def test_large_native_table_has_constant_dash_subscriptions_and_escapes_feed_text(self):
        source=bundle();source['providers']={'Kalshi':source['providers']['Kalshi']}
        snapshot=source['providers']['Kalshi']['snapshot']
        snapshot['rows']=[{**binary(),'ticker':f'TOTAL-{i}','line':i} for i in range(150)]
        rows=entries(source);items=comparison_rows(rows,'Game Lines')['Game Lines','Game Total']
        snapshots={'Kalshi':{**snapshot,'game':source['providers']['Kalshi']['game']}}
        small=selection_table(source,items,'Game Total',snapshots,6)
        large=selection_table(source,items,'Game Total',snapshots,len(items))
        self.assertEqual(len(list(components(small))),len(list(components(large))))
        self.assertLess(len(list(components(large))),4)
        self.assertEqual(len(native_dom(large).find('tr'))-2,len(items))
        self.assertNotIn('market-structure-open',to_json(large))
        control=native_dom(large).find('button','market-structure-button')[0]
        self.assertEqual(json.loads(control.attrs['data-structure-context'])['provider'],'Kalshi')
        from sports_analytics.static_markup import markup
        from dash import html,dcc
        text='\"><script>alert("feed")</script>&{unsafe}'
        parser=NativeParser();parser.feed(markup(html.Span(text,title=text)))
        self.assertEqual(str(parser.root),text);self.assertEqual(parser.root.children[0].attrs['title'],text)
        self.assertEqual(parser.root.find('script'),[])
        with self.assertRaises(TypeError):markup(dcc.Input())
        with self.assertRaises(ValueError):markup(html.Script('alert(1)'))
        with self.assertRaises(ValueError):markup(html.A('Unsafe',href='javascript:alert(1)'))

    def test_equivalent_totals_align_in_source_columns_for_each_sport(self):
        for sport,away,home in [('NFL','Pittsburgh Steelers','New Orleans Saints'),
                                ('NBA','Miami Heat','Charlotte Hornets'),('NCAAF','Texas Longhorns','Oklahoma Sooners')]:
            source=bundle();source['sport']=sport
            source['game']['away']['name']=away;source['game']['home']['name']=home
            fd=source['providers']['FanDuel']['snapshot']
            fd.update(sport=sport,away_team=away,home_team=home,rows=[dict(group='Game',market='Total',market_key='alternate_totals',
                subject=side,selection=side,line=47.5,odds=100) for side in ['Over','Under']])
            for provider in ['Kalshi','Polymarket']:
                record=source['providers'][provider];record['game'].update(sport=sport,title=away+' at '+home)
                record['snapshot'].update(sport=sport,rows=[binary(provider=provider)])
                record['snapshot']['rows'][0]['event_ticker']='KX'+sport+'TOTAL-TEST'
            groups=comparison_rows(entries(source),'Game Lines')
            markets=groups['Game Lines','Game Total']
            self.assertEqual(len(markets),2)
            self.assertEqual([m['outcome']['label'] for m in markets],['Over 47.5','Under 47.5'])
            self.assertTrue(all(set(m['quotes'])=={'FanDuel','Kalshi','Polymarket'} for m in markets))
            rendered=sections(source,entries(source),'Game Lines')
            table=native_dom(rendered).find('table')[0]
            header=table.find('thead')[0]
            self.assertEqual(len(header.children[0].children),6)
            self.assertEqual(len(header.children[1].children),11)
            self.assertEqual([int(c.attrs['colspan']) for c in header.children[0].children[1:]],[2,2,2,2,3])
            self.assertEqual([c.attrs['alt'] for c in header.find('img')],['FanDuel','Kalshi','Polymarket'])
            body=table.find('tbody')[0]
            self.assertEqual(len(body.children),2)
            self.assertTrue(all(sum(int(cell.attrs.get('colspan',1)) for cell in row.children)==12 for row in body.children))
            for row in body.children:
                self.assertEqual(sum('summary-model-values' in cell.attrs.get('class','') for cell in row.children),3)
                self.assertTrue(all('summary-model-values' not in cell.attrs.get('class','') for cell in row.children[1:7]))
            self.assertIn('summary-best',str(rendered));self.assertIn('Historical Data',str(rendered))

    def test_push_lines_and_unsupported_contracts_are_never_merged(self):
        source=bundle();fd=source['providers']['FanDuel']['snapshot']
        fd['rows']=[dict(group='Game',market='Total',market_key='alternate_totals',subject='Over',selection='Over',line=47,odds=100)]
        source['providers']['Kalshi']['snapshot']['rows']=[binary()]
        source['providers']['Polymarket']['snapshot']['rows']=[{**binary(provider='Polymarket'),'rules_primary':'First half result only'}]
        groups=comparison_rows(entries(source),'All Markets')
        totals=groups['Game Lines','Game Total']
        self.assertEqual(len(totals),3)
        push=next(item for item in totals if item['outcome']['note'])
        self.assertEqual(set(push['quotes']),{'FanDuel'});self.assertEqual(push['outcome']['label'],'Over 47')
        self.assertTrue(any(key[0]=='Other Markets' for key in groups))
        self.assertIn('summary-missing',str(sections(source,entries(source),'All Markets')))

    def test_spread_complements_align_with_the_opponents_handicap(self):
        source=bundle();source['providers'].pop('Polymarket')
        source['providers']['FanDuel']['snapshot']['rows']=[dict(group='Game',market='Spread',market_key='alternate_spreads',
            subject=name,selection=name,line=line,odds=100) for name,line in [('New Orleans Saints',-3.5),('Pittsburgh Steelers',3.5)]]
        source['providers']['Kalshi']['snapshot']['rows']=[{**binary(),'event_ticker':'KXNFLSPREAD-TEST','line':3,
            'yes_label':'New Orleans Saints wins by over 3','title':'New Orleans Saints wins by over 3 points'}]
        groups=comparison_rows(entries(source),'Game Lines')
        spread=groups['Game Lines','Spread / Winner']
        self.assertEqual(len(spread),2)
        self.assertEqual({item['outcome']['label'] for item in spread},{'New Orleans Saints -3.5','Pittsburgh Steelers +3.5'})
        self.assertTrue(all(set(item['quotes'])=={'FanDuel','Kalshi'} for item in spread))

    def test_player_stats_and_duplicate_contracts_keep_every_forecast_cell(self):
        source=bundle();source['providers'].pop('Polymarket')
        source['providers']['FanDuel']['snapshot']['rows']=[dict(group='Passing',market='Passing yards',market_key='player_pass_yds_alternate',
            subject='Test Quarterback',selection=side,line=200.5,odds=100) for side in ['Over','Under']]
        source['providers']['Kalshi']['snapshot']['rows']=[binary(True),{**binary(True),'ticker':'another-offering'},
            {**binary(True),'ticker':'other-player','event_title':'Another Quarterback: Passing Yards',
             'title':'Another Quarterback: Passing Yards Over 200.5'}]
        groups=comparison_rows(entries(source),'Player Props')
        player=groups['Player Props','Test Quarterback - Alt Passing Yds']
        self.assertEqual(len(player),4)
        self.assertEqual(sum(len(item['quotes']) for item in player),6)
        self.assertIn(('Player Props','Another Quarterback - Alt Passing Yds'),groups)
        rendered=sections(source,entries(source),'Player Props')
        tables,visible=table_states(rendered)
        identities=[(str(table),str(anchor[:3])) for table,rows in zip(tables,visible) for anchor in json.loads(rows)]
        count=sum(len(items) for items in groups.values())
        self.assertEqual(len(identities),count)
        self.assertEqual(len({str(identity) for identity in identities}),count)

    def test_shared_estimate_keeps_probability_odds_and_edge_from_one_quote(self):
        quotes={'FanDuel':dict(provider='FanDuel',probability=.8,model_odds=-400,implied=.7,edge=.1),
                'Kalshi':dict(provider='Kalshi',probability=.65,model_odds=-186,implied=.4,edge=.25)}
        chosen=shared_estimate(quotes)
        self.assertEqual((chosen['probability'],chosen['model_odds'],chosen['edge'],chosen['edge_provider']),(.65,-186,.25,'Kalshi'))
        self.assertEqual(quotes['FanDuel']['probability'],.8)
        self.assertNotIn('edge_provider',quotes['Kalshi'])

    def test_shared_probability_uses_the_best_price_when_estimates_agree(self):
        quotes={'FanDuel':dict(provider='FanDuel',probability=.7,model_odds=-233,implied=.6,edge=.1),
                'Kalshi':dict(provider='Kalshi',probability=.7,model_odds=-233,implied=.4,edge=.3)}
        self.assertEqual(shared_estimate(quotes)['edge_provider'],'Kalshi')

    def test_polymarket_team_handicaps_and_equality_rules_are_preserved(self):
        snapshot={**bundle()['providers']['Polymarket']['snapshot'],'game':exchange('Polymarket')}
        row={**binary(provider='Polymarket'),'sports_market_type':'spreads','title':'New Orleans Saints (-3.5)',
             'line':-3.5,'yes_label':'New Orleans Saints','no_label':'Pittsburgh Steelers'}
        target,invert=polymarket_target(snapshot,row)
        self.assertEqual((target['subject'],target['line'],invert),('New Orleans Saints',-3.5,False))
        other=polymarket_target(snapshot,{**row,'yes_label':'Pittsburgh Steelers'})[0]
        self.assertEqual((other['subject'],other['line']),('Pittsburgh Steelers',3.5))
        winner=polymarket_target(snapshot,{**row,'sports_market_type':'moneyline','title':'Pittsburgh at New Orleans','line':None})[0]
        self.assertEqual((winner['subject'],winner['line']),('New Orleans Saints',0))
        self.assertIsNone(polymarket_target(snapshot,{**row,'title':'New Orleans Saints (-3)','line':-3}))
        for rules in ['Refund if tied','First half result only','Ties settle at $0.50']:
            self.assertIsNone(polymarket_target(snapshot,{**row,'rules_primary':rules}))

    def test_college_team_models_can_use_verified_home_away_identity_without_aliases(self):
        source=bundle();source['sport']='NCAAF';source['game']['home']['name']='Texas Longhorns'
        source['game']['away']['name']='Oklahoma Sooners'
        source['providers']={'FanDuel':source['providers']['FanDuel']}
        source['providers']['FanDuel']['snapshot']['rows']=source['providers']['FanDuel']['snapshot']['rows'][:1]
        source['providers']['FanDuel']['snapshot']['rows'][0]['subject']='Texas Longhorns'
        forecast=Mock(home_team='Texas',away_team='Oklahoma',diagnostics={})
        forecast.spread.return_value=MarketProbability(.6,.4,0)
        models=calculate(source,'PPP','WHP',lambda *args:(forecast,None),Mock())
        forecast.spread.assert_called_once_with('Texas',-3.5)
        self.assertEqual(models['rows'][0]['probability'],.6)

    def test_game_matching_requires_exact_teams_start_sport_and_uniqueness(self):
        g=game();candidate=exchange()
        self.assertEqual(matching_game(g,'NFL',dict(games=[candidate])),candidate)
        for changes in [dict(title='Pittsburgh at Cleveland'),dict(start='2026-10-26T13:30:00Z'),dict(start=None),dict(sport='NBA')]:
            self.assertIsNone(matching_game(g,'NFL',dict(games=[dict(candidate,**changes)])))
        self.assertIsNone(matching_game(g,'NFL',dict(games=[candidate,dict(candidate,id=KID+'2')])))
        self.assertEqual(matching_game(g,'NFL',dict(games=[dict(candidate,start='2026-10-25T09:30:00-04:00')])),
                         dict(candidate,start='2026-10-25T09:30:00-04:00'))

    def test_all_providers_use_one_game_and_player_fit_with_a_common_safe_cutoff(self):
        source=bundle();before=deepcopy(source)
        game_model=Mock(return_value=(GameForecast(),None));player_model=Mock(return_value=(PlayerForecast(),None,{}))
        models=calculate(source,'EPA','OAM',game_model,player_model)
        game_model.assert_called_once();player_model.assert_called_once()
        self.assertEqual(game_model.call_args.args[1],'EPA');self.assertEqual(player_model.call_args.args[1],'OAM')
        self.assertEqual(game_model.call_args.args[0],player_model.call_args.args[0])
        self.assertEqual(models['cutoff'],'2026-10-23T12:00:00+00:00')
        self.assertEqual(len(models['rows']),10);self.assertEqual(source,before)
        fd,player=models['rows'][:2]
        self.assertAlmostEqual(fd['probability'],.6/.9);self.assertAlmostEqual(fd['model_odds'],-200)
        self.assertAlmostEqual(player['probability'],.65/.9)
        k_yes,k_no=models['rows'][2:4]
        self.assertEqual(k_yes['probability'],.7);self.assertAlmostEqual(k_no['probability'],.3)
        self.assertEqual(k_yes['implied'],.5);self.assertEqual(k_no['implied'],.4)
        p_yes,p_no=models['rows'][6:8]
        self.assertEqual(p_yes['probability'],.7);self.assertAlmostEqual(p_no['probability'],.3)
        self.assertEqual({r['method'] for r in models['rows']},{'EPA','OAM'})

    def test_unsupported_rules_missing_prices_and_missing_models_stay_visible(self):
        source=bundle()
        source['providers']['Polymarket']['snapshot']['rows'][1]['line']=200
        source['providers']['Kalshi']['snapshot']['rows'][0]['yes_ask']=None
        models=calculate(source,'PPP','WHP',lambda *args:(None,'No team history'),lambda *args:(PlayerForecast(),None,{}))
        self.assertEqual(len(models['rows']),10)
        self.assertIsNone(models['rows'][2]['implied']);self.assertIsNone(models['rows'][2]['probability'])
        self.assertIsNone(models['rows'][-1]['probability']);self.assertIsNotNone(models['rows'][1]['probability'])
        self.assertIn('PPP: No team history',models['errors'])
        rendered=sections(source,models['rows'],'All Markets')
        self.assertIn('FanDuel',str(rendered));self.assertIn('Kalshi',str(rendered));self.assertIn('Polymarket',str(rendered))
        self.assertIn('Historical Data',str(rendered));self.assertIn('Calculated odds',str(rendered))

    def test_zero_and_one_probabilities_and_quote_endpoints_are_preserved(self):
        class Certain(PlayerForecast):
            def selection(self,row):return MarketProbability(0,1,0)
        source=bundle();source['providers']['Kalshi']['snapshot']['rows'][1]['yes_ask']=0
        models=calculate(source,'PPP','WHP',lambda *args:(GameForecast(),None),lambda *args:(Certain(),None,{}))
        player=[r for r in models['rows'] if r['provider']=='Kalshi' and r['index']==1]
        self.assertEqual([r['probability'] for r in player],[0,1]);self.assertEqual(player[0]['implied'],0)
        self.assertIn('+∞',str(sections(source,models['rows'],'Player Props')))


class SummaryCallbackChecks(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.source=Mock(path=Path(self.tmp.name)/'cache.sqlite3')
        self.schedule=Mock();self.schedule.load.return_value=SimpleNamespace(games=[game()],failed_days=[],stale_days=[])
        self.fd=Mock();self.dk=Mock();self.kalshi=Mock();self.poly=Mock();self.data=bundle()
        self.fd.latest.return_value={'123':self.data['providers']['FanDuel']['snapshot']}
        self.fd.pull.return_value=self.data['providers']['FanDuel']['snapshot']
        draftkings={**self.fd.pull.return_value,'bookmaker':'draftkings','rows':[]}
        self.dk.latest.return_value={'123':draftkings}
        self.dk.history.return_value=[draftkings]
        self.dk.pull.return_value={**draftkings,'rows':self.fd.pull.return_value['rows']}
        for name,service in [('Kalshi',self.kalshi),('Polymarket',self.poly)]:
            service.games.return_value=dict(sport='NFL',games=[exchange(name)])
            service.all_quotes.return_value=self.data['providers'][name]['snapshot']
        self.engines={key:Mock() for key in GAME_METHODS};self.players={key:Mock() for key in PLAYER_METHODS}
        for engine in self.engines.values():engine.forecast.return_value=GameForecast()
        for engine in self.players.values():engine.forecast.return_value=PlayerForecast()
        self.app=create_app(service=self.source,schedule_service=self.schedule,nfl_market_service=self.fd,draftkings_services={'NFL':self.dk},
            kalshi_service=self.kalshi,polymarket_service=self.poly,ppp_service=self.engines['PPP'],epa_service=self.engines['EPA'],
            elo_service=self.engines['ELO'],whp_service=self.players['WHP'],usage_opportunity_service=self.players['UO'],
            opponent_adjusted_service=self.players['OAM'],pace_game_environment_service=self.players['PGE'])
        self.cb=callback_functions(self.app)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No external requests'))
        network.start();self.addCleanup(network.stop)
        self.catalog=dict(sport='NFL',days=7,games=[game()],Kalshi=dict(games=[exchange()]),Polymarket=dict(games=[exchange('Polymarket')]))

    def quotes(self,pull=False,previous=None):
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-pull' if pull else 'summary-game')):
            return self.cb['summary_quotes']('123',self.catalog,1 if pull else 0,'market-summary','NFL',previous)

    def test_probability_charts_render_only_open_game_lines_and_reuse_the_existing_forecast(self):
        quoted=self.quotes()[0]
        callback=self.cb['summary_game_probabilities']
        args=(quoted,None,'Game Lines','123','PPP','WHP',None,None,None,'NFL','market-summary',None)
        with self.assertRaises(PreventUpdate):callback(False,*args)
        for index,value in [(2,'Player Props'),(3,'456'),(9,'NBA'),(10,'market')]:
            changed=list(args);changed[index]=value
            with self.assertRaises(PreventUpdate):callback(True,*changed)
        market,scope=callback(True,*args)
        self.assertIn('Calculating the selected forecast',str(market))
        with self.assertRaises(PreventUpdate):callback(True,*args[:-1],scope)
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-quotes')):
            models,_=self.cb['summary_models'](quoted,'PPP','WHP',0,'market-summary','NFL','123',None)
        calls=self.engines['PPP'].forecast.call_count
        completed,new_scope=callback(True,quoted,models,*args[2:])
        self.assertIn('PPP forecast',str(completed));self.assertNotEqual(scope,new_scope)
        self.assertEqual(self.engines['PPP'].forecast.call_count,calls)
        self.fd.pull.assert_not_called()

    def test_market_data_contains_draftkings_and_summary_routes_for_all_sports(self):
        nav=next(c for c in components(self.app.layout) if getattr(c,'id',None)=='market-data-navigation')
        links={getattr(c,'id',None):getattr(c,'children',None) for c in components(nav)}
        self.assertEqual([links[k] for k in ['market-page-link','draftkings-page-link','kalshi-page-link','polymarket-page-link','market-summary-page-link']],
                         ['FanDuel','DraftKings','Kalshi','Polymarket','Market Summary'])
        for sport in ['NBA','NFL','NCAAF']:
            href=f'#{sport.lower()}-market-summary'
            self.assertEqual(route(href),sport)
            self.assertEqual(self.cb['restore_game_route'](f'#{sport.lower()}-draftkings-market'),(sport,'market-summary'))
            self.assertEqual(self.cb['restore_game_route'](href),(sport,'market-summary'))
            self.assertEqual(self.cb['navigate']('market-summary',sport)[0],{'display':'none'})
            visible=self.cb['summary_visibility']('market-summary',sport)
            self.assertEqual(visible[0],{'display':'grid'});self.assertEqual(visible[3],href)
        self.assertEqual(self.cb['summary_visibility']('historical','RESEARCH')[1],{'display':'none'})

    def test_initial_summary_never_spends_fanduel_credits_and_manual_pull_refreshes_all(self):
        quoted,status=self.quotes()
        self.fd.pull.assert_not_called();self.fd.latest.assert_called_once_with(['123'])
        self.dk.pull.assert_not_called();self.dk.latest.assert_called_once_with(['123'])
        self.kalshi.all_quotes.assert_called_once_with(exchange(),refresh=False,save=False)
        self.poly.all_quotes.assert_called_once_with(exchange('Polymarket'),refresh=False,save=False)
        self.assertEqual(sum(len(p['snapshot']['rows']) for p in quoted['providers'].values()),6)
        self.quotes(True,quoted)
        self.fd.pull.assert_called_once();self.assertIn('summary:NFL:123:',self.fd.pull.call_args.args[1])
        self.dk.pull.assert_called_once()
        self.assertEqual(self.kalshi.all_quotes.call_args.kwargs,dict(refresh=True,save=True))
        self.assertEqual(self.poly.all_quotes.call_args.kwargs,dict(refresh=True,save=True))

    def test_individual_pull_controls_show_costs_for_all_supported_sports(self):
        ids={getattr(c,'id',None):c for c in components(self.app.layout) if isinstance(getattr(c,'id',None),str)}
        for provider in ('FanDuel','Kalshi','Polymarket','DraftKings'):
            self.assertEqual(ids['summary-pull-'+provider.lower()].children,'Pull '+provider)
            self.assertTrue(ids['summary-source-'+provider.lower()].disabled)
        for sport,cost in [('NBA',13),('NFL',17),('NCAAF',17)]:
            self.assertEqual(pull_estimate(sport,'FanDuel'),f'Estimated up to {cost} API credits')
            for provider in ('Kalshi','Polymarket'):self.assertIn('0 Odds API credits',pull_estimate(sport,provider))

    def test_source_controls_enable_only_current_matched_sources_in_the_browser(self):
        from check_navigation_pipeline import node,run_client
        if not node():self.skipTest('Node unavailable')
        refs=[]
        for sport in ('NBA','NFL','NCAAF'):
            catalog=dict(sport=sport,games=[dict(id='123')])
            data=dict(sport=sport,game=dict(id='123'),providers={
                'FanDuel':dict(game=dict(id='123'),snapshot=None,error='No saved snapshot'),
                'Kalshi':dict(game=dict(id='KX'),snapshot=dict(rows=[{}])),
                'Polymarket':dict(game=None,snapshot=None)})
            refs.append(dict(output='summary-source-fanduel.disabled',args=['market-summary',sport,'123',catalog,data],
                expected=[False,False,True,False,'Unavailable · ready to retry','1 market loaded',
                          'No matching event · refresh upcoming games','No quotes loaded',pull_estimate(sport,'FanDuel'),
                          pull_estimate(sport,'DraftKings')]))
            refs.append(dict(output='summary-source-fanduel.disabled',args=['market-summary',sport,'other-game',catalog,data],
                expected=[True]*4+['Select a game']*4+[pull_estimate(sport,'FanDuel'),pull_estimate(sport,'DraftKings')]))
        self.assertEqual(run_client(self.app,references=refs,steps=[])['checks'],len(refs))

    def test_draftkings_route_loads_only_its_saved_archive_and_its_pull_stays_isolated(self):
        location='#nfl-draftkings-market'
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='nav-market-summary')):
            catalog,_=self.cb['summary_catalog']('market-summary','NFL',7,0,None,None,location)
        self.assertEqual(catalog['mode'],'draftkings')
        self.kalshi.games.assert_not_called();self.poly.games.assert_not_called()
        snapshot=self.dk.pull.return_value
        self.dk.history.return_value=[snapshot]
        archived='123|'+snapshot['pulled_at']
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-draftkings-view')):
            record,_=self.cb['summary_quotes']('123',catalog,0,'market-summary','NFL',None,
                                              None,0,0,0,0,location,archived)
        self.assertEqual(set(record['providers']),{'DraftKings'})
        self.assertEqual(record['providers']['DraftKings']['snapshot'],snapshot)
        self.dk.pull.assert_not_called()
        options=self.cb['draftkings_snapshot_options'](record)
        self.assertIn(archived,[option['value'] for option in options])
        rows=entries(record)
        self.assertEqual({row['category'] for row in rows},{'Game Lines','Player Props'})
        shown=sections(record,rows,'Game Lines')
        table=native_dom(shown).find('table')[0]
        self.assertIn('summary-table--single-source',table.attrs['class'])
        self.assertIn('DraftKings',str(table))
        self.assertNotIn('FanDuel',str(table))
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-pull-draftkings')):
            refreshed,_=self.cb['summary_quotes']('123',catalog,0,'market-summary','NFL',record,
                                                 None,0,0,0,1,location,archived)
        self.assertEqual(set(refreshed['providers']),{'DraftKings'})
        self.dk.pull.assert_called_once()
        self.fd.pull.assert_not_called();self.fd.latest.assert_not_called()
        self.kalshi.all_quotes.assert_not_called();self.poly.all_quotes.assert_not_called()

    def test_missing_fanduel_can_be_pulled_without_refreshing_other_sources(self):
        self.fd.latest.return_value={};previous,_=self.quotes()
        self.assertIsNone(previous['providers']['FanDuel']['snapshot'])
        for service in (self.fd,self.kalshi,self.poly):service.reset_mock()
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-pull-fanduel')):
            result,_=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',previous,None,1,0,0)
        self.fd.pull.assert_called_once();self.fd.latest.assert_not_called()
        self.assertIn(':FanDuel:1',self.fd.pull.call_args.args[1])
        for provider,service in [('Kalshi',self.kalshi),('Polymarket',self.poly)]:
            service.all_quotes.assert_not_called();service.games.assert_not_called()
            self.assertEqual(result['providers'][provider],previous['providers'][provider])
        self.assertIsNotNone(result['providers']['FanDuel']['snapshot'])

    def test_individual_exchange_pull_never_calls_fanduel_or_the_other_exchange(self):
        previous,_=self.quotes()
        for provider,service,counts in [('Kalshi',self.kalshi,(0,1,0)),('Polymarket',self.poly,(0,0,1))]:
            with self.subTest(provider=provider):
                for candidate in (self.fd,self.kalshi,self.poly):candidate.reset_mock()
                with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-pull-'+provider.lower())):
                    result,_=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',previous,None,*counts)
                service.all_quotes.assert_called_once_with(exchange(provider),refresh=True,save=True)
                self.fd.pull.assert_not_called();self.fd.latest.assert_not_called()
                other=self.poly if provider=='Kalshi' else self.kalshi
                other.all_quotes.assert_not_called()
                for name in ('FanDuel','Kalshi','Polymarket'):
                    if name!=provider:self.assertEqual(result['providers'][name],previous['providers'][name])

    def test_fanduel_pull_keeps_exchange_snapshots_when_schedule_metadata_changes(self):
        previous=self.quotes()[0];catalog=deepcopy(self.catalog)
        catalog['games'][0]['venue']='Updated venue'
        catalog['games'][0]['home']['id']='18'
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-pull-fanduel')):
            result,_=self.cb['summary_quotes']('123',catalog,0,'market-summary','NFL',previous,None,1,0,0)
        for provider in ('Kalshi','Polymarket'):
            self.assertEqual(result['providers'][provider],previous['providers'][provider])

    def test_expired_reference_cannot_erase_other_sources_or_spend_credits(self):
        previous=dict(id='expired',quote_ref='released',sport='NFL',game=game())
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-pull-fanduel')):
            result,status=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',previous,None,1,0,0)
        self.assertIs(result,no_update);self.fd.pull.assert_not_called();self.assertIn('No pull was made',str(status))

    def test_failed_individual_pull_keeps_saved_quotes_and_model_identity(self):
        previous,_=self.quotes();before=deepcopy(previous)
        self.fd.pull.side_effect=DataSourceError('Credit allowance exhausted')
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-pull-fanduel')):
            result,status=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',previous,None,1,0,0)
        self.assertEqual(result['id'],previous['id'])
        self.assertEqual(result['providers']['FanDuel']['snapshot'],previous['providers']['FanDuel']['snapshot'])
        self.assertIn('Saved quotes kept',str(status));self.assertIn('Credit allowance exhausted',result['providers']['FanDuel']['error'])
        self.assertEqual(previous,before)
        result['updating']=True
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-cache-poll')):
            unchanged,status=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',result,1,1,0,0)
        self.assertIs(unchanged,no_update);self.assertIn('Credit allowance exhausted',str(status))
        self.fd.pull.assert_called_once()

    def test_individual_pull_without_previous_bundle_does_not_load_other_sources(self):
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-pull-fanduel')):
            result,_=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',None,None,1,0,0)
        self.fd.pull.assert_called_once();self.fd.latest.assert_not_called()
        self.kalshi.all_quotes.assert_not_called();self.poly.all_quotes.assert_not_called()
        self.assertIsNone(result['providers']['Kalshi']['snapshot'])
        self.assertIsNone(result['providers']['Polymarket']['snapshot'])

    def test_unmatched_exchange_pull_keeps_other_sources_and_spends_no_credits(self):
        previous,_=self.quotes();catalog={**self.catalog,'Kalshi':dict(games=[])}
        for service in (self.fd,self.kalshi,self.poly):service.reset_mock()
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-pull-kalshi')):
            result,status=self.cb['summary_quotes']('123',catalog,0,'market-summary','NFL',previous,None,0,1,0)
        self.kalshi.all_quotes.assert_not_called();self.poly.all_quotes.assert_not_called();self.fd.pull.assert_not_called()
        self.assertIn('No unambiguous matching event',str(status))
        self.assertEqual(result['providers']['Polymarket'],previous['providers']['Polymarket'])

    def test_previous_source_click_count_cannot_trigger_another_paid_pull_on_poll(self):
        self.kalshi.all_quotes.return_value['updating']=True
        previous,_=self.quotes()
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-cache-poll')):
            self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',previous,1,1,0,0)
        self.fd.pull.assert_not_called()

    def test_provider_failures_and_wrong_games_do_not_hide_other_quotes(self):
        self.kalshi.all_quotes.side_effect=DataSourceError('Provider offline')
        self.poly.all_quotes.return_value=dict(self.poly.all_quotes.return_value,game_id='another-game')
        quotes,status=self.quotes()
        self.assertIsNotNone(quotes['providers']['FanDuel']['snapshot'])
        self.assertIsNone(quotes['providers']['Kalshi']['snapshot']);self.assertIsNone(quotes['providers']['Polymarket']['snapshot'])
        self.assertIn('Provider offline',str(status));self.assertIn('another game',str(status))

    def test_pending_cache_poll_publishes_fresh_quotes_without_spending_fanduel_credits(self):
        self.kalshi.all_quotes.return_value['updating']=True
        quoted,_=self.quotes();self.assertTrue(quoted['updating'])
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-cache-poll')):
            unchanged,_=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',deepcopy(quoted),1)
            self.assertIs(unchanged,no_update)
            self.kalshi.all_quotes.return_value={**self.kalshi.all_quotes.return_value,'updating':False,'fetched_at':'2026-10-24T12:05:00Z'}
            updated,_=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',quoted,2)
            self.assertFalse(updated['updating']);self.assertNotEqual(updated['id'],quoted['id'])
            with self.assertRaises(PreventUpdate):
                self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',updated,3)
            with self.assertRaises(PreventUpdate):
                self.cb['summary_catalog']('market-summary','NFL',7,0,{**self.catalog,'updating':False},3)
        self.fd.pull.assert_not_called()

    def test_pending_source_poll_does_not_restart_sources_that_already_completed(self):
        previous=self.quotes()[0]
        previous['updating']=True
        previous['providers']['Polymarket']['snapshot']={**previous['providers']['Polymarket']['snapshot'],'updating':True}
        self.fd.latest.reset_mock();self.kalshi.all_quotes.reset_mock();self.poly.all_quotes.reset_mock()
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-cache-poll')):
            completed,_=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',previous,1)
        self.fd.latest.assert_not_called();self.kalshi.all_quotes.assert_not_called()
        self.poly.all_quotes.assert_called_once()
        self.assertEqual(completed['providers']['Kalshi'],previous['providers']['Kalshi'])

    def test_overlapping_completion_polls_publish_one_forecast_identity(self):
        self.kalshi.all_quotes.return_value['updating']=True
        previous=self.quotes()[0]
        self.kalshi.all_quotes.return_value={**self.kalshi.all_quotes.return_value,'updating':False,
            'fetched_at':'2026-10-24T12:05:00Z'}
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-cache-poll')):
            a=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',deepcopy(previous),1)[0]
            b=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',deepcopy(previous),2)[0]
        self.assertEqual(a['id'],b['id']);self.assertNotEqual(a['id'],previous['id'])

    def test_refresh_notices_do_not_invalidate_forecasts_for_identical_prices(self):
        self.kalshi.all_quotes.return_value['updating']=True
        previous=self.quotes()[0]
        self.kalshi.all_quotes.return_value={**self.kalshi.all_quotes.return_value,'updating':False,'notices':['Refresh complete']}
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-cache-poll')):
            completed=self.cb['summary_quotes']('123',self.catalog,0,'market-summary','NFL',previous,1)[0]
        self.assertEqual(completed['id'],previous['id']);self.assertFalse(completed['updating'])

    def test_pending_catalog_poll_bypasses_recent_check_and_stops_after_refresh(self):
        self.schedule.load.return_value.updating=True
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='workspace-page')):
            old,_=self.cb['summary_catalog']('market-summary','NFL',7,0,None)
        self.assertTrue(old['updating']);self.schedule.load.return_value.updating=False
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-cache-poll')):
            fresh,_=self.cb['summary_catalog']('market-summary','NFL',7,0,old,1)
            self.assertFalse(fresh['updating'])
            with self.assertRaises(PreventUpdate):self.cb['summary_catalog']('market-summary','NFL',7,0,fresh,2)

    def test_returning_to_summary_keeps_quotes_models_and_rendered_accordions(self):
        quoted,_=self.quotes()
        with self.assertRaises(PreventUpdate):self.quotes(previous=quoted)
        self.fd.latest.assert_called_once()
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-quotes')):
            models,_=self.cb['summary_models'](quoted,'EPA','OAM',0,'market-summary','NFL','123',None)
        self.engines['EPA'].forecast.assert_called_once();self.players['OAM'].forecast.assert_called_once()
        shown,signature=self.cb['summary_sections'](quoted,models,'All Markets','EPA','OAM','123','market-summary','NFL')
        self.assertIn('Calculated probability',str(shown))
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='workspace-page')):
            with self.assertRaises(PreventUpdate):self.cb['summary_models'](quoted,'EPA','OAM',0,'market-summary','NFL','123',models)
            # Simulate JSON.parse's Number precision, then a callback POST.
            # Unchanged revisions must still reuse this completed forecast.
            browser=json.loads(to_json(models),parse_int=lambda value:int(float(value)))
            with self.assertRaises(PreventUpdate):self.cb['summary_models'](quoted,'EPA','OAM',0,'market-summary','NFL','123',browser)
        self.engines['EPA'].forecast.assert_called_once();self.players['OAM'].forecast.assert_called_once()
        with self.assertRaises(PreventUpdate):self.cb['summary_sections'](quoted,models,'All Markets','EPA','OAM','123','market-summary','NFL',signature)
        with self.assertRaises(PreventUpdate):self.cb['summary_quotes']('123',self.catalog,1,'market','NFL',None)

    def test_background_result_uses_small_scoped_reference_and_recovers_after_eviction(self):
        quoted,_=self.quotes();tasks=self.app._page_tasks
        self.addCleanup(tasks.queue.shutdown)
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-quotes')):
            pending,_=self.cb['summary_models'](quoted,'PPP','WHP',0,'market-summary','NFL','123',None,1)
        tasks.queue.tasks.join()
        event=self.cb['summary_model_poll'](1,pending,'123','market-summary','NFL',None)
        self.assertEqual(event['status'],'complete');self.assertLess(len(to_json(event)),256)
        with self.assertRaises(PreventUpdate):self.cb['summary_model_poll'](2,pending,'123','market-summary','NFL',event)
        with self.assertRaises(PreventUpdate):self.cb['summary_model_poll'](2,pending,'another-game','market-summary','NFL',None)
        poll=next(meta for meta in self.app.callback_map.values() if meta.get('callback') and meta['callback'].__wrapped__.__name__=='summary_model_poll')
        self.assertNotIn('summary-quotes',{item['id'] for item in poll['inputs']+poll['state']})
        request=next(meta for meta in self.app.callback_map.values() if meta.get('callback') and meta['callback'].__wrapped__.__name__=='summary_models')
        self.assertNotIn('summary-model-poll',{item['id'] for item in request['inputs']})
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-model-poll')):
            result,_=self.cb['summary_models'](quoted,'PPP','WHP',0,'market-summary','NFL','123',pending,event)
            self.assertNotIn('rows',result);self.assertLess(len(to_json(result)),3000)
            browser=json.loads(to_json(result),parse_int=lambda value:int(float(value)))
            with self.assertRaises(PreventUpdate):
                self.cb['summary_models'](quoted,'PPP','WHP',0,'market-summary','NFL','123',browser,3)
        shown,_=self.cb['summary_sections'](quoted,browser,'All Markets','PPP','WHP','123','market-summary','NFL')
        identities,visible=table_states(shown)
        cells=self.cb['summary_model_cells'](browser,'PPP','WHP',visible,'market-summary','NFL',quoted,'123',identities)
        self.assertIn('70.00%',str(cells));self.engines['PPP'].forecast.assert_called_once()
        wrong={**browser,'bundle_id':'another-game'}
        with self.assertRaises(PreventUpdate):self.cb['summary_model_cells'](wrong,'PPP','WHP',visible,'market-summary','NFL',quoted,'123',identities)
        with tasks.lock:tasks.values.pop(browser['job'])
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-model-poll')):
            retry,_=self.cb['summary_models'](quoted,'PPP','WHP',0,'market-summary','NFL','123',browser,4)
        self.assertTrue(retry['pending']);self.assertTrue(retry['clear'])
        cleared=self.cb['summary_model_cells'](retry,'PPP','WHP',visible,'market-summary','NFL',quoted,'123',identities)
        self.assertNotIn('70.00%',str(cleared))
        tasks.queue.tasks.join();self.assertIsNotNone(tasks.peek(browser['job']))
        self.engines['PPP'].forecast.assert_called_once()
        with patch.object(tasks,'read',return_value=(None,False,'History unavailable')),\
                patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-recalculate')):
            failed,_=self.cb['summary_models'](quoted,'PPP','WHP',1,'market-summary','NFL','123',browser,5)
        self.assertEqual(failed['status'],'error')
        self.assertNotIn('70.00%',str(self.cb['summary_model_cells'](failed,'PPP','WHP',visible,'market-summary','NFL',quoted,'123',identities)))
        with self.assertRaises(PreventUpdate):
            self.cb['summary_model_cells']({**failed,'bundle_id':'old-game'},'PPP','WHP',visible,'market-summary','NFL',quoted,'123',identities)

    def test_catalogs_load_in_parallel_and_game_cards_select_and_return_to_list(self):
        barrier=Barrier(3,timeout=5)
        def scheduled(*args,**kwargs):barrier.wait();return SimpleNamespace(games=[game()],failed_days=[],stale_days=[])
        def k(*args,**kwargs):barrier.wait();return dict(games=[exchange()])
        def p(*args,**kwargs):barrier.wait();return dict(games=[exchange('Polymarket')])
        self.schedule.load.side_effect=scheduled;self.kalshi.games.side_effect=k;self.poly.games.side_effect=p
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='workspace-page')):
            catalog,status=self.cb['summary_catalog']('market-summary','NFL',7,0,None)
        self.assertEqual(len(catalog['games']),1)
        self.assertEqual(len(self.cb['summary_options'](catalog)),1)

    def test_late_catalog_response_cannot_overwrite_a_newer_game_click(self):
        from check_navigation_pipeline import node,run_client
        if not node():self.skipTest('Node unavailable')
        refs=[]
        for trigger,clicks,previous,expected in [
            (dict(type='summary-game-choice',game='123'),[1],None,'123'),
            ('summary-catalog',[1],'123',{'_dash_no_update':'_dash_no_update'}),
            ('summary-catalog',[],None,{'_dash_no_update':'_dash_no_update'}),
            ('summary-catalog',[],'old-game',None),('summary-back',[],'123',None)]:
            refs.append(dict(output='summary-game.value',args=[self.catalog,clicks,1,previous],
                context=dict(triggered_id=trigger),expected=expected))
        self.assertEqual(run_client(self.app,references=refs,steps=[])['checks'],len(refs))

    def test_real_dash_http_quote_model_and_table_callbacks_serialize(self):
        values={'summary-game':'123','summary-catalog':self.catalog,'summary-pull':0,'workspace-page':'market-summary','sport':'NFL',
                'summary-quotes':None,'summary-models':None,'summary-game-method':'PPP','summary-player-method':'WHP',
                'summary-recalculate':0,'summary-category':'All Markets','summary-rendered':None,'summary-cache-poll':0}
        with self.app.server.test_client() as client:
            for name in ['summary_quotes','summary_models','summary_sections']:
                key,meta=next((k,m) for k,m in self.app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__==name)
                def send(trigger):
                    return client.post('/_dash-update-component',json=dict(output=key,
                        outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                        inputs=[dict(**item,value=wire_value(item,values)) for item in meta['inputs']],
                        state=[dict(**item,value=wire_value(item,values)) for item in meta['state']],changedPropIds=[trigger]))
                response=send('summary-game.value')
                self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                result=response.get_json()['response']
                if name=='summary_quotes':
                    values['summary-quotes']=result['summary-quotes']['data'];deadline=perf_counter()+5
                    while values['summary-quotes']['updating'] and perf_counter()<deadline:
                        values['summary-cache-poll']+=1
                        response=send('summary-cache-poll.n_intervals')
                        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
                        update=response.get_json()['response'].get('summary-quotes')
                        if update:values['summary-quotes']=update['data']
                        sleep(.005)
                    self.assertFalse(values['summary-quotes']['updating'])
                    self.assertIn('quote_ref',values['summary-quotes'])
                    self.assertNotIn('rows',values['summary-quotes']['providers']['Kalshi']['snapshot'])
                elif name=='summary_models':values['summary-models']=result['summary-models']['data']
                else:self.assertIn('Calculated odds',str(result));self.assertIn('Polymarket',str(result))
        self.fd.pull.assert_not_called()

    def test_quote_view_is_independent_of_forecasts_and_model_completion_only_updates_cells(self):
        raw_meta=next(meta for meta in self.app.callback_map.values() if meta.get('callback') and
                      meta['callback'].__wrapped__.__name__=='summary_sections')
        self.assertNotIn('summary-models',{item['id'] for item in raw_meta['inputs']})
        quoted,_=self.quotes()
        raw,_=self.cb['summary_sections'](quoted,None,'All Markets','PPP','WHP','123','market-summary','NFL')
        identities,visible=table_states(raw)
        row_count=sum(len(json.loads(rows)) for rows in visible)
        self.assertLess(len(identities),3*row_count);self.assertIn('Market odds',str(raw))
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-quotes')):
            models,_=self.cb['summary_models'](quoted,'PPP','WHP',0,'market-summary','NFL','123',None)
        key,meta=next((key,meta) for key,meta in self.app.callback_map.items() if meta.get('callback') and
                      meta['callback'].__wrapped__.__name__=='summary_model_cells')
        pattern=next(item['id'] for item in meta['inputs'] if item['id'].startswith('{'))
        values={'summary-models':models,'summary-game-method':'PPP','summary-player-method':'WHP',pattern:visible,
                'workspace-page':'market-summary','sport':'NFL','summary-quotes':quoted,'summary-game':'123'}
        with self.app.server.test_client() as client:
            response=client.post('/_dash-update-component',json=dict(output=key,
                outputs=[dict(id=identity,property=meta['output'].component_property) for identity in identities],
                inputs=[dict(**item,value=wire_value(item,values)) for item in meta['inputs']],
                state=[dict(**item,value=identities if item['id']==pattern else wire_value(item,values)) for item in meta['state']],changedPropIds=['summary-models.data']))
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        data=response.get_json()['response']
        self.assertEqual(len(data),len(identities));self.assertIn('70.00%',str(data));self.assertNotIn('summary-market-sections',data)
        self.assertEqual(sum(len(json.loads(v['data-models'])) for v in data.values()),row_count)
        with self.assertRaises(PreventUpdate):
            self.cb['summary_model_cells'](models,'EPA','OAM',visible,'market-summary','NFL',quoted,'123',identities)
        self.fd.pull.assert_not_called()

    def test_shared_cells_update_to_best_edge_source_without_changing_row_identity(self):
        self.poly.all_quotes.return_value['rows'][0]['yes_ask']=.4
        quoted,_=self.quotes()
        raw,_=self.cb['summary_sections'](quoted,None,'Game Lines','PPP','WHP','123','market-summary','NFL')
        identities,visible=table_states(raw)
        with patch('sports_analytics.market_summary_callbacks.ctx',SimpleNamespace(triggered_id='summary-quotes')):
            models,_=self.cb['summary_models'](quoted,'PPP','WHP',0,'market-summary','NFL','123',None)
        values=self.cb['summary_model_cells'](models,'PPP','WHP',visible,'market-summary','NFL',quoted,'123',identities)
        by_row={key:metrics for table in values for key,metrics in json.loads(table).items()}
        by_metric=dict(zip(('probability','odds','edge'),by_row[json.dumps(['Kalshi',0,'yes'],separators=(',',':'))]))
        self.assertIn('70.00%',by_metric['probability']);self.assertIn('-233',by_metric['odds'])
        self.assertIn('+30.00 pp',by_metric['edge']);self.assertIn('Polymarket',by_metric['edge'])


if __name__=='__main__':main()
