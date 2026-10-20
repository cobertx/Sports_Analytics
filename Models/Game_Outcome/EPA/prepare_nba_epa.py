"""Prepare/audit public NBA EPA/EPV inputs; this does not enable a forecast.

Project seasons use starting years: 2025 = 2025-26. First run:
    python -m Models.Game_Outcome.EPA.prepare_nba_epa --seasons 2024 2025 --download
Later offline audits reuse cached archives without --download.
No API keys, Odds API requests, or extra Python packages are required.
"""

import argparse
from pathlib import Path

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sports_analytics.data import DataSourceError
from Models.Game_Outcome.EPA.nba_epa_data import prepare_inputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", nargs="+", type=int, default=[2024, 2025])
    parser.add_argument("--download", action="store_true", help="Download missing public archives")
    parser.add_argument("--refresh", action="store_true", help="Replace cached archives (requires --download)")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[3] / "data/modeling/nba/epa")
    args = parser.parse_args()
    if args.refresh and not args.download:
        parser.error("--refresh requires --download")
    print("Preparing NBA EPA/EPV inputs; no market odds will be requested.", flush=True)
    try:
        report = prepare_inputs(args.seasons, args.output, download=args.download, refresh=args.refresh)
    except (DataSourceError, OSError, EOFError, ValueError, KeyError) as exc:
        print(f"Input preparation failed: {exc}")
        return 1
    for season, audit in report["seasons"].items():
        print(f"{season}-{str(int(season)+1)[-2:]}: {audit['possession_games']:,} possession games; "
              f"{audit['event_rows']:,} events; {len(audit['score_mismatches']):,} scoring mismatches in "
              f"{audit['box_score_team_rows_compared']:,} compared box-score team rows.")
        print(f"  Final event scores: {len(audit['pbp_score_mismatches']):,} mismatches in "
              f"{audit['pbp_box_score_team_rows_compared']:,} compared rows. "
              f"Invalid possession rows: {audit['invalid_possession_rows']:,}.")
    print(f"Readiness report: {args.output.resolve() / 'readiness.json'}")
    print("EPA is not enabled: state construction, model training and forecast validation are still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
