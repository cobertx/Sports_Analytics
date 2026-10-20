"""Prepare NBA/NFL history, price saved quotes or replay a pregame archive."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import pandas as pd
from dotenv import load_dotenv
from sports_analytics.nfl_markets import NflMarketService
from .config import UsageOpportunityConfig
from .historical_data import UsageHistoryRepository
from .prediction_service import UsageOpportunityMarketService
from .backtesting import walk_forward_backtest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sport', required=True, choices=['NBA','NFL'])
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--start', help='Matchup start, including timezone; required for --prepare')
    parser.add_argument('--teams', nargs=2, default=[], help='Home and away full names')
    parser.add_argument('--snapshot', type=Path, help='Saved market CSV or canonical snapshot JSON')
    parser.add_argument('--simulations', type=int, default=12000)
    parser.add_argument('--backtest-snapshots', type=Path, help='JSON list of canonical pregame snapshots')
    parser.add_argument('--actuals', type=Path, help='CSV: game_id,subject,market,value')
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parents[3]/'.env')
    try:
        repository = UsageHistoryRepository()
        if args.prepare:
            if not args.start:
                parser.error('--prepare requires --start')
            print(repository.prepare(args.sport, args.start, args.teams, args.refresh))
            return 0
        config = replace(UsageOpportunityConfig.for_sport(args.sport), simulations=args.simulations)
        service = UsageOpportunityMarketService(repository, config)
        if args.backtest_snapshots:
            if not args.actuals:
                parser.error('--backtest-snapshots requires --actuals')
            result = walk_forward_backtest(service, json.loads(args.backtest_snapshots.read_text(encoding='utf-8')),
                                          pd.read_csv(args.actuals, dtype={'game_id': str}))
        else:
            if not args.snapshot:
                parser.error('Choose --prepare, --snapshot or --backtest-snapshots')
            if args.snapshot.suffix.lower() == '.csv':
                reader = NflMarketService(None, repository.root/'reader.sqlite3')
                snapshot = reader._read_csv(args.snapshot)
            else:
                snapshot = json.loads(args.snapshot.read_text(encoding='utf-8'))
            snapshot['sport'] = args.sport
            forecast = service.forecast(snapshot)
            result = dict(forecasts=[dict(subject=key[0], market=key[1], **value.summary())
                                     for key, value in forecast.forecasts.items()],
                          unavailable=[dict(subject=key[0], market=key[1], reason=error)
                                       for key, error in forecast.errors.items()],
                          selections=[dict(subject=row['subject'], market=row['market_key'], line=row['line'],
                                           selection=row['selection'], win=price.win, loss=price.loss, push=price.push,
                                           probability=price.probability, fair_odds=price.odds)
                                      for row in snapshot['rows'] if (price := forecast.selection(row)) is not None])
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'Usage & Opportunity unavailable: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
