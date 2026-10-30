"""Windows lock recovery, immutable result reuse and forecast reload regressions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from sports_analytics.result_cache import atomic_json, atomic_text, save_json, load_json
from models.Player_Stats.Weighted_Historical_Performance.prediction_service import WHPMarketService
from models.Player_Stats.Weighted_Historical_Performance.core import WeightedHistoricalPerformance
from scripts.check_research import player_inputs


def denied():
    error=PermissionError(13,'Access is denied')
    error.winerror=5
    return error


class ResultCacheChecks(TestCase):
    def setUp(self):
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.path=self.root/'forecasts/nba/result.json'
        self.data={'identity':{'version':1},'forecast':{'mean':20.,'variance':9.}}
        sleep=patch('sports_analytics.result_cache.sleep');self.sleep=sleep.start();self.addCleanup(sleep.stop)
        network=patch('requests.sessions.Session.request',side_effect=AssertionError('No HTTP in cache regressions'))
        network.start();self.addCleanup(network.stop)

    def block_replacements(self):
        original=Path.replace
        def replace(source,destination):
            if Path(destination).suffix=='.json':raise denied()
            return original(source,destination)
        return patch.object(Path,'replace',replace)

    def repository(self):
        dates=pd.date_range('2025-10-01',periods=35,freq='3D',tz='UTC')
        frame=pd.DataFrame(dict(game_id=list(map(str,range(35))),event_date=dates,
            available_at=dates+pd.Timedelta(days=1),entity_id='one',entity_name='Player One',
            team='BOS',pts=[15+i%9 for i in range(35)],reb=[i%7 for i in range(35)],min=30.))
        repo=Mock(root=self.root);repo.load.side_effect=lambda *args:frame.copy()
        rows=[dict(group='Points',market='Points',market_key=key,subject='Player One',selection='Over',line=15.5,odds=-110)
              for key in ('player_points_alternate','player_rebounds_alternate')]
        snapshot=dict(sport='NBA',game_id='123',home_team='Boston Celtics',away_team='Detroit Pistons',
            pulled_at='2026-03-01T12:00:00Z',commence_time='2026-03-02T12:00:00Z',rows=rows)
        return repo,snapshot

    def test_temporary_windows_denial_is_retried_and_json_remains_complete(self):
        original=Path.replace;calls=[]
        def transient(source,destination):
            calls.append(destination)
            if len(calls)<3:raise denied()
            return original(source,destination)
        with patch.object(Path,'replace',transient):atomic_json(self.path,self.data)
        self.assertEqual(len(calls),3);self.assertEqual(self.sleep.call_count,2)
        self.assertEqual(json.loads(self.path.read_text()),self.data)
        self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_identical_existing_result_is_reused_even_if_replacement_is_denied(self):
        atomic_json(self.path,self.data);stamp=self.path.stat().st_mtime_ns
        with patch.object(Path,'replace',side_effect=AssertionError('An identical result must not be replaced')):
            atomic_json(self.path,self.data)
        self.assertEqual(self.path.stat().st_mtime_ns,stamp)

    def test_permanent_json_lock_saves_durably_and_restores_after_restart(self):
        with self.block_replacements():
            self.assertTrue(save_json(self.path,self.data,self.root))
            self.assertFalse(self.path.exists())
            self.assertEqual(load_json(self.path,self.root,self.data['identity']),self.data)
        self.assertEqual(load_json(self.path,self.root,self.data['identity']),self.data)
        self.assertEqual(json.loads(self.path.read_text()),self.data)
        with closing(sqlite3.connect(self.root/'model_cache/result_recovery.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM results').fetchone()[0],0)

    def test_strict_source_writes_do_not_silently_become_forecast_cache_entries(self):
        with self.block_replacements(),self.assertRaises(PermissionError):atomic_json(self.path,self.data)
        self.assertFalse((self.root/'model_cache').exists())

    def test_read_lock_uses_the_durable_recovery_record(self):
        atomic_json(self.path,{'identity':{'version':0}})
        with self.block_replacements():save_json(self.path,self.data,self.root)
        original=Path.read_text
        def locked(path,*args,**kwargs):
            if path==self.path:raise denied()
            return original(path,*args,**kwargs)
        with self.block_replacements(),patch.object(Path,'read_text',locked):
            self.assertEqual(load_json(self.path,self.root,self.data['identity']),self.data)

    def test_mismatched_and_corrupt_results_are_never_used(self):
        atomic_json(self.path,self.data)
        self.assertIsNone(load_json(self.path,self.root,{'version':2}))
        for text in ('{truncated','{"identity":{"version":1},"forecast":{"mean":NaN}}','[]'):
            self.path.write_text(text)
            self.assertIsNone(load_json(self.path,self.root,self.data['identity']))
        with self.block_replacements():save_json(self.path,self.data,self.root)
        with closing(sqlite3.connect(self.root/'model_cache/result_recovery.sqlite3')) as db,db:
            db.execute('UPDATE results SET payload=?',(json.dumps({'identity':{'version':1},'forecast':{'mean':999}}),))
        self.path.unlink()
        self.assertIsNone(load_json(self.path,self.root,self.data['identity']))

    def test_schema_validation_can_recover_from_a_valid_json_with_invalid_forecast(self):
        self.path.parent.mkdir(parents=True)
        atomic_json(self.path,{'identity':{'version':1},'forecast':{}})
        with self.block_replacements():save_json(self.path,self.data,self.root)
        valid=lambda entry:'mean' in entry['forecast']
        self.assertEqual(load_json(self.path,self.root,self.data['identity'],valid),self.data)

    def test_invalid_serialization_preserves_the_previous_valid_file(self):
        atomic_json(self.path,self.data)
        with self.assertRaises(ValueError):save_json(self.path,{'value':float('nan')},self.root)
        self.assertEqual(json.loads(self.path.read_text()),self.data)

    def test_concurrent_identical_writes_do_not_race_or_rewrite(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _:atomic_json(self.path,self.data),range(20)))
        self.assertEqual(json.loads(self.path.read_text()),self.data)
        self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_separate_processes_publish_complete_identical_results(self):
        program="from pathlib import Path; import sys; from sports_analytics.result_cache import atomic_json; atomic_json(Path(sys.argv[1]), {'identity':{'version':1},'forecast':{'mean':20.,'variance':9.}})"
        def writer(_):
            return subprocess.run([sys.executable,'-c',program,str(self.path)],cwd=Path(__file__).resolve().parents[1],
                                  capture_output=True,text=True,timeout=20)
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(writer,range(3)))
        self.assertTrue(all(result.returncode==0 for result in results),[r.stderr for r in results])
        self.assertEqual(json.loads(self.path.read_text()),self.data)

    def test_separate_processes_share_the_recovery_store_without_losing_results(self):
        program="""from pathlib import Path
import sys
from unittest.mock import patch
from sports_analytics.result_cache import save_json
with patch.object(Path, 'replace', side_effect=PermissionError(13, 'Access is denied')):
    assert save_json(Path(sys.argv[1]), {'identity':{'version':int(sys.argv[3])}}, Path(sys.argv[2]))
"""
        def writer(i):
            return subprocess.run([sys.executable,'-c',program,str(self.path.with_name(f'{i}.json')),str(self.root),str(i)],
                                  cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=20)
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(writer,range(3)))
        self.assertTrue(all(result.returncode==0 for result in results),[r.stderr for r in results])
        for i in range(3):
            self.assertEqual(load_json(self.path.with_name(f'{i}.json'),self.root,{'version':i}),{'identity':{'version':i}})

    def test_temporary_cleanup_lock_does_not_mask_a_successful_write(self):
        with patch.object(Path,'unlink',side_effect=denied()):atomic_json(self.path,self.data)
        self.assertEqual(json.loads(self.path.read_text()),self.data)

    def test_whp_forecasts_and_audits_reload_without_refitting_while_json_is_locked(self):
        repo,snapshot=self.repository()
        with self.block_replacements():
            original=WHPMarketService(repo).forecast(snapshot)
            self.assertEqual(len(original.forecasts),2);self.assertFalse(original.errors)
            with patch.object(WeightedHistoricalPerformance,'forecast',side_effect=AssertionError('Must restore saved results')):
                restored=WHPMarketService(repo).forecast(snapshot)
            self.assertEqual(asdict(original),asdict(restored))
            for row in snapshot['rows']:
                self.assertEqual(original.selection(row),restored.selection(row))
        with patch.object(WeightedHistoricalPerformance,'forecast',side_effect=AssertionError('Must repair from recovery')):
            restored=WHPMarketService(repo).forecast(snapshot)
        self.assertEqual(asdict(original),asdict(restored))
        self.assertEqual(len(list((self.root/'forecasts/nba').glob('*.json'))),2)
        self.assertEqual(len(list((self.root/'evaluations').glob('*.json'))),1)

    def test_whp_incorrect_identity_and_invalid_moments_are_refitted(self):
        repo,snapshot=self.repository();first=WHPMarketService(repo).forecast(snapshot)
        path=next((self.root/'forecasts/nba').glob('*.json'))
        for corruption in ('identity','variance','diagnostics'):
            stored=json.loads(path.read_text())
            if corruption=='identity':stored['identity']['player']='Wrong Player'
            elif corruption=='variance':stored['forecast']['variance']=-1
            else:stored['forecast']['diagnostics']={}
            path.write_text(json.dumps(stored))
            fixed=WHPMarketService(repo).forecast(snapshot)
            self.assertEqual(asdict(first),asdict(fixed))

    def test_whp_simultaneous_requests_share_one_calculation(self):
        repo,snapshot=self.repository();service=WHPMarketService(repo)
        with patch.object(service,'_calculate',wraps=service._calculate) as calculate,ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _:service.forecast(snapshot),range(6)))
        self.assertEqual(calculate.call_count,1)
        self.assertTrue(all(asdict(r)==asdict(results[0]) for r in results))
        self.assertFalse(service._pending)

    def test_whp_audit_failure_does_not_remove_forecasts(self):
        repo,snapshot=self.repository();original=Path.replace
        def audit_lock(source,destination):
            if Path(destination).parent.name=='evaluations':raise denied()
            return original(source,destination)
        with patch.object(Path,'replace',audit_lock):
            result=WHPMarketService(repo).forecast(snapshot)
        self.assertEqual(len(result.forecasts),2);self.assertFalse(result.errors)

    def test_both_disk_stores_unavailable_keeps_the_valid_session_forecast(self):
        repo,snapshot=self.repository();service=WHPMarketService(repo)
        with self.block_replacements(),patch('sports_analytics.result_cache.sqlite3.connect',side_effect=sqlite3.OperationalError('read only')):
            with self.assertLogs('sports_analytics.result_cache',level='WARNING'):
                result=service.forecast(snapshot)
            self.assertEqual(len(result.forecasts),2);self.assertFalse(result.errors)
            self.assertIn('session',next(iter(result.forecasts.values())).diagnostics['cache_notice'])
            with patch.object(WeightedHistoricalPerformance,'forecast',side_effect=AssertionError('Use session cache')):
                self.assertEqual(asdict(service.forecast(snapshot)),asdict(result))

    def test_usage_matchup_and_pace_reload_the_same_saved_distribution(self):
        from dataclasses import replace
        from models.Player_Stats.Usage_Opportunity.prediction_service import UsageOpportunityMarketService
        from models.Player_Stats.Usage_Opportunity.config import UsageOpportunityConfig
        from models.Player_Stats.Opponent_Adjusted_Matchup.prediction_service import OpponentAdjustedMarketService
        from models.Player_Stats.Opponent_Adjusted_Matchup.config import MatchupConfig
        from models.Player_Stats.Pace_Game_Environment.prediction_service import PaceGameEnvironmentMarketService
        from models.Player_Stats.Pace_Game_Environment.config import PaceConfig
        players,teams=player_inputs()
        base=replace(UsageOpportunityConfig.for_sport('NBA'),simulations=1000)
        configs=[(UsageOpportunityMarketService,base,'models.Player_Stats.Usage_Opportunity.prediction_service.simulate_nba'),
                 (OpponentAdjustedMarketService,replace(MatchupConfig.for_sport('NBA'),baseline=base),'models.Player_Stats.Opponent_Adjusted_Matchup.prediction_service.simulate_nba'),
                 (PaceGameEnvironmentMarketService,replace(PaceConfig.for_sport('NBA'),baseline=base,minimum_league_games=8),'models.Player_Stats.Pace_Game_Environment.prediction_service.simulate_nba')]
        _,snapshot=self.repository()
        snapshot['rows']=[{**snapshot['rows'][0],'subject':'Player A'}]
        for cls,config,engine in configs:
            repo=Mock(root=self.root/cls.__name__)
            repo.load.side_effect=lambda *args:(players.copy(),teams.copy())
            with self.subTest(model=cls.__name__),self.block_replacements():
                first=cls(repo,config).forecast(snapshot)
                self.assertTrue(first.forecasts,first.errors)
                with patch(engine,side_effect=AssertionError('Must reuse persisted probability mass')):
                    restored=cls(repo,config).forecast(snapshot)
                self.assertEqual(asdict(first),asdict(restored))

    def test_game_forecast_can_be_reloaded_when_json_publication_fails(self):
        from sports_analytics.ppp_service import PPPMarketService
        from models.Game_Outcome.PPP.PPP import PPPModel,PPPForecast
        from dataclasses import replace
        model=PPPModel();model.config=replace(model.config,minimum_training_games=1,minimum_team_games=1,
            minimum_calibration_games=2,maximum_calibration_games=2)
        model.predict=Mock(return_value=PPPForecast('BAL','CLE',25,21,10,14,{}))
        data=SimpleNamespace(directory=self.root,prepare=Mock(return_value=(pd.DataFrame(),[])))
        snapshot=dict(home_team='Baltimore Ravens',away_team='Cleveland Browns',pulled_at='2026-10-01T12:00Z',commence_time='2026-10-02T00:00Z')
        with self.block_replacements():
            first=PPPMarketService(data,model).forecast(snapshot)
            model.predict.side_effect=AssertionError('Must load the recovery record')
            restored=PPPMarketService(data,model).forecast(snapshot)
        self.assertEqual(asdict(first),asdict(restored));model.predict.assert_called_once()


if __name__=='__main__':
    main()
