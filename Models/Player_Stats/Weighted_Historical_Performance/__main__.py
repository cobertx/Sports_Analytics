"""Prepare history or price a canonical player-target CSV without an odds pull."""

import argparse
import json
from pathlib import Path
import pandas as pd

from .backtesting import walk_forward_backtest
from .core import WHPConfig, WeightedHistoricalPerformance
from .historical_data import PlayerHistoryRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", required=True, choices=["NBA", "NFL", "NCAAF"])
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--start", help="Matchup start including timezone; required with --prepare")
    parser.add_argument("--teams", nargs=2, help="Full home and away names; required for NCAAF --prepare")
    parser.add_argument("--history", type=Path, help="Single-player CSV with game_id,event_date,available_at,value")
    parser.add_argument("--as-of", help="Prediction cutoff including timezone")
    parser.add_argument("--line", type=float)
    parser.add_argument("--side", choices=["Over", "Under"], default="Over")
    parser.add_argument("--count", action="store_true")
    parser.add_argument("--signed", action="store_true", help="Allow negative net football yardage")
    parser.add_argument("--opportunity", type=float, help="Known pregame workload projection; use with --beta below 1")
    parser.add_argument("--beta", type=float, default=1)
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--evaluation-start", help="Start of untouched test interval, with timezone")
    args = parser.parse_args()
    if args.prepare:
        if not args.start:
            parser.error("--prepare requires --start")
        print(PlayerHistoryRepository().prepare(args.sport, args.start, args.teams or (), args.refresh))
        return
    if not args.history or not args.as_of:
        parser.error("Pricing/backtesting requires --history and --as-of")
    frame = pd.read_csv(args.history, dtype={"game_id": str})
    from dataclasses import replace
    config = replace(WHPConfig.for_sport(args.sport), beta=args.beta)
    if args.backtest:
        # Truncate replay at the declared audit cutoff too.
        from .core import eligible_history
        result = walk_forward_backtest(eligible_history(frame, args.as_of), config,
            nonnegative=not args.signed, count=args.count, line=args.line, side=args.side,
            evaluation_start=args.evaluation_start)
    else:
        forecast = WeightedHistoricalPerformance(config).forecast(frame, args.as_of,
            nonnegative=not args.signed, count=args.count, opportunity_override=args.opportunity)
        result = dict(mean=forecast.mean, variance=forecast.variance, family=forecast.family,
                      diagnostics=forecast.diagnostics)
        if args.line is not None:
            price = forecast.market(args.line, args.side)
            result.update(win=price.win, loss=price.loss, push=price.push,
                          probability=price.probability, fair_odds=price.odds)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
