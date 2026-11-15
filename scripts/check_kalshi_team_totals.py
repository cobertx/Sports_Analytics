"""Team-score marginal math and named player ladders, using offline fixtures."""
from copy import deepcopy
from pathlib import Path
from statistics import NormalDist
from types import SimpleNamespace
import math
import sys
from unittest import TestCase,main
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from models.Game_Outcome.PPP.PPP import PPPForecast,PPPError
from models.Game_Outcome.PPP.NBA_PPP import NBAPPPForecast
from models.Game_Outcome.EPA.EPA import EPAForecast
from models.Game_Outcome.EPA.NBA_EPA import NBAEPAForecast
from models.Game_Outcome.ELO.ELO import ELOForecast,NBAELOForecast
from sports_analytics.kalshi_analysis import adapter,calculate,decorate,quick_calculate,player_subject
from sports_analytics.kalshi_labels import selection,selection_rows
from sports_analytics.kalshi_view import market_sections,market_groups,odds_text
from sports_analytics.exchange_render import render
from sports_analytics.quick_methods import prompt_key,plan
from sports_analytics.availability_adjustment import AvailabilityAdjuster
from sports_analytics.market_summary import comparison
from check_exchange_updates import apply_patch,normalized,expand_sections
from check_kalshi import components
import check_kalshi_analysis as fixtures
import check_kalshi_model_display as display_fixtures


def forecast(cls=PPPForecast):
    return cls('NO','PIT',30,20,10,math.sqrt(112),dict(score_covariance=[[81,3],[3,25]]))


def team_row(team='Pittsburgh',line=23.5,kind='greater'):
    return dict(fixtures.row(),ticker='KXNFLTEAMTOTAL-TEST-'+team+str(line),event_ticker='KXNFLTEAMTOTAL-TEST',
        title=f'{team} over {line} points scored',yes_label=f'{team} over {line} points',line=line,strike_type=kind)


class TeamScoreMathChecks(TestCase):
    def test_each_method_uses_the_selected_team_variance_not_game_total_variance(self):
        for cls in (PPPForecast,NBAPPPForecast,EPAForecast,NBAEPAForecast,ELOForecast,NBAELOForecast):
            f=forecast(cls)
            self.assertAlmostEqual(f.team_total('PIT',23.5).win,1-NormalDist(20,5).cdf(23.5))
            self.assertAlmostEqual(f.team_total('NO',23.5).win,1-NormalDist(30,9).cdf(23.5))
            self.assertNotEqual(f.team_total('PIT',23.5).win,f.total(23.5).win)
            for line in (0,0.5,20,23.5):
                over,under=f.team_total('PIT',line),f.team_total('PIT',line,'Under')
                self.assertAlmostEqual(over.win+over.loss+over.push,1)
                self.assertAlmostEqual(over.win,under.loss);self.assertAlmostEqual(over.loss,under.win)

    def test_missing_or_invalid_covariance_is_explained_without_inventing_uncertainty(self):
        for covariance in (None,[],[[1]],[[1,2],[2,1]],[[1,0],[0,0]],[[1,float('nan')],[0,2]],[[1,1],[0,2]]):
            f=forecast();f.diagnostics['score_covariance']=covariance
            with self.assertRaises(PPPError):f.team_total('PIT',20)
        with self.assertRaises(PPPError):forecast().team_total('Other',20)
        with self.assertRaises(PPPError):forecast().team_total('PIT',20,'Yes')

    def test_team_identity_and_all_strike_equality_cases_keep_yes_no_complements(self):
        f=forecast();game=fixtures.game();engine=Mock(return_value=(f,None));player=Mock()
        for kind in ('greater','greater_or_equal','less','less_or_equal'):
            row=team_row(line=20,kind=kind)
            snapshot=dict(fixtures.snapshot(),rows=[row])
            models=calculate(game,snapshot,'live','PPP','WHP',engine,player)
            shown=decorate(snapshot,game,'live','PPP','WHP',models)['rows'][0]
            expected=(1-NormalDist(20,5).cdf(20.5) if kind=='greater' else
                      1-NormalDist(20,5).cdf(19.5) if kind=='greater_or_equal' else
                      NormalDist(20,5).cdf(19.5) if kind=='less' else NormalDist(20,5).cdf(20.5))
            self.assertAlmostEqual(shown['yes_probability'],expected)
            self.assertAlmostEqual(shown['yes_probability']+shown['no_probability'],1)
        self.assertIsNone(adapter(team_row('Unknown'),'NFL',['Pittsburgh Steelers','New Orleans Saints']))
        player.assert_not_called()

    def test_away_availability_uncertainty_updates_home_away_covariance_in_correct_order(self):
        result=forecast();effect=SimpleNamespace(delta=np.array([1.,2.]),covariance=np.array([[9.,2.],[2.,4.]]),evidence={})
        adjuster=AvailabilityAdjuster(repository=SimpleNamespace(root=Path('.')),store=Mock())
        with (patch('sports_analytics.availability_adjustment.absence_columns',return_value=(pd.DataFrame(),[],[])),
              patch('sports_analytics.availability_adjustment.estimate',side_effect=[None,effect])):
            adjuster._game(dict(sport='NFL',home_team='New Orleans Saints',away_team='Pittsburgh Steelers'),
                result,pd.DataFrame(),pd.DataFrame(),[],None,dict(skipped=[],applied=[]))
        self.assertEqual(result.diagnostics['score_covariance'],[[85.,5.],[5.,34.]])
        self.assertAlmostEqual(result.margin_sd**2,109);self.assertAlmostEqual(result.total_sd**2,129)
        self.assertAlmostEqual(result.team_total('PIT',23.5).win,1-NormalDist(21,math.sqrt(34)).cdf(23.5))

    def test_team_prompt_assignments_and_cross_market_keys_separate_both_teams(self):
        teams=['Pittsburgh Steelers','New Orleans Saints']
        away=adapter(team_row(),'NFL',teams)[0]
        home=adapter(team_row('New Orleans'),'NFL',teams)[0]
        self.assertNotEqual(prompt_key(away),prompt_key(home))
        self.assertEqual(len(plan([away,home],['PPP'],['WHP'])),2)
        a=comparison(away,False,'yes','Kalshi','NFL',teams)
        h=comparison(home,False,'yes','Kalshi','NFL',teams)
        self.assertNotEqual(a['key'],h['key']);self.assertNotEqual(a['group'],h['group'])


class TeamTotalDisplayChecks(TestCase):
    def setUp(self):
        self.d=display_fixtures.ModelDisplayChecks();self.d.setUp();self.addCleanup(self.d.doCleanups)
        self.d.f.snapshot['rows']=[team_row(),team_row('New Orleans')]

    def test_only_team_total_contracts_get_probability_odds_and_edge_for_every_method(self):
        for method in ('PPP','EPA','ELO'):
            self.d.values['kalshi-game-method']=method
            self.d.values['kalshi-rules-signature']=None
            self.d.f.engines[method].forecast.return_value=forecast()
            self.d.complete();identities=self.d.quote_rows();self.assertEqual(len(identities),4)
            for key,value in self.d.overlay(identities).items():
                self.assertNotEqual(value['children'][3]['props']['children'],'—')
                self.assertNotEqual(value['children'][4]['props']['children'],'—')
                self.assertNotEqual(value['children'][5]['props']['children'],'—')
            self.d.f.engines[method].forecast.assert_called_once()
        self.d.f.players['WHP'].forecast.assert_not_called()

    def test_quick_filtering_prices_team_totals_under_all_assigned_methods(self):
        engine=Mock(return_value=(forecast(),None));players=Mock()
        ranked,errors=quick_calculate(self.d.f.game,self.d.f.snapshot,'live',['PPP','EPA','ELO'],['WHP'],
            dict(probability=0,edge=-1),engine,players)
        self.assertEqual(ranked['evaluated'],4);self.assertEqual(len(ranked['matches']),4)
        self.assertTrue(all(len(item['estimates'])==3 for item in ranked['matches']))
        self.assertEqual(errors,[]);self.assertEqual(engine.call_count,3);players.assert_not_called()


class PlayerNameChecks(TestCase):
    def row(self,**changes):
        return dict(fixtures.row(),event_ticker='KXNBAPTS-TEST',event_title='Boston vs Toronto: Points',
            title='Jayson Tatum: 20+ points',yes_label='Jayson Tatum: 20+',category='Player Props',**changes)

    def test_names_are_read_from_contracts_subtitles_and_supported_sentence_variants(self):
        row=self.row()
        variants=[row,dict(row,title='Over 19.5 points'),dict(row,title='Jayson Tatum:\n20+ points'),
            dict(row,title='Will Jayson Tatum record 20+ points?',yes_label='Yes'),
            dict(row,title='Jayson Tatum 20+ points',yes_label='Yes'),
            dict(row,title='Over 19.5 points',yes_label='Yes',no_label='No',subtitle='Jayson Tatum: Points')]
        for value in variants:
            self.assertEqual(player_subject(value),'Jayson Tatum')
            self.assertIn('Jayson Tatum',selection(value,'yes',dict(sport='NBA'))[0])
            self.assertIn('Jayson Tatum',selection(value,'no',dict(sport='NBA'))[0])
        protected=dict(row,event_ticker='KXNBAPTSPROT-TEST')
        self.assertIn('Jayson Tatum',selection(protected,'no',dict(sport='NBA'))[0])
        combined=dict(row,event_ticker='KXNBASTOCK-TEST')
        self.assertIn('Jayson Tatum',selection(combined,'no',dict(sport='NBA'))[0])

    def test_players_have_separate_named_sections_and_keep_names_after_quote_and_model_updates(self):
        a=self.row();b=dict(a,ticker='SECOND',title='Scottie Barnes: 20+ points',yes_label='Scottie Barnes: 20+')
        snapshot=dict(sport='NBA',game_id='KXNBAGAME-TEST',category='Player Props',rows=[a,dict(a,ticker='ALT',line=25.5),b])
        self.assertEqual(len(market_groups(snapshot)),2)
        first=render(snapshot);expand_sections(first[3])
        self.assertEqual([section.children[0].children[0].children for section in first[3]],['Jayson Tatum - Points','Scottie Barnes - Points'])
        revised=deepcopy(snapshot)
        for row in revised['rows']:row.update(yes_ask=.75,yes_probability=.8,no_probability=.2,model_method='WHP')
        second=render(revised,first[-1])
        self.assertEqual(apply_patch(first[3],second[3]),normalized(expand_sections(market_sections(revised))))
        labels=[r['_display_label'] for r in selection_rows(revised['rows'],revised)]
        self.assertTrue(all('Jayson Tatum' in label or 'Scottie Barnes' in label for label in labels))


if __name__=='__main__':main()
