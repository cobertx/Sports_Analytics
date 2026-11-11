"""Selection semantics and refresh continuity, with no external requests."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest import TestCase,main
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import no_update,Patch
from sports_analytics.data import DataSourceError
from sports_analytics.kalshi_labels import selection,selection_rows
from sports_analytics.exchange_render import render
from sports_analytics.lazy_markets import LazyMarkets
import check_kalshi as fixtures
from check_kalshi import contract,raw_market,GAME,SPREAD
from check_exchange_updates import apply_patch,normalized,expand_sections


class KalshiLadderChecks(TestCase):
    def setUp(self):
        self.fixture=fixtures.KalshiChecks();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.game=self.fixture.game
        self.snapshot=dict(self.fixture.service.quotes(self.game),game=self.game,selection='live')

    def total(self,line,kind='greater',family='TOTAL',**changes):
        event='KXNFL'+family+'-TEST'
        return contract(raw_market(event,ticker=event+str(line),floor_strike=line,strike_type=kind,
                        yes_sub_title=f'Over {line} points',no_sub_title=f'Over {line} points',
                        title=f'Over {line} points',**changes),dict(event_ticker=event,title='Pittsburgh at New Orleans: Total points'),'NFL')

    def test_unders_and_overs_sort_separately_and_retain_the_actual_quote_side(self):
        rows=[self.total(line) for line in (100.5,9.5,40.5)]
        shown=selection_rows(rows,self.snapshot)
        self.assertEqual([r['_display_label'] for r in shown],
                         [f'{side} {line}' for side in ('Under','Over') for line in (9.5,40.5,100.5)])
        self.assertEqual([r['_display_side'] for r in shown],['no']*3+['yes']*3)
        self.assertEqual(rows[0]['yes_label'],'Over 100.5 points')

    def test_equality_stays_on_the_correct_side_without_a_sportsbook_push(self):
        for kind,line,side in [('greater',40.5,'Over'),('greater_or_equal',39.5,'Over'),
                               ('less',39.5,'Under'),('less_or_equal',40.5,'Under')]:
            row=self.total(40,kind)
            self.assertEqual(selection(row,'yes',self.snapshot)[0],f'{side} {line}')
            self.assertEqual(selection(row,'no',self.snapshot)[0],f'{"Under" if side=="Over" else "Over"} {line}')

    def test_team_spreads_reverse_teams_and_signs_and_sort_each_team_numerically(self):
        rows=[contract(raw_market(SPREAD,ticker=SPREAD+str(line),floor_strike=line,strike_type='greater',
                         yes_sub_title=f'Pittsburgh wins by over {line} points'),
                         dict(event_ticker=SPREAD,title='Spread'),'NFL') for line in (3.5,10.5,7.5)]
        shown=selection_rows(rows,self.snapshot)
        self.assertEqual([r['_display_label'] for r in shown],
            ['New Orleans Saints +3.5','New Orleans Saints +7.5','New Orleans Saints +10.5',
             'Pittsburgh Steelers -10.5','Pittsburgh Steelers -7.5','Pittsburgh Steelers -3.5'])
        inverse=dict(rows[0],strike_type='less')
        self.assertEqual(selection(inverse,'yes',self.snapshot)[0],'New Orleans Saints +3.5')
        self.assertEqual(selection(inverse,'no',self.snapshot)[0],'Pittsburgh Steelers -3.5')

    def test_player_team_total_and_unknown_contracts_keep_context_and_rules(self):
        player=dict(self.total(20,'greater_or_equal','PASSYDS'),event_title='Player One: Passing yards')
        self.assertEqual(selection(player,'yes',self.snapshot)[0],'Player One Over 19.5')
        team=dict(self.total(20.5,family='TEAMTOTAL'),event_title='Pittsburgh: Team points')
        self.assertEqual(selection(team,'no',self.snapshot)[0],'Pittsburgh Under 20.5')
        unknown=dict(player,cap_strike=30,strike_type='between',yes_label='20 to 30 yards',no_label='No')
        self.assertEqual(selection(unknown,'yes',self.snapshot)[0],'20 to 30 yards')
        self.assertEqual(selection(unknown,'no',self.snapshot)[0],'Not: 20 to 30 yards')
        winner=dict(self.snapshot['rows'][0],no_label='No')
        self.assertEqual(selection(winner,'yes',self.snapshot)[0],'Pittsburgh Steelers')
        self.assertEqual(selection(winner,'no',self.snapshot)[0],'New Orleans Saints')

    def test_price_patch_updates_the_separate_ladders_including_expanded_rows(self):
        snapshot=dict(self.snapshot,rows=[self.total(n+.5) for n in range(10)])
        first=render(snapshot);expand_sections(first[3])
        changed=deepcopy(snapshot);changed['rows'][0].update(yes_ask=.7,no_ask=.4)
        second=render(changed,first[-1])
        self.assertIsInstance(second[3],Patch)
        from sports_analytics.kalshi_view import market_sections
        self.assertEqual(apply_patch(first[3],second[3]),normalized(expand_sections(market_sections(changed))))

    def test_incomplete_raw_response_cannot_replace_the_good_persistent_cache(self):
        original=self.fixture.service.quotes(self.game)
        self.fixture.clock+=timedelta(seconds=3)
        self.fixture.client.json.side_effect=None
        self.fixture.client.json.return_value=dict(events=[],cursor='')
        failed=self.fixture.service.quotes(self.game,refresh=True)
        self.assertEqual(failed['rows'],original['rows'])
        self.assertEqual(failed['fetched_at'],original['fetched_at'])
        self.assertIn('omitted',str(failed['notices']))
        self.fixture.client.json.return_value=dict(events=[dict(event_ticker=t,markets=[]) for t in (GAME,SPREAD)],cursor='')
        failed=self.fixture.service.quotes(self.game,refresh=True)
        self.assertEqual(failed['rows'],original['rows'])
        self.assertIn('empty contract list',str(failed['notices']))

    def test_stale_markers_patch_rows_and_exports_then_clear_on_recovery(self):
        first=render(self.snapshot,table_open=True)
        stale=deepcopy(self.snapshot)
        stale['rows'][0].update(quote_stale=True,quote_fetched_at=stale['fetched_at'])
        stale['fetched_at']='2026-10-23T20:05:00+00:00'
        second=render(stale,first[-1],table_open=True)
        from sports_analytics.kalshi_view import market_sections,table
        self.assertEqual(apply_patch(first[3],second[3]),normalized(market_sections(stale)))
        self.assertEqual(apply_patch(first[0],second[0]),normalized(table(stale)[0]))
        recovered=dict(self.snapshot,fetched_at=stale['fetched_at'])
        third=render(recovered,second[-1],table_open=True)
        self.assertEqual(apply_patch(apply_patch(first[0],second[0]),third[0]),normalized(table(recovered)[0]))

    def test_active_lazy_view_is_not_evicted_by_other_views(self):
        markets=LazyMarkets(capacity=2)
        active=markets.view();markets.panel(active,0,lambda count:[],20)
        markets.view();self.assertTrue(markets.exists(active));markets.view()
        self.assertTrue(markets.exists(active))
        markets.view();markets.update(active,0,lambda count:[]);markets.view()
        self.assertTrue(markets.exists(active))


class KalshiRefreshChecks(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=fixtures.KalshiChecks();cls.fixture.setUp()
        cls.app,cls.callbacks=cls.fixture.app()

    @classmethod
    def tearDownClass(cls):cls.fixture.doCleanups()

    def setUp(self):
        self.game=self.fixture.game
        self.snapshot=dict(self.fixture.service.quotes(self.game),selection='live')

    def refresh(self,snapshot,previous=None,game=None,category='Game Lines'):
        with (patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-quote-timer')),
              patch.object(self.fixture.service,'quotes',return_value=snapshot)):
            return self.callbacks['kalshi_quotes']('kalshi','NFL',game or self.game,category,'live',1,0,5,
                       None,None,None,previous or self.snapshot)[0]

    def test_empty_partial_recovery_and_reordered_responses_keep_selections_and_timestamps(self):
        empty=dict(self.snapshot,rows=[],fetched_at=None,updating=True)
        retained=self.refresh(empty)
        self.assertEqual([r['ticker'] for r in retained['rows']],[r['ticker'] for r in self.snapshot['rows']])
        self.assertTrue(all(r['quote_stale'] for r in retained['rows']))
        self.assertTrue(all(r['quote_fetched_at']==self.snapshot['fetched_at'] for r in retained['rows']))
        self.assertEqual(retained['fetched_at'],self.snapshot['fetched_at'])
        partial=dict(self.snapshot,rows=[dict(self.snapshot['rows'][0],yes_ask=.8)])
        retained=self.refresh(partial)
        self.assertEqual(retained['rows'][0]['yes_ask'],.8)
        self.assertTrue(retained['rows'][1]['quote_stale'])
        recovered=self.refresh(dict(self.snapshot,rows=list(reversed(self.snapshot['rows']))),retained)
        self.assertEqual(recovered['rows'],self.snapshot['rows'])

    def test_another_game_or_category_never_inherits_retained_markets(self):
        empty=dict(self.snapshot,rows=[],game_id=GAME+'OTHER')
        result=self.refresh(empty,game=dict(self.game,id=GAME+'OTHER'))
        self.assertEqual(result['rows'],[])
        empty=dict(self.snapshot,rows=[],category='Passing Props')
        self.assertEqual(self.refresh(empty,category='Passing Props')['rows'],[])

    def test_catalog_failure_and_missing_active_game_or_category_do_not_reset_tabs(self):
        catalog=dict(sport='NFL',window_days=7,games=[self.game])
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='kalshi-list-timer')):
            with patch.object(self.fixture.service,'games',side_effect=DataSourceError('offline')):
                output=self.callbacks['kalshi_games']('kalshi','NFL',7,0,1,catalog)
            self.assertIs(output[0],no_update);self.assertIs(output[2],no_update)
            output=self.callbacks['kalshi_game'](dict(catalog,games=[]),'#nfl-kalshi-game-'+GAME,'NFL',self.game,'Game Lines')
            self.assertTrue(all(value is no_update for value in output))
            changed=dict(self.game,events=[GAME])
            output=self.callbacks['kalshi_game'](dict(catalog,games=[changed]),'#nfl-kalshi-game-'+GAME,'NFL',self.game,'Passing Props')
            self.assertTrue(all(value is no_update for value in output))


if __name__=='__main__':main()
