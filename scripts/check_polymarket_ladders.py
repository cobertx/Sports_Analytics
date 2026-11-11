"""Polymarket selection ladders preserve token ownership and refresh continuity."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest import TestCase,main
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash import Patch,no_update
import check_polymarket as fixtures
from check_polymarket import market,components
from check_exchange_updates import apply_patch,normalized,expand_sections
from sports_analytics.polymarket_labels import selection
from sports_analytics.kalshi_labels import selection_rows
from sports_analytics.kalshi_view import market_sections,comparison_rows
from sports_analytics.exchange_render import render
from sports_analytics.prompt_history import exchange_target


class PolymarketLadderChecks(TestCase):
    def setUp(self):
        self.f=fixtures.PolymarketChecks();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.game=self.f.game()
        self.snapshot=dict(self.f.service.quotes(self.game),game=self.game,selection='live')

    def totals(self):
        self.f.events[0]['markets']=[market(str(i),'totals',(str(100+i*2),str(101+i*2)),
            question=f'Total points: O/U {line}',line=line,
            outcomes='["Under","Over"]' if i%2 else '["Over","Under"]')
            for i,line in enumerate((230.5,99.5,210.5,200.5,220.5,240.5,250.5))]
        return dict(self.f.service.quotes(self.game,refresh=True),game=self.game,selection='live')

    def test_totals_sort_under_then_over_numerically_using_each_actual_token_side(self):
        snapshot=self.totals()
        rows=selection_rows(snapshot['rows'],snapshot,'Polymarket')
        self.assertEqual([r['_display_label'] for r in rows],
            [f'{side} {line}' for side in ('Under','Over') for line in (99.5,200.5,210.5,220.5,230.5,240.5,250.5)])
        self.assertEqual((rows[0]['_display_side'],rows[0]['yes_token']),('yes','102'))
        self.assertEqual((rows[4]['_display_side'],rows[4]['no_token']),('no','101'))
        self.assertEqual(snapshot['rows'][0]['yes_label'],'Over')
        self.assertEqual(market_sections(snapshot,'Polymarket')[0].children[0].children[0].children,'Total points')

    def test_winners_spreads_and_reversed_outcomes_have_short_team_labels_and_signed_lines(self):
        winner,spread=self.snapshot['rows']
        self.assertEqual(selection(winner,'yes',self.snapshot)[0],'Boston Celtics')
        self.assertEqual(selection(winner,'no',self.snapshot)[0],'Detroit Pistons')
        self.assertEqual(selection(spread,'yes',self.snapshot)[0],'Boston Celtics -3.5')
        self.assertEqual(selection(spread,'no',self.snapshot)[0],'Detroit Pistons +3.5')
        reversed_row=dict(spread,yes_label='Pistons',no_label='Celtics')
        self.assertEqual(selection(reversed_row,'yes',self.snapshot)[0],'Detroit Pistons +3.5')
        positive=dict(spread,title='Spread: Pistons (+7.5)')
        self.assertEqual(selection(positive,'yes',self.snapshot)[0],'Boston Celtics -7.5')
        explicit=dict(spread,yes_label='Celtics (-4.5)',no_label='Pistons (+4.5)')
        self.assertEqual(selection(explicit,'no',self.snapshot)[0],'Detroit Pistons +4.5')
        titles=[s.children[0].children[0].children for s in market_sections(self.snapshot,'Polymarket')]
        self.assertEqual(titles,['Game winner','Alternate spreads'])

    def test_player_team_totals_and_saved_rows_without_a_line_retain_subject_and_boundary(self):
        player=dict(self.f.service.quotes(self.game,'Player Props'),game=self.game)
        row=player['rows'][0]
        self.assertEqual(selection(row,'yes',player)[0],'Player One Over 20.5')
        self.assertEqual(selection(row,'no',player)[0],'Player One Under 20.5')
        self.assertEqual(exchange_target(player,row,'Polymarket','no')['prompt'],'Player One Under 20.5')
        team=dict(row,sports_market_type='team_totals',title='Celtics: Team Total Over 110',line=110)
        self.assertEqual(selection(team,'no',player)[0],'Celtics Under 110')
        binary=dict(row,yes_label='Yes',no_label='No')
        self.assertEqual(selection(binary,'no',player)[0],'Player One Under 20.5')
        unknown=dict(row,yes_label='Yes',no_label='No',sports_market_type='custom',title='Player One records a triple double',line=None)
        self.assertEqual(selection(unknown,'no',player)[0],'Not: Player One records a triple double')

    def test_player_lines_without_colons_group_into_one_stat_ladder(self):
        self.f.events[1]['markets']=[market(str(i),'points',(str(i*2),str(i*2+1)),
             question=f'Player One Points Over {line}',line=line,outcomes='["Over","Under"]')
             for i,line in enumerate((20.5,10.5,15.5),20)]
        snapshot=dict(self.f.service.quotes(self.game,'Player Props',refresh=True),game=self.game)
        sections=market_sections(snapshot,'Polymarket')
        self.assertEqual(len(sections),1)
        self.assertEqual(sections[0].children[0].children[0].children,'Player One - Points')
        labels=[r['_display_label'] for r in selection_rows(snapshot['rows'],snapshot,'Polymarket')]
        self.assertEqual(labels,[f'Player One {side} {line}' for side in ('Under','Over') for line in (10.5,15.5,20.5)])

    def test_live_patches_update_both_ladders_and_expanded_rows_without_rebuilding(self):
        snapshot=self.totals();before=render(snapshot,provider='Polymarket');expand_sections(before[3])
        revised=deepcopy(snapshot);revised['rows'][0].update(yes_ask=.8,no_ask=.25)
        after=render(revised,before[-1],provider='Polymarket')
        self.assertIsInstance(after[3],Patch)
        self.assertEqual(apply_patch(before[3],after[3]),normalized(expand_sections(market_sections(revised,'Polymarket'))))
        self.assertTrue(all('open' not in op['location'] for op in normalized(after[3])['operations']))
        row=dict(revised['rows'][0],yes_ask=.8,no_ask=.25)
        self.assertEqual([r.children[1].children for r in comparison_rows(row,revised,'Polymarket')],['-400','+300'])

    def test_rendered_rows_have_separate_selections_and_preserve_history_and_structure_buttons(self):
        sections=market_sections(self.snapshot,'Polymarket')
        outcomes=[node for node in components(sections) if 'market-outcome' in (getattr(node,'className','') or '').split()]
        self.assertEqual(len(outcomes),4)
        self.assertTrue(all(len(node.children)==1 for node in components(sections) if getattr(node,'className','')=='kalshi-contract'))
        labels=[node.children[0].children[0].children for node in outcomes]
        self.assertEqual(labels,['Boston Celtics','Detroit Pistons','Boston Celtics -3.5','Detroit Pistons +3.5'])
        self.assertEqual(sum('market-history-button' in (getattr(node,'className','') or '').split() for node in components(sections)),4)

    def test_discovery_and_quote_refreshes_preserve_active_categories_and_loaded_selections(self):
        _,callbacks=self.f.app()
        with patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='polymarket-list-timer')):
            partial=dict(self.game,events=[self.game['events'][0]],categories=['Game Lines'])
            result=callbacks['polymarket_game'](dict(sport='NBA',games=[partial]),
                '#nba-polymarket-game-'+self.game['id'],'NBA',self.game,'Player Props')
            self.assertTrue(all(value is no_update for value in result))
        empty=dict(self.snapshot,rows=[],updating=True,fetched_at=None)
        with (patch('sports_analytics.kalshi_callbacks.ctx',SimpleNamespace(triggered_id='polymarket-quote-timer')),
              patch.object(self.f.service,'quotes',return_value=empty)):
            result=callbacks['polymarket_quotes']('polymarket','NBA',self.game,'Game Lines','live',1,0,5,
                None,None,None,self.snapshot)[0]
        self.assertEqual(len(result['rows']),2)
        self.assertTrue(all(row['quote_stale'] for row in result['rows']))
        self.assertEqual(result['fetched_at'],self.snapshot['fetched_at'])


if __name__=='__main__':main()
