"""One local-data replay. Standard output carries only progress/result messages."""
from contextlib import contextmanager, redirect_stdout
import json
import sys
import threading
from time import monotonic


@contextmanager
def progress_channel(send, interval=5):
    """Report a living worker during long fits without inventing completed work."""
    stopped=threading.Event()
    lock=threading.Lock()
    current=[0,0,'Starting backtest worker',monotonic()]
    def progress(done,total,message):
        with lock:
            current[:]=done,total,message,monotonic()
            send('progress',done=done,total=total,message=message)
    def heartbeat():
        while not stopped.wait(interval):
            with lock:
                done,total,message,started=current
                send('progress',done=done,total=total,
                     message=f'{message} · working ({int(monotonic()-started)}s in this step)')
    thread=threading.Thread(target=heartbeat,name='backtest-heartbeat',daemon=True)
    thread.start()
    try:
        yield progress
    finally:
        stopped.set()
        thread.join()


def main():
    output = sys.stdout

    def send(kind, **values):
        output.write(json.dumps(dict(type=kind, **values), allow_nan=False) + '\n')
        output.flush()

    try:
        request = json.loads(sys.stdin.readline())
        # Third-party diagnostics belong on stderr, not the message channel.
        with progress_channel(send) as progress, redirect_stdout(sys.stderr):
            import pandas as pd
            from .backtesting_service import BacktestingService
            from .service import DataService
            service = BacktestingService(root=request['root'], data=DataService(request['cache']),
                now=lambda: pd.Timestamp(request['now']), publish_latest=False)
            result = service.run(**request['parameters'], progress=progress)
        send('result', result=result)
        return 0
    except Exception as exc:
        send('error', message=str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
