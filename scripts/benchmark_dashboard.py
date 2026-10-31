"""Offline, read-only saved-data timings; derived fixture writes use temp folders.

Run: python scripts/benchmark_dashboard.py --repeats 5 --concurrency
No benchmarks request odds, news, or other remote data. Timings are local samples,
not browser interaction timings. The concurrency option runs actual isolated
backtest and numerical-equivalence regression fixtures, not production jobs.
"""
import argparse
from collections import OrderedDict
from contextlib import redirect_stdout,redirect_stderr
import io
import json
from pathlib import Path
from statistics import median
import sys
from threading import RLock,Event
from time import perf_counter
import tracemalloc
from unittest.mock import patch
import unittest

import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dash._utils import to_json
from models.replay import input_digest
from models.Player_Stats.Usage_Opportunity.historical_data import UsageHistoryRepository,eligible
from sports_analytics.frame_cache import FrameCache
from sports_analytics.market_view import market_sections
from sports_analytics.nfl_markets import NflMarketService
from sports_analytics.service import DataService


def measure(work,repeats):
    samples=[];value=None
    for _ in range(repeats):
        start=perf_counter();value=work();samples.append((perf_counter()-start)*1000)
    return dict(samples_ms=[round(v,3) for v in samples],median_ms=round(median(samples),3)),value


def python_peak(work):
    tracemalloc.start()
    try:
        work();return tracemalloc.get_traced_memory()[1]
    finally:tracemalloc.stop()


def quick_filter_benchmark():
    import check_mispricing as fixtures
    from check_dashboard_efficiency import post
    from sports_analytics.mispricing import criteria_values,filter_context,FILTER_CATEGORY
    fixture=fixtures.MispricingChecks();fixture.setUp();release=Event();entered=Event()
    forecast=fixture.game_services['PPP'].forecast.return_value
    def blocked(snapshot):
        entered.set()
        if not release.wait(10):raise RuntimeError('Benchmark fit was not released')
        return forecast
    fixture.game_services['PPP'].forecast.side_effect=blocked
    location='#nfl-game-123';selection=fixture.saved['pulled_at']
    request=filter_context(location,selection,['PPP'],['WHP'],criteria_values(75,5))
    values={'mispricing-request':request,'market-category':FILTER_CATEGORY,'market-url':location,
        'market-snapshot':selection,'market-quick-game-methods':['PPP'],'market-quick-player-methods':['WHP'],
        'market-quick-probability':75,'market-quick-edge':5,'workspace-page':'market'}
    try:
        start=perf_counter();reply=post(fixture.app,'quick_filter_markets',values,'mispricing-request.data')
        elapsed=(perf_counter()-start)*1000
        if not entered.wait(2):raise AssertionError('Forecast never started')
        history=reply['response']['mispricing-forecast-history']['data']
        assert history['pending']
        release.set();fixture.app._filter_jobs.tasks.queue.tasks.join()
        values['mispricing-forecast-history']=history
        ready=post(fixture.app,'quick_filter_markets',values,'market-quick-poll.n_intervals')
        assert 'Top 3 by edge' in str(ready)
        values['mispricing-request']=filter_context(location,selection,['PPP'],['WHP'],criteria_values(80,5))
        values['market-quick-probability']=80
        post(fixture.app,'quick_filter_markets',values,'mispricing-request.data')
        fixture.app._filter_jobs.tasks.queue.tasks.join()
        assert fixture.game_services['PPP'].forecast.call_count==1
        return dict(submit_while_fit_blocked_ms=round(elapsed,3),request_state_bytes=len(json.dumps(history).encode()),
                    game_fits_after_two_thresholds=1,workload='controlled offline HTTP fixture')
    finally:
        release.set();fixture.doCleanups()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats',type=int,default=5)
    parser.add_argument('--concurrency',action='store_true')
    args=parser.parse_args()
    if args.repeats<1:parser.error('repeats must be positive')
    root=Path(__file__).resolve().parents[1]
    source=object.__new__(NflMarketService);source._csv_cache=OrderedDict();source._csv_lock=RLock()
    files=sorted((root/'data/market_data').glob('nba_*.csv'))
    if not files:parser.error('No saved NBA market CSVs; create local fixtures or pull a snapshot separately.')
    snapshots=[source._read_csv(path) for path in files]
    snapshot=max(snapshots,key=lambda value:len(value['rows']))
    groups={row['group'] for row in snapshot['rows']}
    category=max(groups,key=lambda group:sum(row['group']==group for row in snapshot['rows']))
    snapshot={**snapshot,'sport':'NBA'}
    eager=lambda:to_json(market_sections(snapshot,category,method='WHP',loading=True,lazy=False))
    lazy=lambda:to_json(market_sections(snapshot,category,method='WHP',loading=True))
    report=dict(conditions=dict(repeats=args.repeats,browser_measured=False,network='blocked',
                               timing='perf_counter; Python allocations separately via tracemalloc'))
    with patch('requests.sessions.Session.request',side_effect=AssertionError('Benchmark forbids remote requests')):
        baseline,before=measure(eager,args.repeats);optimized,after=measure(lazy,args.repeats)
        baseline.update(bytes=len(before.encode()),python_peak_bytes=python_peak(eager))
        optimized.update(bytes=len(after.encode()),python_peak_bytes=python_peak(lazy))
        report['market_render']=dict(category=category,selections=sum(row['group']==category for row in snapshot['rows']),
            eager_reference=baseline,lazy=optimized,payload_reduction=1-optimized['bytes']/baseline['bytes'])
        assert optimized['bytes']<=baseline['bytes']*.25,'Initial payload reduction below 75%'
        repo=UsageHistoryRepository();names=[snapshot['home_team'],snapshot['away_team']]
        load=lambda:repo.load('NBA',snapshot['commence_time'],names)
        decode=DataService._decode.decode
        histories={}
        for label,cache in [('four_entry_reference',FrameCache(decode,maxsize=4)),('bounded_16_entry',FrameCache(decode))]:
            with patch.object(DataService,'_decode',staticmethod(cache)):
                cold,frames=measure(load,1);warm,last=measure(load,args.repeats)
                pd.testing.assert_frame_equal(frames[0],last[0]);pd.testing.assert_frame_equal(frames[1],last[1])
                histories[label]=dict(cold=cold,warm=warm,decode_cache=cache.cache_info()._asdict())
                if label=='four_entry_reference':reference=frames
                else:
                    pd.testing.assert_frame_equal(reference[0],frames[0]);pd.testing.assert_frame_equal(reference[1],frames[1])
        report['history_load']=histories
        cutoff=min(pd.Timestamp(snapshot['pulled_at']),pd.Timestamp(snapshot['commence_time']))
        players,teams=(eligible(frame,cutoff,'NBA') for frame in frames)
        old=lambda:players.to_json(orient='table',date_format='iso',index=False)+teams.to_json(orient='table',date_format='iso',index=False)
        legacy,blob=measure(old,args.repeats);current,_=measure(lambda:input_digest(players,teams),args.repeats)
        report['input_identity']=dict(json_reference=legacy,content_fingerprint=current,temporary_json_bytes=len(blob.encode()),
                                      player_rows=len(players),team_rows=len(teams))
        report['quick_filter']=quick_filter_benchmark()
        if args.concurrency:
            import check_backtest_concurrency as concurrency
            stream=io.StringIO();start=perf_counter()
            with redirect_stdout(stream),redirect_stderr(stream):
                result=unittest.TextTestRunner(stream=stream).run(unittest.defaultTestLoader.loadTestsFromModule(concurrency))
            if not result.wasSuccessful():raise AssertionError(stream.getvalue())
            report['concurrent_backtests']=dict(tests=result.testsRun,elapsed_seconds=round(perf_counter()-start,3),
                result='passed',workload='isolated backtest workers, live filtering and sequential numerical equivalence fixtures')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
