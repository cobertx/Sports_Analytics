"""Run with: python app/app.py"""

import argparse
import errno
import faulthandler
import os
from pathlib import Path
import socket
import sys

from dotenv import load_dotenv
from werkzeug.debug import DebuggedApplication
from werkzeug.serving import ThreadedWSGIServer

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.paths import PROJECT_DIRECTORY

load_dotenv(PROJECT_DIRECTORY / ".env")

from app.dashboard import create_app

app = create_app()
server = app.server


class ExclusiveDashboardServer(ThreadedWSGIServer):
    """Bind a Windows listener without sharing its address with another process."""

    allow_reuse_address = False

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE or getattr(exc, 'winerror', None) in (10013, 10048):
                message = (f"Port {self.port} is already in use or reserved. "
                           "Use the running dashboard or choose another --port.")
                raise OSError(exc.errno, message) from exc
            raise


@server.before_request
def start_history_updates():
    app._history_updates.start()


def main(argv=None):
    """Start the dashboard from either app.py or index.py."""
    parser = argparse.ArgumentParser(description="NBA, NFL, and NCAAF analytics dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--debug", action="store_true", help="Enable the debugger and Dash development tools.")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if sys.stderr is not None:
        faulthandler.enable()

    # The Windows scheduled worker also covers periods when the dashboard is
    # closed. This loop provides startup catch-up and other deployment support.
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE") and not args.host.startswith("unix://"):
        with ExclusiveDashboardServer(args.host, args.port, server) as dashboard:
            server.debug = args.debug
            app.enable_dev_tools(debug=args.debug, dev_tools_hot_reload=False)
            if args.debug:
                debugger = DebuggedApplication(server, evalex=True)
                debugger.trusted_hosts.append(args.host)
                dashboard.app = debugger
            app._history_updates.start()
            dashboard.log_startup()
            print(f"Dashboard PID {os.getpid()}. Press Ctrl+C to stop.", flush=True)
            dashboard.serve_forever()
    else:
        app._history_updates.start()
        app.run(host=args.host, port=args.port, debug=args.debug)

    print(f"Dashboard PID {os.getpid()} stopped.", flush=True)


if __name__ == "__main__":
    main()
