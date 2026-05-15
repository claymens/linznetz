"""
Push power_data.db statistics into Home Assistant for the Energy Dashboard.
Run once to backfill all history; safe to re-run after new data is imported —
only entries newer than the last import are sent (with a 25-hour overlap to
catch any incomplete hours from the previous run).

Requires in .env:
  HA_URL   = ws://10.0.0.x:8123/api/websocket
  HA_TOKEN = <long-lived access token>
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import websockets
from dotenv import load_dotenv

load_dotenv()

DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "power_data.db")
HA_URL   = os.getenv("HA_URL", "ws://homeassistant.local:8123/api/websocket")
HA_TOKEN = os.getenv("HA_TOKEN")
TZ       = ZoneInfo("Europe/Vienna")
CHUNK    = 2000  # hourly entries per WebSocket message

SERIES = [
    ("sensor.linz_netz_energy",    "Linz Netz Energy",    "energy_kwh"),
    ("sensor.linz_netz_grid",      "Linz Netz Grid",      "grid_kwh"),
    ("sensor.linz_netz_community", "Linz Netz Community", "community_kwh"),
]


def get_meta(key: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def set_last_import() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('ha_last_import', ?)",
        (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),)
    )
    conn.commit()
    conn.close()


def load_hourly(col: str, since_hour: str | None) -> list[dict]:
    """Aggregate 15-min intervals into hourly buckets with a running cumulative sum.
    Always computes the full cumulative sum; only returns entries >= since_hour."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(f"""
        SELECT strftime('%Y-%m-%dT%H:00:00', timestamp_from) AS hour,
               ROUND(SUM({col}), 4) AS kwh
        FROM consumption
        GROUP BY hour
        ORDER BY hour
    """).fetchall()
    conn.close()

    result, running = [], 0.0
    for hour, kwh in rows:
        kwh = kwh or 0.0
        running = round(running + kwh, 4)
        if since_hour is None or hour >= since_hour:
            dt = datetime.fromisoformat(hour).replace(tzinfo=TZ)
            result.append({"start": dt.isoformat(), "sum": running, "state": kwh})
    return result


async def import_series(ws, msg_id: int, statistic_id: str, name: str, col: str,
                        since_hour: str | None) -> int:
    stats = load_hourly(col, since_hour)
    print(f"{name}: {len(stats)} entries")
    for i in range(0, len(stats), CHUNK):
        chunk = stats[i : i + CHUNK]
        await ws.send(json.dumps({
            "id": msg_id,
            "type": "recorder/import_statistics",
            "metadata": {
                "has_mean": False,
                "has_sum":  True,
                "name":     name,
                "source":   "recorder",
                "statistic_id": statistic_id,
                "unit_of_measurement": "kWh",
            },
            "stats": chunk,
        }))
        result = json.loads(await ws.recv())
        ok = result.get("success", False)
        end = min(i + CHUNK, len(stats))
        print(f"  {'✅' if ok else '❌'} entries {i + 1}–{end}" +
              (f"  error: {result.get('error')}" if not ok else ""))
        msg_id += 1
    return msg_id


async def main():
    missing = [k for k, v in [("HA_URL", HA_URL), ("HA_TOKEN", HA_TOKEN)] if not v]
    if missing:
        raise SystemExit(f"{', '.join(missing)} not set — add to .env")

    last_import = get_meta("ha_last_import")
    last_run    = get_meta("last_run")  # unix timestamp written by db_update.py

    if last_import and last_run:
        last_import_dt = datetime.fromisoformat(last_import)
        last_run_dt    = datetime.fromtimestamp(float(last_run))
        if last_import_dt >= last_run_dt:
            print("Database unchanged since last import — nothing to do.")
            return

    if last_import:
        since_dt   = datetime.fromisoformat(last_import) - timedelta(hours=2)
        since_hour = since_dt.strftime("%Y-%m-%dT%H:00:00")
        print(f"Incremental import since {since_hour}\n")
    else:
        since_hour = None
        print("First import — sending full history\n")

    print(f"Connecting to {HA_URL} …")
    try:
        async with websockets.connect(HA_URL) as ws:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise SystemExit(f"Unexpected response from HA: {msg}")
            await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            msg = json.loads(await ws.recv())
            if msg["type"] != "auth_ok":
                raise SystemExit("Authentication failed — check HA_TOKEN in .env")
            print("Authenticated.\n")

            msg_id = 1
            for statistic_id, name, col in SERIES:
                msg_id = await import_series(ws, msg_id, statistic_id, name, col, since_hour)
                print()

        set_last_import()
        print("Import complete.")

    except OSError as e:
        raise SystemExit(f"Cannot reach Home Assistant at {HA_URL}\n  {e}")
    except websockets.exceptions.InvalidURI:
        raise SystemExit(f"Invalid HA_URL format: {HA_URL}\n  Expected: ws://<host>:8123/api/websocket")
    except websockets.exceptions.WebSocketException as e:
        raise SystemExit(f"WebSocket error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
