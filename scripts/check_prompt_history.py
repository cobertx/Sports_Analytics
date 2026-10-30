"""Market prompt links, exact threshold outcomes and historical chart integration."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest import TestCase,main
from unittest.mock import patch

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash._utils import to_json
from dash.exceptions import PreventUpdate
from sports_analytics.historical_links import history_target,target_metric
from sports_analytics.prompt_history import button,fanduel_target,exchange_target,outcomes,prior_history
from sports_analytics.market_view import market_sections
from sports_analytics import kalshi_view
from sports_analytics.exchange_render import render
from sports_analytics.dashboard import ALL_TEAMS
import check_market_history as history_fixtures
import check_kalshi as kalshi_fixtures
import check_polymarket as poly_fixtures
from check_exchange_updates import apply_patch,normalized


def snapshot():
    return dict(sport='NFL',game_id='123',home_team='Cleveland Browns',away_team='Baltimore Ravens',
                commence_time='2026-10-25T17:00:00Z',pulled_at='2026-10-24T12:00:00Z',rows=[])


def row(**changes):
    return dict(market_key='player_reception_yds_alternate',group='Receiving',market='Receiving yards',
                subject='Jerry Jeudy',selection='Over',line=52,odds=-110,**changes)


class PromptHistoryChecks(TestCase):
    def test_fanduel_links_preserve_prompt_entity_stat_line_cutoff_and_origin(self):
        s=snapshot();r=row()
        target=history_target(button(s,r).href)
        self.assertEqual((target['sport'],target['kind'],target['name'],target['market']),
                         ('NFL','player','Jerry Jeudy','player_reception_yds_alternate'))
        self.assertEqual((target['line'],target['compare'],target['push']),(52,'gt',True))
        self.assertEqual(target['origin'],'#nfl-game-123');self.assertEqual(target['as_of'],s['commence_time'])
        for sport in ['NBA','NFL','NCAAF']:
            spread=dict(r,market_key='alternate_spreads',subject='Home Team',selection='Home Team',line=-3.5)
            mapped=fanduel_target(dict(s,sport=sport),spread)
            self.assertEqual((mapped['name'],mapped['line'],mapped['compare']),('Home Team',3.5,'gt'))
        total=dict(r,market_key='alternate_totals',subject='Under',selection='Under',line=45)
        self.assertEqual(fanduel_target(s,total)['name'],'Cleveland Browns')
        self.assertEqual(fanduel_target(s,total)['compare'],'lt')

    def test_every_fanduel_selection_has_button_including_more_lines_and_scorer_reference(self):
        s=snapshot();s['rows']=[dict(row(),line=i+.5) for i in range(8)]
        shown=market_sections(s,'Receiving')
        buttons=[c for c in history_fixtures.components(shown) if 'market-history-button' in (getattr(c,'className','') or '').split()]
        self.assertEqual(len(buttons),8)
        self.assertEqual(sorted(history_target(b.href)['line'] for b in buttons),[i+.5 for i in range(8)])
        scorer=dict(row(),market_key='player_1st_td',selection='Yes',line=None)
        target=history_target(button(s,scorer).href)
        self.assertNotIn('line',target);self.assertIn('Scorer-order history',target['note'])
        self.assertTrue(button(s,dict(row(),market_key='unmapped')).disabled)

    def test_exact_binary_complements_and_sportsbook_pushes(self):
        history=pd.DataFrame({'value':[0,1,2,3,None]})
        for compare,expected in [('gt',['Miss','Miss','Miss','Hit',None]),('ge',['Miss','Miss','Hit','Hit',None]),
                                 ('lt',['Hit','Hit','Miss','Miss',None]),('le',['Hit','Hit','Hit','Miss',None])]:
            with self.subTest(compare=compare):
                target=dict(sport='NBA',kind='player',market='player_points',line=2,compare=compare,push=False)
                values=history.rename(columns={'value':'pts'})
                self.assertEqual(outcomes(values,'pts',target).tolist(),expected)
        target.update(compare='gt',push=True)
        self.assertEqual(outcomes(values,'pts',target).tolist(),['Miss','Miss','Push','Hit',None])
        self.assertIsNone(outcomes(values,'other',target))
        # A missing margin or total cannot silently fall back to points/fouls.
        self.assertIsNone(target_metric(dict(kind='team',sport='NBA',market='alternate_totals',line=200),['pts','pf']))
        self.assertIsNone(target_metric(dict(kind='team',sport='NBA',market='team_total',line=100),['pf']))

    def test_linked_game_and_future_dates_are_excluded_even_with_date_only_boxes(self):
        frame=pd.DataFrame(dict(event_date=pd.to_datetime(['2026-10-23T00:00:00Z','2026-10-24T00:00:00Z','2026-10-25T00:00:00Z'])))
        selected=prior_history(frame,{'as_of':'2026-10-25T00:30:00Z'}) # October 24 Eastern.
        self.assertEqual(selected.index.tolist(),[0])
        frame=pd.DataFrame(dict(event_date=pd.to_datetime(['2026-10-24T00:30:00Z','2026-10-24T18:00:00Z']),
                                available_at=pd.to_datetime(['2026-10-24T04:00:00Z','2026-10-24T21:00:00Z'])))
        self.assertEqual(prior_history(frame,{'as_of':'2026-10-24T20:00:00Z'}).index.tolist(),[0])

    def test_kalshi_sides_select_correct_raw_threshold_and_team_total_stat(self):
        f=kalshi_fixtures.KalshiChecks();f.setUp();self.addCleanup(f.doCleanups)
        g=f.service.games('NFL')['games'][0]
        s=dict(f.service.quotes(g),game=g)
        r=dict(s['rows'][0],event_ticker='KXNFLTOTAL-TEST',title='Over 40 points',line=40,strike_type='greater',market_type='binary')
        yes=history_target(button(s,r,'Kalshi','yes').href);no=history_target(button(s,r,'Kalshi','no').href)
        self.assertEqual((yes['line'],yes['compare'],yes['push']),(40,'gt',False))
        self.assertEqual(no['compare'],'le');self.assertEqual(no['market'],'alternate_totals')
        r.update(event_ticker='KXNFLTEAMTOTAL-TEST',yes_label='Pittsburgh over 20.5 points',line=20.5)
        target=exchange_target(s,r,'Kalshi')
        self.assertEqual((target['name'],target['market']),('Pittsburgh Steelers','team_total'))
        self.assertEqual(target_metric(target,['pf','pa']),'pf')
        for compare,inverse in [('greater_or_equal','lt'),('less','ge'),('less_or_equal','gt')]:
            r['strike_type']=compare
            self.assertEqual(exchange_target(s,r,'Kalshi','no')['compare'],inverse)

    def test_polymarket_winner_spreads_and_player_sides_use_actual_labels(self):
        f=poly_fixtures.PolymarketChecks();f.setUp();self.addCleanup(f.doCleanups)
        g=f.game();s=dict(f.service.quotes(g),game=g)
        winner=s['rows'][0];spread=s['rows'][1]
        self.assertEqual(exchange_target(s,winner,'Polymarket','yes')['name'],'Boston Celtics')
        self.assertEqual(exchange_target(s,winner,'Polymarket','no')['name'],'Detroit Pistons')
        self.assertEqual(exchange_target(s,spread,'Polymarket','yes')['line'],3.5)
        self.assertEqual(exchange_target(s,spread,'Polymarket','no')['line'],-3.5)
        player=dict(f.service.quotes(g,'Player Props'),game=g)
        r=player['rows'][0]
        over=history_target(button(player,r,'Polymarket','yes').href)
        under=history_target(button(player,r,'Polymarket','no').href)
        self.assertEqual((over['name'],over['market'],over['line'],over['compare']),('Player One','player_points',20.5,'gt'))
        self.assertEqual(under['compare'],'lt')
        rendered=kalshi_view.market_sections(player,'Polymarket')
        buttons=[c for c in history_fixtures.components(rendered) if 'market-history-button' in (getattr(c,'className','') or '').split()]
        self.assertEqual(len(buttons),2)

    def test_live_patches_retain_history_links_and_only_rule_changes_rebuild(self):
        f=kalshi_fixtures.KalshiChecks();f.setUp();self.addCleanup(f.doCleanups)
        game=f.service.games('NFL')['games'][0]
        s=dict(f.service.quotes(game),game=game)
        before=render(s);updated=deepcopy(s);updated['rows'][0]['yes_ask']=.6
        after=render(updated,before[-1])
        self.assertEqual(apply_patch(before[3],after[3]),normalized(kalshi_view.market_sections(updated)))
        self.assertIn('Historical Data',to_json(after[3]));self.assertNotIn("'open'",str(normalized(after[3])))

    def test_prompt_url_validation_rejects_nonfinite_lines_bad_dates_and_external_return(self):
        href=button(snapshot(),row()).href
        for bad in [href.replace('line=52','line=nan'),href.replace('compare=gt','compare=bad'),
                    href.replace('origin=%23nfl-game-123','origin=https%3A%2F%2Fevil.example'),
                    href.replace('as_of=2026-10-25T17%3A00%3A00Z','as_of=bad')]:
            self.assertIsNone(history_target(bad))


class PromptChartChecks(TestCase):
    def setUp(self):
        self.fixture=history_fixtures.MarketHistoryChecks();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.cb=self.fixture.callbacks
        self.dataset=dict(key='fixture',sport='NFL',season=2026,kind='player',phase='regular')
        self.href=button(snapshot(),row()).href

    def test_button_to_chart_hit_rate_line_colors_table_and_return_route(self):
        dataset=self.dataset;cb=self.cb
        entity=cb['select_options'](dataset,ALL_TEAMS,None,self.href)[1]
        category=cb['select_categories'](dataset,entity,ALL_TEAMS,None,None,self.href)[1]
        metric=cb['select_metrics'](dataset,entity,ALL_TEAMS,category,None,self.href)[1]
        figure,cards,records,columns=cb['analyze'](dataset,entity,metric,3,ALL_TEAMS,self.href)
        self.assertEqual(metric,'receiving_yards')
        self.assertEqual(len(records),7)
        self.assertEqual({result:sum(r['prompt_result']==result for r in records) for result in ['Hit','Miss','Push']},dict(Hit=4,Miss=2,Push=1))
        self.assertEqual(figure.layout.shapes[0].y0,52)
        self.assertLess(figure.layout.yaxis.range[0],52);self.assertGreater(figure.layout.yaxis.range[1],52)
        self.assertIn('4/7 historical hits',str(figure.layout.annotations))
        self.assertEqual(list(figure.data[0].marker.color),['#e77c85','#e77c85','#a6b2c5',*['#57dbc4']*4])
        self.assertIn('PROMPT HIT RATE',str(cards));self.assertIn('#nfl-game-123',str(cards))
        self.assertEqual(columns[-1]['id'],'prompt_result')
        normal=cb['analyze'](dataset,entity,metric,3,ALL_TEAMS)
        self.assertFalse(normal[0].layout.shapes);self.assertEqual(len(normal[1]),3)
        self.assertNotIn('prompt_result',normal[2][0])

    def test_same_entity_new_line_updates_via_actual_dash_http_callback(self):
        meta_key,meta=next((key,m) for key,m in self.fixture.app.callback_map.items() if m.get('callback') and m['callback'].__wrapped__.__name__=='analyze')
        for line,expected in [(52,4),(54.5,2)]:
            r=dict(row(),line=line);href=button(snapshot(),r).href
            values={'dataset':self.dataset,'entity':'j','metric':'receiving_yards','window':3,'player-team':ALL_TEAMS,'market-url':href,'workspace-page':'historical'}
            with self.fixture.app.server.test_client() as client:
                response=client.post('/_dash-update-component',json=dict(output=meta_key,
                    outputs=[dict(id=o.component_id,property=o.component_property) for o in meta['output']],
                    inputs=[dict(**item,value=values[item['id']]) for item in meta['inputs']],
                    state=[dict(**item,value=values[item['id']]) for item in meta['state']],changedPropIds=['market-url.hash']))
            self.assertEqual(response.status_code,200,response.get_data(as_text=True))
            figure=response.get_json()['response']['trend']['figure']
            self.assertEqual(figure['layout']['shapes'][0]['y0'],line)
            self.assertIn(f'{expected}/7 historical hits',str(figure['layout']['annotations']))

    def test_future_and_current_game_not_counted_wrong_entity_or_stat_clear_overlay(self):
        self.fixture.frame=pd.concat([self.fixture.frame,self.fixture.frame.assign(event_date=pd.Timestamp('2026-10-25T00:00:00Z'))],ignore_index=True)
        chart=self.cb['analyze'](self.dataset,'j','receiving_yards',3,ALL_TEAMS,self.href)
        self.assertEqual(len(chart[2]),7)
        changed=self.cb['analyze'](self.dataset,'a','receiving_yards',3,ALL_TEAMS,self.href)
        self.assertFalse(changed[0].layout.shapes)
        changed=self.cb['analyze'](self.dataset,'j','receptions',3,ALL_TEAMS,self.href)
        self.assertFalse(changed[0].layout.shapes)

    def test_game_spread_and_total_buttons_chart_actual_margins_and_combined_scores(self):
        self.fixture.frame=pd.DataFrame(dict(sport='NFL',entity_id='CLE',entity_name='Cleveland Browns',team='CLE',opponent='BAL',
            season=2026,season_type='regular',week=[1,2,3,4],game_id=['1','2','3','4'],
            event_date=pd.date_range('2026-09-01',periods=4,freq='7D',tz='UTC'),pf=[20,27,24,10],pa=[17,20,24,13]))
        dataset=dict(self.dataset,kind='team')
        for key,selection,line,metric,hits in [('alternate_spreads','Cleveland Browns',-3,'scoring_margin',1),
                                               ('alternate_totals','Under',44,'game_total',2)]:
            r=dict(row(),market_key=key,subject='Cleveland Browns',selection=selection,line=line)
            href=button(snapshot(),r).href
            entity=self.cb['select_options'](dataset,ALL_TEAMS,None,href)[1]
            category=self.cb['select_categories'](dataset,entity,ALL_TEAMS,None,None,href)[1]
            selected=self.cb['select_metrics'](dataset,entity,ALL_TEAMS,category,None,href)[1]
            self.assertEqual(selected,metric)
            figure,_,records,_=self.cb['analyze'](dataset,entity,selected,3,ALL_TEAMS,href)
            self.assertEqual(sum(r['prompt_result']=='Hit' for r in records),hits)
            self.assertEqual(figure.layout.shapes[0].y0,3 if key=='alternate_spreads' else 44)

    def test_nba_combination_counts_exclude_missing_components(self):
        self.fixture.frame=pd.DataFrame(dict(sport='NBA',entity_id='p',entity_name='Player One',team='BOS',opponent='DET',
            season=2026,season_type='regular',week=None,game_id=['1','2','3'],
            event_date=pd.date_range('2026-10-01',periods=3,freq='2D',tz='UTC'),pts=[30,20,None],reb=[5,8,9],ast=[4,6,7]))
        dataset=dict(self.dataset,sport='NBA')
        s=dict(snapshot(),sport='NBA',home_team='Boston Celtics',away_team='Detroit Pistons')
        r=dict(row(),subject='Player One',market_key='player_points_rebounds_assists_alternate',line=35.5)
        href=button(s,r).href
        figure,_,records,_=self.cb['analyze'](dataset,'p','points_rebounds_assists',3,ALL_TEAMS,href)
        self.assertEqual(len(records),2);self.assertEqual(sum(r['prompt_result']=='Hit' for r in records),1)
        self.assertIn('1/2 historical hits',str(figure.layout.annotations))

    def test_all_combo_charts_stack_components_keep_colors_and_score_the_total(self):
        self.fixture.frame=pd.DataFrame(dict(sport='NBA',entity_id='p',entity_name='Player One',team='BOS',opponent='DET',
            season=2026,season_type='regular',week=None,game_id=['1','2','3'],
            event_date=pd.date_range('2026-10-01',periods=3,freq='2D',tz='UTC'),pts=[30,20,None],reb=[5,8,None],ast=[4,6,7]))
        dataset=dict(self.dataset,sport='NBA')
        for metric,fields,total,line in [
            ('points_assists',['pts','ast'],[34,26],30),
            ('points_rebounds',['pts','reb'],[35,28],30),
            ('rebounds_assists',['reb','ast'],[9,14],10),
            ('points_rebounds_assists',['pts','reb','ast'],[39,34],35.5),
        ]:
            s=dict(snapshot(),sport='NBA',home_team='Boston Celtics',away_team='Detroit Pistons')
            r=dict(row(),subject='Player One',market_key='player_'+metric+'_alternate',line=line)
            href=button(s,r).href
            figure,cards,records,columns=self.cb['analyze'](dataset,'p',metric,3,ALL_TEAMS,href)
            bars=[trace for trace in figure.data if trace.type=='bar']
            self.assertEqual(figure.layout.barmode,'stack')
            self.assertEqual(len(bars),len(fields))
            self.assertEqual([sum(trace.y[i] for trace in bars) for i in range(2)],total)
            self.assertEqual(len({trace.marker.color for trace in bars}),len(fields))
            self.assertEqual(sum(record['prompt_result']=='Hit' for record in records),1)
            self.assertIn('1/2 historical hits (50.0%)',str(figure.layout.annotations))
            self.assertIn('PROMPT HIT RATE',str(cards))
            self.assertTrue(set(fields)<={column['id'] for column in columns})
            self.assertTrue(all('Combined total' in trace.hovertemplate for trace in bars))
            self.assertEqual(figure.layout.shapes[0].y0,line)
            self.assertIn('/',bars[0].marker.pattern.shape)

    def test_url_renders_linked_chart_without_waiting_for_stale_dropdowns(self):
        with patch('sports_analytics.dashboard.ctx',SimpleNamespace(triggered_id='market-url')):
            figure,cards,records,_=self.cb['analyze'](self.dataset,'a','receptions',3,'DEN',self.href,'historical')
        self.assertEqual(len(records),7)
        self.assertTrue(all(record['receiving_yards']<100 for record in records))
        self.assertEqual(figure.layout.shapes[0].y0,52)
        self.assertIn('4/7 historical hits',str(figure.layout.annotations))
        self.assertIn('PROMPT HIT RATE',str(cards))
        self.assertEqual(self.cb['select_options'](self.dataset,'DEN','a',self.href)[1],'j')

    def test_college_team_alias_and_paired_game_history_are_selected(self):
        self.fixture.frame=pd.DataFrame([dict(sport='NCAAF',entity_id=name,entity_name=name,team=name,opponent=opponent,
            season=2026,season_type='regular',week=i+1,game_id=str(i),event_date=pd.Timestamp(f'2026-09-{i+1:02}',tz='UTC'),points=score)
            for i,(home,away) in enumerate([(21,17),(31,29)])
            for name,opponent,score in [('Michigan','Alabama',home),('Alabama','Michigan',away)]])
        s=dict(snapshot(),sport='NCAAF',home_team='Michigan Wolverines',away_team='Alabama Crimson Tide')
        r=dict(row(),market_key='alternate_totals',subject='Over',selection='Over',line=50)
        href=button(s,r).href;dataset=dict(self.dataset,sport='NCAAF',kind='team')
        entity=self.cb['select_options'](dataset,ALL_TEAMS,None,href)[1]
        self.assertEqual(entity,'Michigan')
        figure,_,records,_=self.cb['analyze'](dataset,entity,'game_total',3,ALL_TEAMS,href)
        self.assertEqual(len(records),2);self.assertIn('1/2 historical hits',str(figure.layout.annotations))

    def test_market_navigation_does_not_rebuild_hidden_history_charts(self):
        self.fixture.service.read.reset_mock()
        for href,page in [(self.href,'market'),('#nfl-kalshi-market','historical'),('#nfl-game-123','market')]:
            with self.assertRaises(PreventUpdate):self.cb['analyze'](self.dataset,'j','receiving_yards',3,ALL_TEAMS,href,page)
        self.fixture.service.read.assert_not_called()


if __name__=='__main__':main()
