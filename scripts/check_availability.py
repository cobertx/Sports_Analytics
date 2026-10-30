"""Offline injury comparisons, source provenance, probability mass and dispatch."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from models.availability_effects import estimate
from models.Game_Outcome.PPP.PPP import PPPForecast
from models.Player_Stats.Usage_Opportunity.distributions import UsageOpportunityForecast
from models.Player_Stats.Usage_Opportunity.prediction_service import UsageMarketForecast
from models.Player_Stats.Weighted_Historical_Performance.core import WHPForecast
from models.Player_Stats.Weighted_Historical_Performance.prediction_service import PlayerMarketForecast
from sports_analytics.availability import AvailabilityStore, parse
from sports_analytics.data import DataSourceError
from sports_analytics.availability_adjustment import AvailabilityAdjuster, inputs, absence_columns, adjusted_distribution, summary, note
from sports_analytics.availability_view import panel

NOW = datetime(2026,11,12,12,tzinfo=timezone.utc)


def payload(status='Out'):
    return dict(injuries=[dict(displayName='Boston Celtics',injuries=[dict(status=status,date='2026-11-12T10:00Z',
        athlete=dict(id='1',displayName='Player Alpha',team=dict(abbreviation='BOS')))])])


def snapshot():
    return dict(sport='NBA',game_id='upcoming',home_team='Boston Celtics',away_team='New York Knicks',
                pulled_at='2026-11-12T11:00:00Z',commence_time='2026-11-13T00:00:00Z',rows=[])


def history(n=40):
    players,teams = [],[]
    for i in range(n):
        day = pd.Timestamp('2026-10-01',tz='UTC')+pd.Timedelta(days=i)
        absent = bool(i%2 and i<n-1)
        opponent = ['NYK','ATL','LAL'][i%3]
        own_points = 110-12*absent+(i%3)
        other_points = 99+3*absent+(i%3)
        for team,other,points,allowed in [('BOS',opponent,own_points,other_points),(opponent,'BOS',other_points,own_points)]:
            common = dict(game_id=str(i),team=team,opponent=other,event_date=day,
                          available_at=day+pd.Timedelta(hours=8),season=2026,matchup=other+' @ '+team)
            teams.append(dict(**common,entity_id=team,entity_name=team,pts=points,min=240))
            b_minutes = 20+12*absent if team=='BOS' else 0
            a_minutes = 30 if team=='BOS' and not absent else 0
            b_points = 12+8*absent if team=='BOS' else 0
            a_points = 20 if a_minutes else 0
            if b_minutes:players.append(dict(**common,entity_id='B',entity_name='Player Beta',min=b_minutes,pts=b_points))
            if a_minutes:players.append(dict(**common,entity_id='A',entity_name='Player Alpha',min=a_minutes,pts=a_points))
            for j in range(8):
                players.append(dict(**common,entity_id=team+str(j),entity_name=team+' Bench '+str(j),
                                    min=(240-b_minutes-a_minutes)/8,pts=(points-b_points-a_points)/8))
    return pd.DataFrame(players),pd.DataFrame(teams)


class AvailabilityChecks(TestCase):
    def setUp(self):
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fetch = Mock(return_value=payload())
        self.clock = NOW
        self.store = AvailabilityStore(self.root/'availability',fetch=self.fetch,now=lambda:self.clock)
        self.players,self.teams = history()
        self.repo = SimpleNamespace(root=self.root,load=Mock(return_value=(self.players,self.teams)))
        self.engine = AvailabilityAdjuster(self.repo,self.store,nfl_directory=self.root/'raw')
        self.network = patch('requests.sessions.Session.request',side_effect=AssertionError('No live network in tests'))
        self.network.start();self.addCleanup(self.network.stop)

    def test_structured_status_and_concurrent_single_fetch(self):
        def delayed_clock(_):
            self.clock += timedelta(seconds=1)
            return payload()
        self.fetch.side_effect=delayed_clock
        with ThreadPoolExecutor(max_workers=6) as pool:
            packets = list(pool.map(lambda _:self.store.load(snapshot()),range(6)))
        self.assertEqual(self.fetch.call_count,1)
        self.assertTrue(all(p[0]['reports'][0]['status']=='Out' for p in packets))
        self.assertEqual(packets[0][0]['reports'][0]['player_id'],'1')
        self.assertEqual(len(list((self.root/'availability/archive/nba').rglob('*.json'))),1)

    def test_public_feed_player_card_ids_and_active_status(self):
        source=payload('Active')
        athlete=source['injuries'][0]['injuries'][0]['athlete']
        athlete.pop('id');athlete['links']=[dict(href='https://www.espn.com/nba/player/_/id/12345/player-alpha')]
        packet=parse(source,'NBA',NOW.isoformat())
        self.assertEqual(packet['reports'][0]['player_id'],'12345')
        self.fetch.return_value=source
        result=self.engine.apply(snapshot(),PPPForecast('BOS','NYK',110,100,12,15,{}),'PPP')
        self.assertFalse(summary(result)['uncertain'])

    def test_historical_replay_only_uses_pre_cutoff_archives(self):
        self.store.load(snapshot())
        self.clock += timedelta(days=2)
        game = {**snapshot(),'pulled_at':NOW.isoformat()}
        packet,error = self.store.load(game)
        self.assertIsNotNone(packet);self.assertIsNone(error)
        self.assertEqual(self.fetch.call_count,1)
        packet,error = self.store.load({**game,'pulled_at':(NOW-timedelta(hours=1)).isoformat()})
        self.assertIsNone(packet);self.assertIn('archive',error)
        self.assertEqual(self.fetch.call_count,1)

    def test_bad_or_stale_feed_never_makes_an_adjustment(self):
        with self.assertRaises(Exception):parse(dict(articles=[]),'NBA',NOW.isoformat())
        self.store.load(snapshot());self.clock += timedelta(minutes=6)
        self.fetch.side_effect = ValueError('feed schema changed')
        packet,error = self.store.load(snapshot())
        self.assertIsNone(packet);self.assertIn('schema',error)
        self.store.load(snapshot());self.assertEqual(self.fetch.call_count,2)

    def test_team_effects_change_each_game_method_and_preserve_original(self):
        for method in ('PPP','EPA','ELO'):
            base = PPPForecast('BOS','NYK',110,100,12,15,dict(as_of=snapshot()['pulled_at']))
            original = deepcopy(base)
            result = self.engine.apply(snapshot(),base,method)
            self.assertLess(result.home_mean,base.home_mean)
            self.assertGreater(result.away_mean,base.away_mean)
            self.assertGreaterEqual(result.margin_sd,base.margin_sd)
            self.assertGreaterEqual(result.total_sd,base.total_sd)
            self.assertNotEqual(result.spread('BOS',0).probability,base.spread('BOS',0).probability)
            self.assertEqual(base,original)
            evidence = summary(result)['applied'][0]
            self.assertGreaterEqual(evidence['joint_absence_games'],3)
            self.assertIn('win_rate',evidence['players'][0]['playing'])
            self.assertIsNotNone(panel(result))

    def test_player_workload_updates_every_method_and_exact_mass(self):
        key = ('Player Beta','player_points')
        for method in ('WHP','UO','OAM','PGE'):
            base = WHPForecast(15,16,'normal',True,[],[],{}) if method=='WHP' else UsageOpportunityForecast([10,15,20],[.2,.6,.2],1000,{})
            forecast = PlayerMarketForecast({key:base}) if method=='WHP' else UsageMarketForecast({key:base})
            result = self.engine.apply(snapshot(),forecast,method)
            fitted = result.forecasts[key]
            self.assertGreater(fitted.mean,base.mean)
            self.assertAlmostEqual(sum(fitted.probabilities),1,places=12)
            self.assertTrue(all(int(x)==x for x in fitted.values))
            for line in (0,15,15.5,30):
                over,under = fitted.market(line,'Over'),fitted.market(line,'Under')
                self.assertAlmostEqual(over.win+over.loss+over.push,1,places=12)
                self.assertAlmostEqual(over.win,under.loss,places=12)
            self.assertEqual(forecast.forecasts[key].mean,15)

    def test_incomplete_box_scores_do_not_imply_absence(self):
        # Removing a small bench participant breaks minutes reconciliation.
        broken = self.players[~self.players.entity_id.eq('BOS0')]
        _,games,ledger = inputs(broken,self.teams,'NBA',NOW)
        self.assertFalse(games.team.eq('BOS').any())
        self.assertFalse(ledger.team.eq('BOS').any())

    def test_unknown_inactive_prop_not_priced_as_under_even_without_history(self):
        self.repo.load.side_effect = DataSourceError('No saved player history.')
        key = ('Player Alpha','player_points')
        base = PlayerMarketForecast({key:WHPForecast(20,9,'normal',True,[],[],{})})
        result = self.engine.apply(snapshot(),base,'WHP')
        self.assertNotIn(key,result.forecasts)
        self.assertIn('void',result.errors[key])

    def test_questionable_is_not_assigned_arbitrary_probability(self):
        self.fetch.return_value = payload('Questionable')
        base = PPPForecast('BOS','NYK',110,100,12,15,{})
        result = self.engine.apply(snapshot(),base,'PPP')
        self.assertEqual(result.home_mean,base.home_mean)
        self.repo.load.assert_not_called()
        self.assertIn('Questionable',summary(result)['reason'])

    def test_filter_notice_lists_only_game_injuries_once_across_methods(self):
        source=payload('Questionable')
        source['injuries'][0]['injuries'].append(dict(status='Active',date='2026-11-12T10:00Z',
            athlete=dict(id='2',displayName='Healthy Player',team=dict(abbreviation='BOS'))))
        source['injuries'].append(dict(displayName='Los Angeles Lakers',injuries=[dict(status='Out',date='2026-11-12T10:00Z',
            athlete=dict(id='3',displayName='Other Game Player',team=dict(abbreviation='LAL')))]))
        self.fetch.return_value=source
        base=PPPForecast('BOS','NYK',110,100,12,15,{})
        ppp=self.engine.apply(snapshot(),base,'PPP')
        epa=self.engine.apply(snapshot(),base,'EPA')
        message=note(ppp,epa)
        self.assertEqual(message,'Game injuries: BOS — Player Alpha (Questionable).')
        self.assertNotIn('baseline',message)
        self.assertEqual(ppp.home_mean,base.home_mean)

    def test_filter_notice_does_not_expand_missing_history_explanations(self):
        self.repo.load.side_effect=DataSourceError('No saved player history.')
        result=self.engine.apply(snapshot(),PPPForecast('BOS','NYK',110,100,12,15,{}),'PPP')
        self.assertEqual(note(result),'Game injuries: BOS — Player Alpha (Out).')
        self.assertIn('baseline retained',summary(result)['reason'])

    def test_future_outcomes_and_revisions_do_not_leak(self):
        p,g,l = inputs(self.players,self.teams,'NBA',NOW)
        frame,columns,_ = absence_columns(g,l,[parse(payload(),'NBA',NOW.isoformat())['reports'][0]],'BOS',NOW)
        effect = estimate(frame,columns,['points','points_allowed'],NOW,'NBA')
        future = frame.iloc[:1].copy()
        future['event_date']=NOW+timedelta(days=1);future['available_at']=NOW+timedelta(days=2)
        future['points']=10000
        again = estimate(pd.concat([frame,future]),columns,['points','points_allowed'],NOW,'NBA')
        np.testing.assert_array_equal(effect.delta,again.delta)
        revised = frame.copy();revised.loc[revised[columns[0]].eq(1),'points']-=10
        changed = estimate(revised,columns,['points','points_allowed'],NOW,'NBA')
        self.assertLess(changed.delta[0],effect.delta[0])

    def test_established_absence_is_not_deducted_twice(self):
        _,games,ledger = inputs(self.players,self.teams,'NBA',NOW)
        frame,columns,_ = absence_columns(games,ledger,[parse(payload(),'NBA',NOW.isoformat())['reports'][0]],'BOS',NOW)
        frame.loc[frame.tail(5).index,columns]=1
        effect = estimate(frame,columns,['points','points_allowed'],NOW,'NBA')
        np.testing.assert_array_equal(effect.delta,[0,0])

    def test_integer_shift_keeps_fractional_mean_and_negative_yards(self):
        base = UsageOpportunityForecast([-4,0,10],[.1,.2,.7],100,{})
        shifted = adjusted_distribution(base,2.25,0,nonnegative=False)
        self.assertAlmostEqual(shifted.mean,base.mean+2.25)
        self.assertLess(min(shifted.values),0)
        bounded = adjusted_distribution(base,-20,0,nonnegative=True)
        self.assertEqual(bounded.values,[0]);self.assertAlmostEqual(bounded.probabilities[0],1)

    def test_nfl_scoring_rows_are_not_a_participation_ledger(self):
        players,teams = history()
        teams=teams.rename(columns={'pts':'pf'});teams['pa']=100
        _,games,ledger = inputs(players,teams,'NFL',NOW)
        self.assertTrue(ledger.empty)

    def test_nfl_snap_ledger_drives_game_and_teammate_usage(self):
        rename = {'BOS':'BAL','NYK':'CLE','ATL':'PIT','LAL':'CIN'}
        players,teams = history()
        for frame in (players,teams):
            for c in ('team','opponent'):frame[c]=frame[c].replace(rename)
        teams=teams.rename(columns={'pts':'pf'})
        players['attempts']=players['min'];players['carries']=players['min']/2;players['targets']=players['min']/3
        snaps=[]
        for (game,team),group in players.groupby(['game_id','team']):
            a=bool(group.entity_name.eq('Player Alpha').any())
            if a:snaps.append(dict(game_id=game,team=team,player='Player Alpha',pfr_player_id='A',offense_snaps=40,defense_snaps=0))
            snaps.append(dict(game_id=game,team=team,player='Player Beta' if team=='BAL' else team+' Lead',pfr_player_id=team+'B',offense_snaps=60,defense_snaps=0))
            for j in range(10):snaps.append(dict(game_id=game,team=team,player=team+' Off '+str(j),pfr_player_id=team+'o'+str(j),offense_snaps=(600-40*a)/10,defense_snaps=0))
            for j in range(11):snaps.append(dict(game_id=game,team=team,player=team+' Def '+str(j),pfr_player_id=team+'d'+str(j),offense_snaps=0,defense_snaps=60))
        self.engine.nfl_directory.mkdir(parents=True)
        pd.DataFrame(snaps).to_csv(self.engine.nfl_directory/'snap_counts_2026.csv',index=False)
        self.repo.load.return_value=(players,teams)
        self.fetch.return_value=payload()
        self.fetch.return_value['injuries'][0]['displayName']='Baltimore Ravens'
        self.fetch.return_value['injuries'][0]['injuries'][0]['athlete']['team']['abbreviation']='BAL'
        game={**snapshot(),'sport':'NFL','home_team':'Baltimore Ravens','away_team':'Cleveland Browns'}
        original=PPPForecast('BAL','CLE',26,22,12,15,{})
        result=self.engine.apply(game,original,'PPP')
        self.assertLess(result.home_mean,original.home_mean)
        for market in ('player_pass_yds','player_rush_yds','player_reception_yds'):
            key=('Player Beta',market)
            base=UsageOpportunityForecast([100,150,200],[.2,.6,.2],1000,{})
            result=self.engine.apply(game,UsageMarketForecast({key:base}),'UO')
            self.assertGreater(result.forecasts[key].mean,base.mean)
            self.assertTrue(summary(result)['applied'])

    def test_new_base_forecast_cannot_reuse_an_old_adjusted_value(self):
        first=self.engine.apply(snapshot(),PPPForecast('BOS','NYK',110,100,12,15,{}),'PPP')
        second=self.engine.apply(snapshot(),PPPForecast('BOS','NYK',120,100,12,15,{}),'PPP')
        self.assertAlmostEqual(second.home_mean-first.home_mean,10)

    def test_unseen_joint_absences_are_not_added_independently(self):
        _,games,ledger=inputs(self.players,self.teams,'NBA',NOW)
        frame,columns,_=absence_columns(games,ledger,[parse(payload(),'NBA',NOW.isoformat())['reports'][0]],'BOS',NOW)
        frame['absent:other']=1-frame[columns[0]]
        self.assertIsNone(estimate(frame,[*columns,'absent:other'],['points','points_allowed'],NOW,'NBA'))


if __name__=='__main__':main()
