"""One local-data replay. Standard output carries only progress/result messages."""
from contextlib import redirect_stdout
import json
import sys


def main():
    output = sys.stdout

    def send(kind, **values):
        output.write(json.dumps(dict(type=kind, **values), allow_nan=False) + '\n')
        output.flush()

    try:
        request = json.loads(sys.stdin.readline())
        # Third-party diagnostics belong on stderr, not the message channel.
        with redirect_stdout(sys.stderr):
            import pandas as pd
            from .backtesting_service import BacktestingService
            from .service import DataService
            service = BacktestingService(root=request['root'], data=DataService(request['cache']),
                now=lambda: pd.Timestamp(request['now']), publish_latest=False)
            result = service.run(**request['parameters'], progress=lambda done, total, message:
                send('progress', done=done, total=total, message=message))
        send('result', result=result)
        return 0
    except Exception as exc:
        send('error', message=str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
