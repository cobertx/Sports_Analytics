"""Run PTR on a saved NBA market snapshot."""

import argparse
import json
from pathlib import Path
import sys

from sports_analytics.data import DataSourceError

from .prediction_service import PlayingTimeRotationMarketService


def main(factory=PlayingTimeRotationMarketService, *, description=__doc__):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--prepare", action="store_true", help="Prepare the saved public NBA box-score inputs.")
    args = parser.parse_args()
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        service = factory()
        if args.prepare:
            print(service.prepare(snapshot), file=sys.stderr)
        result = service.forecast(snapshot)
    except (DataSourceError, OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")
    output = dict(forecasts=[dict(player=key[0], market=key[1], **forecast.summary(),
                                          values=forecast.values, probabilities=forecast.probabilities)
                                     for key, forecast in result.forecasts.items()],
                          errors=[dict(player=key[0], market=key[1], error=error)
                                  for key, error in result.errors.items()])
    if getattr(result, "availability", None):
        output["availability"] = result.availability
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
