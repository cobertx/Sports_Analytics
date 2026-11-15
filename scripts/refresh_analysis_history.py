"""Scheduled entry point: no dashboard or sportsbook requests are started."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    from sports_analytics.history_updates import HistoryUpdater
    updater = HistoryUpdater()
    try:
        status = updater.tick(wait=True)
        failed = [name for name, state in status.get('sources', {}).items() if state.get('error')]
        if sys.stdout:
            print(f'History update completed: {len(status.get("sources",{}))} sources; {len(failed)} need retry.')
        return bool(failed)
    except Exception as exc:
        updater._record('Automatic updater',pending=False,error=str(exc))
        return 1
    finally:
        updater.stop()


if __name__ == '__main__':
    raise SystemExit(main())
