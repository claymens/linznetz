import sys
import os
from datetime import datetime

LASTRUN_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lastrun.log")
OK = "✅ "


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            s.write(data)

    def flush(self):
        for s in self._streams:
            s.flush()


def run():
    from get_data import run_archiver
    from db_update import main as update_db

    ha_full = "--ha_full" in sys.argv
    ha = "--ha" in sys.argv or ha_full

    ha_full_period = None
    if ha_full:
        idx = sys.argv.index("--ha_full")
        if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("-"):
            ha_full_period = sys.argv[idx + 1]

    steps = 3 if ha else 2

    print("━" * 50)
    print(f"  Step 1/{steps} — Downloading data from portal")
    print("━" * 50)
    run_archiver()

    print("━" * 50)
    print(f"  Step 2/{steps} — Updating database")
    print("━" * 50)
    update_db()

    if ha:
        import asyncio
        from ha_import import main as ha_import
        print("━" * 50)
        print(f"  Step 3/{steps} — Pushing to Home Assistant")
        print("━" * 50)
        asyncio.run(ha_import(full=ha_full, last_period=ha_full_period))

    print(f"{OK}All done.\n")


with open(LASTRUN_LOG, "w") as _log:
    _log.write(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    _log.flush()
    sys.stdout = _Tee(sys.__stdout__, _log)
    sys.stderr = _Tee(sys.__stderr__, _log)
    try:
        run()
    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
