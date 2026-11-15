"""Automatic results ingestion, failure visibility and prediction invalidation."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sports_analytics.data import DataSourceError, Query
from sports_analytics.history_freshness import active_season, source_fresh, update_lease
from sports_analytics.history_updates import HistoryUpdater
from sports_analytics.service import DataService
from sports_analytics.ppp_service import PPPMarketService
from sports_analytics.ncaaf_model_data import NcaafModelData
from sports_analytics.page_cache import forecast_revision
from models.Game_Outcome.PPP.PPP import PPPModel, PPPForecast

NOW=datetime(2026,11,13,17,tzinfo=timezone.utc)


class AutomaticHistoryChecks(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.data=DataService(self.root/'results.sqlite3')

    def test_shared_five_minute_policy_and_season_rollovers(self):
        for sport in ('NBA','NFL','NCAAF'):
            path=self.root/(sport+'.csv');path.write_text('results')
            self.assertTrue(source_fresh(path,sport,2026,fetched_at=NOW.isoformat(),now=NOW))
            old=(NOW-timedelta(minutes=6)).isoformat()
            self.assertFalse(source_fresh(path,sport,2026,fetched_at=old,now=NOW))
            self.assertTrue(source_fresh(path,sport,2025,fetched_at=old,now=NOW))
        self.assertEqual(active_season('NBA',datetime(2027,1,1)),2026)
        self.assertEqual(active_season('NFL',datetime(2027,2,1)),2026)
        self.assertEqual(active_season('NBA',datetime(2027,10,1)),2027)

    def test_plan_always_refreshes_both_team_and_player_histories(self):
        updater=HistoryUpdater(self.data,self.root,now=lambda:NOW)
        self.addCleanup(updater.stop)
        self.assertEqual(set(updater.plan()),{
            'NBA 2026 regular player','NBA 2026 regular team',
            'NFL 2026 regular player','NFL 2026 regular team'})

    def test_unattended_failures_preserve_success_metadata_and_retry(self):
        updater=HistoryUpdater(self.data,self.root,now=lambda:NOW)
        self.addCleanup(updater.stop)
        work=Mock(return_value={'rows':10})
        with patch.object(updater,'plan',return_value={'NBA results':work}):
            first=updater.tick()['sources']['NBA results']
            self.assertEqual(first['detail']['rows'],10)
            work.side_effect=DataSourceError('Provider unavailable')
            second=updater.tick()['sources']['NBA results']
            self.assertEqual(second['last_success'],first['last_success'])
            self.assertEqual(second['error'],'Provider unavailable')
            self.assertFalse(second['pending'])
            work.side_effect=None;work.return_value={'rows':12}
            updater.queue.retry_seconds=0
            # Explicit retry bypasses the failed queue task's backoff.
            updater.queue.jobs.clear()
            third=updater.tick()['sources']['NBA results']
            self.assertIsNone(third['error']);self.assertEqual(third['detail']['rows'],12)

    def test_cross_process_lease_prevents_duplicate_dashboard_and_scheduled_cycles(self):
        path=self.root/'cycle.lock'
        with update_lease(path) as first:
            self.assertTrue(first)
            with update_lease(path) as second:self.assertFalse(second)
        with update_lease(path) as third:self.assertTrue(third)

    def test_current_college_result_feeds_expire_and_archive_stays_offline(self):
        college=NcaafModelData(self.root/'college',client=Mock())
        path=college.directory/'raw/games.json';path.parent.mkdir(parents=True)
        path.write_text('[{"id":1}]')
        old=NOW.timestamp()-600;os.utime(path,(old,old))
        with patch('sports_analytics.history_freshness.datetime') as clock, patch.object(college,'_request',return_value=[{'id':1},{'id':2}]) as request:
            clock.now.return_value=NOW
            self.assertEqual(len(college._raw('games.json','/games',{'year':2026},False)),2)
            request.assert_called_once()
            request.reset_mock()
            college._raw('games.json','/games',{'year':2025},False)
            request.assert_not_called()

    def test_college_refresh_waits_for_games_and_retries_missing_final_statistics(self):
        from sports_analytics.ncaaf_model_data import school_token
        college=NcaafModelData(self.root/'college')
        school='Test School';token=school_token(school)
        paths=[college.team_path(2026,school),college.player_path(2026,school),
               college.directory/f'player_team_games_2026_{token}.csv']
        for path in paths:
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text('game_id\n1\n')
        schedule=college.directory/'raw'/f'games_2026_{token}.json'
        schedule.parent.mkdir(parents=True)
        schedule.write_text(json.dumps([dict(id=1,completed=True,startDate='2026-11-07T18:00Z'),
                                       dict(id=2,completed=False,startDate='2026-11-14T18:00Z')]))
        os.utime(schedule,(NOW.timestamp(),NOW.timestamp()))
        self.assertFalse(HistoryUpdater.college_due(college,school,2026,NOW))
        self.assertTrue(HistoryUpdater.college_due(college,school,2026,NOW+timedelta(days=1,hours=5)))

    def test_new_final_flows_through_background_cycle_into_shared_history(self):
        updater=HistoryUpdater(self.data,self.root,now=lambda:NOW);self.addCleanup(updater.stop)
        query=Query('NBA',2026,'player')
        frame=pd.DataFrame(dict(game_id=['one'],entity_id=['p'],event_date=pd.to_datetime(['2026-11-12T01:00Z'],utc=True),
            position=['G'],matchup=['A @ B'],value=[10]))
        def update():
            result=self.data.load(query,refresh=True)
            return {'rows':len(result.frame)}
        with patch.object(updater,'plan',return_value={'NBA results':update}),patch('sports_analytics.service.fetch',return_value=frame):
            updater.tick()
        new=pd.concat([frame,frame.assign(game_id='two',value=1)],ignore_index=True)
        with patch.object(updater,'plan',return_value={'NBA results':update}),patch('sports_analytics.service.fetch',return_value=new):
            updater.tick()
        self.assertEqual(self.data.cached(self.data.key(query)).frame.game_id.tolist(),['one','two'])
        self.assertEqual(updater.status()['sources']['NBA results']['detail']['rows'],2)

    def test_fixed_quote_cutoff_uses_revised_inputs_in_both_memory_and_disk_cache(self):
        frame=pd.DataFrame({'game_id':['one'],'value':[10.]})
        source=SimpleNamespace(directory=self.root,prepare=Mock(return_value=(frame,[])))
        model=PPPModel();model.predict=Mock(side_effect=[
            PPPForecast('BAL','CLE',25,21,10,14,{}),PPPForecast('BAL','CLE',18,24,10,14,{})])
        service=PPPMarketService(source,model)
        snapshot=dict(home_team='Baltimore Ravens',away_team='Cleveland Browns',
            pulled_at='2026-11-13T17:00:00Z',commence_time='2026-11-14T00:00:00Z')
        first=service.forecast(snapshot)
        self.assertIs(service.forecast(snapshot),first)
        source.prepare.return_value=(frame.assign(value=2.),[])
        second=service.forecast(snapshot)
        self.assertNotEqual(first.home_mean,second.home_mean)
        self.assertEqual(model.predict.call_count,2)
        restored=PPPMarketService(source,model).forecast(snapshot)
        self.assertEqual(restored.home_mean,second.home_mean)
        self.assertEqual(model.predict.call_count,2)

    def test_database_revision_is_seen_through_nba_service_attribute(self):
        path=self.root/'history.sqlite3';path.write_text('one')
        source=SimpleNamespace(data=SimpleNamespace(service=SimpleNamespace(path=path)))
        before=forecast_revision(source,'NBA');path.write_text('updated history')
        self.assertNotEqual(before,forecast_revision(source,'NBA'))

    def test_public_result_cache_updates_final_availability_and_rejects_truncation(self):
        query=Query('NBA',2026,'player')
        frame=pd.DataFrame(dict(game_id=['one','two'],entity_id=['p','p'],
            event_date=pd.to_datetime(['2026-11-12T01:00Z','2026-11-13T16:00Z'],utc=True),
            position=['G','G'],matchup=['A @ B']*2,value=[10,20]))
        with patch('sports_analytics.service.fetch',return_value=frame):
            initial=self.data.load(query,refresh=True)
        self.assertTrue((initial.frame.available_at<=pd.Timestamp(initial.fetched_at)).all())
        with patch('sports_analytics.service.fetch',return_value=frame.iloc[:1].copy()):
            failed=self.data.load(query,refresh=True)
        self.assertEqual(failed.status,'stale cache')
        self.assertEqual(len(self.data.cached(self.data.key(query)).frame),2)

    def test_repeated_checks_retain_the_first_time_a_result_was_observed(self):
        from sports_analytics.history_freshness import retain_first_availability
        first=pd.DataFrame(dict(game_id=['g'],entity_id=['p'],available_at=pd.to_datetime(['2026-11-13T17:00Z'],utc=True)))
        newer=first.assign(available_at=pd.Timestamp('2026-11-13T17:05Z'))
        actual=retain_first_availability(newer,first,['game_id','entity_id'])
        self.assertEqual(actual.available_at.iloc[0],first.available_at.iloc[0])

    def test_current_nba_epa_cannot_silently_fall_back_to_only_old_archives(self):
        from models.Game_Outcome.EPA.nba_epa_training import NBAEPAData
        from models.Game_Outcome.EPA.nba_epa_data import archive_source
        from sports_analytics.history_freshness import active_season
        year=active_season('NBA')
        data=NBAEPAData(self.root/'epa',self.data)
        for kind in ('possessions','play_by_play'):
            path=data.directory/'raw'/archive_source(kind,year-1)[0]
            path.parent.mkdir(parents=True,exist_ok=True);path.touch()
        with patch.object(data,'_season',return_value=SimpleNamespace(quality={'reconciled_games':10,'quarantined_games':0})):
            with self.assertRaisesRegex(DataSourceError,'current estimates require current-season'):
                data.prepare([year-1,year])

    def test_missing_published_college_final_does_not_replace_valid_history(self):
        from sports_analytics.history_freshness import active_season
        year=active_season('NCAAF')
        college=NcaafModelData(self.root/'college',client=Mock())
        destination=college.team_path(year,'Test School')
        destination.parent.mkdir(parents=True);destination.write_text('game_id\nold\n')
        old=datetime.now(timezone.utc).timestamp()-600;os.utime(destination,(old,old))
        def raw(name,endpoint,params,refresh):
            return [dict(id='new',completed=True)] if endpoint=='/games' else [dict(id='old')]
        with patch.object(college,'resolve_school',return_value='Test School'),patch.object(college,'_raw',side_effect=raw),\
             patch('sports_analytics.ncaaf_model_data.seasons_for_start',return_value=[year]),\
             patch('sports_analytics.ncaaf_model_data.aggregate_games',return_value=pd.DataFrame({'game_id':['old']})):
            with self.assertRaisesRegex(DataSourceError,'team statistics for completed games'):
                college.prepare_team('Test School',datetime.now(timezone.utc).isoformat())
        self.assertEqual(destination.read_text(),'game_id\nold\n')


if __name__=='__main__':main()
