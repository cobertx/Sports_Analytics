"""NFL prompt cohorts, zero outcomes, shared model inputs and saved summaries."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from unittest import TestCase,main
from unittest.mock import patch

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sports_analytics.backtesting_service import BacktestingService,NFL_PLAYER_COHORT,player_backtest_rows,player_position_rows
from sports_analytics.backtesting_view import available_models,model_config
from sports_analytics.data import DataSourceError
from sports_analytics.performance_summary import PerformanceSummaries,settings
from models.Player_Stats.Weighted_Historical_Performance.markets import FOOTBALL


def player_history():
    rows=[]
    for game in range(20):
        date=pd.Timestamp('2025-07-01',tz='UTC')+pd.Timedelta(weeks=game)
        for position in ['QB','RB','FB','HB','WR','TE','CB','K',None,'ATH']:
            name=position or 'Missing'
            rows.append(dict(game_id=str(game),event_date=date,available_at=date+pd.Timedelta(days=1),
                entity_id=name,entity_name=name,team='CHI',opponent='NE',position=position,season=2025,
                season_type='regular',passing_yards=200+game,attempts=30,completions=20,passing_tds=1,
                interceptions=0,rushing_yards=(0 if game==19 else -3 if game==18 else 20+game),
                carries=0 if game==19 else {'QB':4,'RB':12,'FB':1,'HB':1}.get(position,0),
                rushing_tds=0,receiving_yards=30+game,receptions=3,
                targets=1 if position in {'FB','HB'} else 5,receiving_tds=0,touchdowns=0))
    # A newer game containing no relevant rushers must not consume the game limit.
    latest=dict(rows[-1],game_id='WR-only',entity_id='WR',entity_name='WR',position='WR',
                event_date=pd.Timestamp('2025-12-25',tz='UTC'),available_at=pd.Timestamp('2025-12-26',tz='UTC'))
    return pd.DataFrame([*rows,latest]).sort_values(['event_date','game_id'])


def depth_history():
    frame=player_history()
    additions=[]
    for identity,role,attempts,carries,targets in [
        ('QB backup','QB',20,1,0),('QB third','QB',1,0,0),
        ('RB second','RB',0,8,2),('RB third','RB',0,6,1),
        ('WR second','WR',0,0,6),('WR third','WR',0,0,4),('WR fourth','WR',0,0,3),
        ('TE backup','TE',0,0,3),
    ]:
        rows=frame[frame.entity_id.eq(role)].copy()
        rows=rows.assign(entity_id=identity,entity_name=identity,position=role,
                         attempts=attempts,carries=carries,targets=targets)
        additions.append(rows)
    return pd.concat([frame,*additions],ignore_index=True).sort_values(['event_date','game_id'])


class NFLBacktestRoleChecks(TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.backend=BacktestingService(root=temporary.name,now=lambda:pd.Timestamp('2026-01-01',tz='UTC'))
        self.frame=player_history()
        self.backend.player_inputs=lambda sport:self.frame.copy()
        self.backend.phase_map=lambda sport:{}
        for target,replacement in [('requests.sessions.Session.request',AssertionError('No HTTP')),
                                   ('sports_analytics.backtesting_service.source_revisions',None)]:
            mock=patch(target,side_effect=replacement) if replacement else patch(target,return_value=[])
            mock.start();self.addCleanup(mock.stop)

    def test_every_football_prompt_uses_its_position_family(self):
        expected={
            'pass':{'QB'},'rush':{'QB','RB','FB','HB'},
            'reception':{'RB','FB','HB','WR','TE'},'receptions':{'RB','FB','HB','WR','TE'},
            'tds':{'QB','RB','FB','HB','WR','TE'},'anytime':{'QB','RB','FB','HB','WR','TE'},
        }
        for market in FOOTBALL:
            family=market.split('_')[1]
            with self.subTest(market=market):
                actual=player_position_rows(self.frame,'NFL',market)
                self.assertEqual(set(actual.entity_id),expected[family])
                pd.testing.assert_frame_equal(actual,player_position_rows(self.frame,'NFL',market+'_alternate'))
        self.assertEqual(len(player_backtest_rows(self.frame,'NBA','player_points')),len(self.frame))
        self.assertEqual(len(player_backtest_rows(self.frame,'NCAAF','player_rush_yds')),len(self.frame))

    def test_aliases_and_position_changes_are_checked_per_appearance(self):
        positions=[' quarterback ','Running Back','fullback','Halfback','TB','WR','Tight End','QB/WR',pd.NA,'ATH']
        frame=self.frame.iloc[:len(positions)].copy().assign(position=positions,entity_id='same-player')
        actual=player_position_rows(frame,'NFL','player_rush_yds')
        self.assertEqual(actual.position.tolist(),positions[:5]+['QB/WR'])
        self.assertTrue(player_position_rows(frame.drop(columns='position'),'NFL','player_rush_yds').empty)
        self.assertTrue(player_position_rows(frame,'NFL','unsupported').empty)
        pd.testing.assert_series_equal(frame.position,pd.Series(positions,index=frame.index,name='position'))

    def test_counts_and_statistic_choices_match_evaluation_roles(self):
        self.assertEqual(self.backend.available_games('NFL','player-stats','whp','__all__','player_rush_yds'),19)
        self.assertEqual(self.backend.available_games('NFL','player-stats','whp','WR','player_rush_yds'),0)
        self.assertEqual(self.backend.available_games('NFL','player-stats','whp','QB','player_rush_yds'),19)
        self.assertEqual(self.backend.available_games('NFL','player-stats','whp','__all__','player_reception_yds'),20)
        self.assertEqual(self.backend.available_games('NFL','player-stats','whp','__all__','player_rush_yds',team_filter='NE'),0)
        for key in ['whp','uo','oam','pge']:
            _,options,_=self.backend.options('NFL','player-stats',key,'WR')
            markets={option['value'] for option in options}
            self.assertIn('player_reception_yds',markets)
            self.assertNotIn('player_rush_yds',markets)
            self.assertNotIn('player_pass_yds',markets)
        self.assertEqual(self.backend.options('NFL','player-stats','whp','Missing')[1],[])

    def test_all_four_methods_filter_before_limiting_and_keep_full_training_inputs(self):
        for key in ['whp','uo','oam','pge']:
            captured=[]
            def evaluate(result,sport,method,frame,selected,games,spec,market,line,progress):
                captured.extend(games)
                self.assertEqual(set(frame.entity_id),set(self.frame.entity_id))
                self.assertEqual(set(selected.entity_id),set(self.frame.entity_id))
                for game in games:
                    result['records'].append(dict(game_id=str(game.game_id),date=game.event_date.isoformat(),
                        label=game.entity_name,actual=float(game.value),predicted=0.,lower=-10.,upper=100.,interval_level=.8))
            with self.subTest(key=key),patch.object(self.backend,'_players',side_effect=evaluate):
                result=self.backend.run('NFL','player-stats',key,'__all__','player_rush_yds',limit=2)
                self.assertEqual({game.entity_id for game in captured},{'QB','RB'})
                self.assertEqual({str(game.game_id) for game in captured},{'18','19'})
                self.assertEqual(result['attempted'],4)
                self.assertEqual([r['actual'] for r in result['records']].count(0),2)
                self.assertEqual([r['actual'] for r in result['records']].count(-3),2)
                self.assertEqual(result['context']['player_cohort'],NFL_PLAYER_COHORT)
        with self.assertRaisesRegex(DataSourceError,'relevant recorded position'):
            self.backend.run('NFL','player-stats','whp','WR','player_rush_yds',limit=2)

    def test_real_whp_replay_keeps_zero_yards_and_reopens_exact_results(self):
        result=self.backend.run('NFL','player-stats','whp','__all__','player_rush_yds',limit=2)
        self.assertEqual(result['attempted'],4)
        self.assertEqual(len(result['records']),4)
        self.assertEqual(result['skipped'],[])
        self.assertEqual({row['label'].split(' · ')[0] for row in result['records']},{'QB','RB'})
        self.assertEqual(sum(row['actual']==0 for row in result['records']),2)
        self.assertEqual(self.backend.run('NFL','player-stats','whp','__all__','player_rush_yds',limit=2),result)
        self.assertEqual(self.backend.saved('NFL','player-stats','whp',result['run_id']),result)

    def test_regular_contributor_limits_exclude_backups_for_every_prompt_family(self):
        frame=depth_history()
        target=frame[frame.game_id.eq('19')].copy()
        target.index=[0]*len(target)  # Row selection must also handle repeated indexes.
        expected={
            'player_pass_yds':{'QB'},
            'player_rush_yds':{'QB','RB','RB second'},
            'player_reception_yds':{'RB','RB second','WR','WR second','WR third','TE'},
            'player_anytime_td':{'QB','RB','RB second','WR','WR second','WR third','TE'},
        }
        original=frame.copy(deep=True)
        for market,identities in expected.items():
            with self.subTest(market=market):
                actual=player_backtest_rows(target,'NFL',market,history=frame)
                self.assertEqual(set(actual.entity_id),identities)
                reordered=player_backtest_rows(target,'NFL',market,history=frame.sample(frac=1,random_state=7))
                pd.testing.assert_frame_equal(actual,reordered)
        pd.testing.assert_frame_equal(frame,original)

    def test_current_future_and_not_yet_available_usage_cannot_change_primary_players(self):
        frame=depth_history()
        target=frame[frame.game_id.eq('19')]
        cutoff=target.event_date.iloc[0]
        expected=player_backtest_rows(target,'NFL','player_pass_yds',history=frame)
        revised=frame.copy()
        revised.loc[revised.event_date.ge(cutoff),['attempts','carries','targets']]=10000
        delayed=frame[frame.entity_id.eq('QB backup')].iloc[[0]].copy().assign(
            game_id='delayed',event_date=cutoff-pd.Timedelta(days=2),available_at=cutoff,attempts=10000)
        revised=pd.concat([revised,delayed],ignore_index=True)
        pd.testing.assert_frame_equal(player_backtest_rows(target,'NFL','player_pass_yds',history=revised),expected)
        revised.loc[revised.game_id.eq('delayed'),'available_at']=cutoff-pd.Timedelta(microseconds=1)
        self.assertEqual(set(player_backtest_rows(target,'NFL','player_pass_yds',history=revised).entity_id),{'QB backup'})

    def test_backup_promotion_requires_prior_workload_and_absences_lower_team_game_average(self):
        frame=depth_history()
        target=frame[frame.game_id.eq('19')]
        frame.loc[frame.entity_id.eq('QB'),'attempts']=20
        frame=frame[~frame.entity_id.eq('QB backup')|frame.game_id.isin(['18','19'])].copy()
        frame.loc[frame.entity_id.eq('QB backup'),'attempts']=40
        # One 40-attempt appearance is only 13.3 attempts per recent team game.
        self.assertEqual(set(player_backtest_rows(target,'NFL','player_pass_yds',history=frame).entity_id),{'QB'})
        promoted=depth_history()
        promoted.loc[promoted.entity_id.eq('QB')&promoted.game_id.isin(['17','18']),'attempts']=0
        promoted.loc[promoted.entity_id.eq('QB backup')&promoted.game_id.isin(['17','18']),'attempts']=35
        self.assertEqual(set(player_backtest_rows(target,'NFL','player_pass_yds',history=promoted).entity_id),{'QB backup'})

    def test_single_player_requests_rank_against_team_and_use_history_outside_date_range(self):
        self.frame=depth_history()
        self.assertEqual(self.backend.available_games('NFL','player-stats','whp','QB backup','player_pass_yds'),0)
        cutoff=self.frame[self.frame.game_id.eq('19')].event_date.iloc[0].tz_convert('America/New_York').date().isoformat()
        self.assertEqual(self.backend.available_games('NFL','player-stats','whp','QB','player_pass_yds',start=cutoff),1)
        with self.assertRaisesRegex(DataSourceError,'regular pregame workload'):
            self.backend.run('NFL','player-stats','whp','QB backup','player_pass_yds',limit=2)
        result=self.backend.run('NFL','player-stats','whp','__all__','player_rush_yds',limit=1,start=cutoff)
        self.assertEqual(result['attempted'],3)
        self.assertEqual({row['label'].split(' · ')[0] for row in result['records']},{'QB','RB','RB second'})

    def test_missing_workload_excludes_players_and_no_prior_games_produces_no_cohort(self):
        target=self.frame[self.frame.game_id.eq('19')]
        self.assertTrue(player_backtest_rows(target,'NFL','player_rush_yds',history=self.frame.drop(columns='carries')).empty)
        self.assertTrue(player_backtest_rows(target,'NFL','player_rush_yds',history=target).empty)
        prior=self.frame.copy()
        prior.loc[prior.entity_id.eq('RB')&prior.game_id.eq('18'),'carries']=float('nan')
        self.assertEqual(set(player_backtest_rows(target,'NFL','player_rush_yds',history=prior).entity_id),{'QB'})

    def test_contributor_limits_apply_independently_to_each_team(self):
        frame=depth_history()
        other=frame.assign(team='NE',opponent='CHI',entity_id='NE-'+frame.entity_id)
        history=pd.concat([frame,other],ignore_index=True)
        target=history[history.game_id.eq('19')]
        actual=player_backtest_rows(target,'NFL','player_rush_yds',history=history)
        expected={'QB','RB','RB second'}
        self.assertEqual(set(actual.entity_id),expected|{'NE-'+identity for identity in expected})
        chi=player_backtest_rows(target[target.team.eq('CHI')],'NFL','player_rush_yds',history=history)
        self.assertEqual(set(chi.entity_id),expected)

    def test_performance_summaries_rerun_legacy_cohorts_but_preserve_archives(self):
        result=self.backend.run('NFL','player-stats','whp','__all__','player_rush_yds',limit=2)
        context=dict(sport='NFL',group='player-stats')
        request=settings(context,'player_rush_yds','__all__',2)
        manager=PerformanceSummaries(self.backend,watch=False)
        entries={}
        for number,model in enumerate(available_models('NFL','player-stats')):
            legacy=deepcopy(result)
            legacy['context'].pop('player_cohort')
            if model.key=='whp':legacy['context']['player_cohort']='nfl-market-positions-v1'
            legacy['context']['model']=model.key
            legacy['run_id']=str(number+1)*64
            legacy['config']=model_config(model,'NFL')
            legacy['created_at']='2027-01-01T00:00:00Z'
            path=self.backend.root/'nfl/player-stats'/model.key/(legacy['run_id']+'.json')
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(dict(result=legacy)),encoding='utf-8')
            entries[model.key]=dict(state='completed',run_id=legacy['run_id'],reused=True)
            self.assertEqual(self.backend.saved('NFL','player-stats',model.key,legacy['run_id']),legacy)
        self.assertEqual(manager.latest(context,'player_rush_yds','__all__')['entries']['whp']['run_id'],result['run_id'])
        previous=manager.new(context,entries,request,'performance')
        # Remove the new WHP report to verify all four old methods are rerun.
        (self.backend.root/'nfl/player-stats/whp'/(result['run_id']+'.json')).unlink()
        self.backend.root.joinpath(result['saved_directory'],'report.json').unlink()
        with patch.object(self.backend,'start',side_effect=lambda **p:p['key']+'-job') as start:
            fresh=manager.ensure(context,request,'__all__')
        self.assertNotEqual(fresh['id'],previous['id'])
        self.assertEqual(start.call_count,4)
        self.assertTrue(all(entry['state']=='running' for entry in fresh['entries'].values()))


if __name__=='__main__':main()
