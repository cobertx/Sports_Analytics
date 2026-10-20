"""Prepare league history, price saved NBA/NFL markets or compare models."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import pandas as pd
from dotenv import load_dotenv
from sports_analytics.nfl_markets import NflMarketService
from models.Player_Stats.Weighted_Historical_Performance.prediction_service import WHPMarketService
from models.Player_Stats.Usage_Opportunity import UsageOpportunityMarketService
from .config import MatchupConfig
from .historical_data import MatchupHistoryRepository,atomic_json
from .prediction_service import OpponentAdjustedMarketService
from .backtesting import walk_forward_backtest
from .model_comparison import compare_models


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sport',required=True,choices=['NBA','NFL','NCAAF'])
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--refresh',action='store_true')
    parser.add_argument('--start',help='Timezone-aware matchup start for preparation')
    parser.add_argument('--teams',nargs=2,default=[])
    parser.add_argument('--snapshot',type=Path,help='Saved market CSV or canonical snapshot JSON')
    parser.add_argument('--scenarios',type=Path,help='Optional pregame relative lineup scenarios JSON')
    parser.add_argument('--simulations',type=int,default=12000)
    parser.add_argument('--backtest-snapshots',type=Path)
    parser.add_argument('--actuals',type=Path,help='CSV: game_id,subject,market,value')
    parser.add_argument('--compare',action='store_true',help='Compare WHP, UO and OAM on supplied historical snapshots')
    parser.add_argument('--output',type=Path,help='Write JSON report instead of printing it')
    args=parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parents[3]/'.env')
    try:
        repository=MatchupHistoryRepository()
        if args.prepare:
            if not args.start:parser.error('--prepare requires --start')
            print(repository.prepare(args.sport,args.start,args.teams,args.refresh));return 0
        base=MatchupConfig.for_sport(args.sport)
        config=replace(base,baseline=replace(base.baseline,simulations=args.simulations))
        scenarios=json.loads(args.scenarios.read_text(encoding='utf-8')) if args.scenarios else []
        service=OpponentAdjustedMarketService(repository,config,scenarios)
        if args.backtest_snapshots:
            if not args.actuals:parser.error('Historical replay requires --actuals')
            snapshots=json.loads(args.backtest_snapshots.read_text(encoding='utf-8'))
            if any(s['sport']!=args.sport for s in snapshots):parser.error('Archive sport must match --sport')
            actuals=pd.read_csv(args.actuals,dtype={'game_id':str})
            result=(compare_models({'WHP':WHPMarketService(),'UO':UsageOpportunityMarketService(),'OAM':service},snapshots,actuals)
                    if args.compare else walk_forward_backtest(service,snapshots,actuals))
        else:
            if not args.snapshot:parser.error('Choose --prepare, --snapshot or --backtest-snapshots')
            snapshot=(NflMarketService(None,repository.root/'reader.sqlite3')._read_csv(args.snapshot)
                      if args.snapshot.suffix.lower()=='.csv' else json.loads(args.snapshot.read_text(encoding='utf-8')))
            snapshot['sport']=args.sport
            forecast=service.forecast(snapshot)
            result=dict(forecasts=[dict(subject=k[0],market=k[1],**v.summary()) for k,v in forecast.forecasts.items()],
                        unavailable=[dict(subject=k[0],market=k[1],reason=e) for k,e in forecast.errors.items()],
                        selections=[dict(subject=r['subject'],market=r['market_key'],line=r['line'],side=r['selection'],
                                         win=p.win,loss=p.loss,push=p.push,probability=p.probability,fair_odds=p.odds)
                                    for r in snapshot['rows'] if (p:=forecast.selection(r)) is not None])
        if args.output:atomic_json(args.output,result)
        else:print(json.dumps(result,indent=2,allow_nan=False))
        return 0
    except (ValueError,RuntimeError,OSError) as exc:
        print(f'Opponent-adjusted matchup unavailable: {exc}',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
