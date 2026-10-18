"""Reproducible historical imports: python -m sports_analytics.ingest --help."""

import argparse
from pathlib import Path

from dotenv import load_dotenv

from .data import DataSourceError, Query
from .service import DataService


def main():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    parser = argparse.ArgumentParser(description="Import historical game statistics into the local cache")
    parser.add_argument("sport", choices=["NBA", "NFL", "NCAAF"])
    parser.add_argument("--seasons", type=int, nargs="+", required=True, help="NBA uses the starting year, e.g. 2024 for 2024-25")
    parser.add_argument("--entity", choices=["team", "player"], default="team")
    parser.add_argument("--season-type", choices=["regular", "postseason"], default="regular")
    parser.add_argument("--team", default="", help="NCAAF school name, e.g. Michigan")
    parser.add_argument("--week", type=int)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    service = DataService()
    failed = False
    for season in args.seasons:
        try:
            query = Query(args.sport, season, args.entity, args.season_type, args.team, args.week)
            result = service.load(query, refresh=args.refresh)
            print(f"{args.sport} {season} {args.entity}: {len(result.frame):,} rows | {result.source} | {result.status} | {result.fetched_at}")
            if result.notice:
                print(f"Warning: {result.notice}")
                failed = True
        except (ValueError, DataSourceError) as exc:
            print(f"{args.sport} {season}: {exc}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
