"""Compare cold calculations with run-local reuse on saved inputs, without HTTP.

Example: python scripts/benchmark_backtesting.py --sport NFL --model ppp --games 20
This measures model replay only, not input loading or report rendering. Both
paths use the current model and must produce exactly equal records and skips.
"""
import argparse
from pathlib import Path
import sys
from time import perf_counter
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sports_analytics.backtesting_service import BacktestingService, eligible_pairs, game_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sport', choices=['NBA', 'NFL', 'NCAAF'], default='NFL')
    parser.add_argument('--model', choices=['ppp', 'epa', 'elo'], default='ppp')
    parser.add_argument('--games', type=int, default=10)
    parser.add_argument('--lookback', type=int)
    args = parser.parse_args()
    if args.games < 1 or (args.lookback is not None and args.lookback < 2):
        parser.error('Games must be positive and lookback must be at least 2.')
    service = BacktestingService()
    with patch('requests.sessions.Session.request', side_effect=RuntimeError('Benchmark is offline')):
        frame, dataset = service.game_inputs(args.sport, args.model)
        frame = frame[frame.season_type.eq('regular')]
        now = pd.Timestamp.now(tz='UTC')
        games = eligible_pairs(frame, '__all__', pd.Timestamp('1900-01-01', tz='UTC'), now, now)[-args.games:]
        if not games: parser.error('No saved regular-season game pairs are available.')
        results, timings = [], []
        for label, replay in [('Uncached', BacktestingService._games.__wrapped__),
                              ('Run-local cache', BacktestingService._games)]:
            result = dict(context=dict(lookback=args.lookback), records=[], skipped=[])
            start = perf_counter()
            replay(service, result, game_model(args.sport, args.model), frame, dataset, games)
            timings.append(perf_counter()-start); results.append(result)
            print(f'{label}: {timings[-1]:.3f}s; {len(result["records"])} evaluated, {len(result["skipped"])} skipped', flush=True)
        if results[0] != results[1]: raise RuntimeError('Replay results differ.')
        if not results[0]['records']: raise RuntimeError('No predictions were evaluated; choose a different saved cohort.')
        print(f'Identical results. Reuse speedup: {timings[0]/timings[1]:.2f}x')


if __name__ == '__main__': main()
