"""Run with: python app.py"""

import argparse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from sports_analytics.dashboard import create_app

app = create_app()
server = app.server

def main(argv=None):
    """Start the dashboard from either app.py or index.py."""
    parser = argparse.ArgumentParser(description="NBA, NFL, and NCAAF analytics dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
