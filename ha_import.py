"""
Push power_data.db statistics into Home Assistant for the Energy Dashboard.
Run once to backfill all history; safe to re-run after new data is imported —
only complete days are ever sent, so HA never shows a partial-day bar.

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
TZ       = ZoneInfo(os.getenv("TZ", "Europe/Vienna"))
CHUNK    = 2000  # hourly entries per WebSocket message

ALLOWED_COLS = {"energy_kwh", "grid_kwh", "community_kwh"}


SERIES = [
    ("sensor.linz_netz_energy",    "Linz Netz Energy",    "energy_kwh"),
    ("sensor.linz_netz_grid",      "Linz Netz Grid",      "grid_kwh"),
    ("sensor.linz_netz_community", "Linz Netz Community", "community_kwh"),
]


def get_meta(key: str) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_last_import(until_hour: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('ha_last_import', ?)",
            (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),)
        )
        # Store the actual data boundary so the next incremental run starts from here,
        # not from the wall-clock time this script ran (which would be after until_hour).
        # The boundary hour (until_hour) is re-sent on the next import to allow HA's
        # recorder/import_statistics to upsert idempotently.
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('ha_last_until', ?)",
            (until_hour,)
        )
        conn.commit()


def get_complete_until_hour() -> str | None:
    """Return midnight after the last complete day, or None if no complete days exist.

    A day is complete when it has >= 92 quarter-hour readings. 92 is the minimum
    for any real complete day (spring-forward loses 1h → 23h × 4 = 92 slots).
    Normal and fall-back days produce 96 stored rows (fall-back duplicates are
    dropped by the PRIMARY KEY on timestamp_from).
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT DATE(timestamp_from) AS day
            FROM consumption
            GROUP BY day
            HAVING COUNT(*) >= 92
            ORDER BY day DESC
            LIMIT 1
        """).fetchone()
    if not row:
        return None
    last_complete = datetime.strptime(row[0], "%Y-%m-%d")
    until = last_complete + timedelta(days=1)
    return until.strftime("%Y-%m-%dT%H:00:00")


def load_hourly(col: str, since_hour: str | None, until_hour: str | None) -> list[dict]:
    """Aggregate 15-min intervals into hourly buckets with a running cumulative sum.

    Cumulative sum is computed in SQL via a window function so the database
    handles the full-time aggregation internally. Only rows within
    [since_hour, until_hour) are returned to Python.
    """
    if col not in ALLOWED_COLS:
        raise ValueError(f"Invalid column: {col!r} — must be one of {sorted(ALLOWED_COLS)}")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        where_clauses: list[str] = []
        params: list[str] = []
        if since_hour is not None:
            where_clauses.append("hour >= ?")
            params.append(since_hour)
        if until_hour is not None:
            where_clauses.append("hour < ?")
            params.append(until_hour)
        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        query = f"""
            SELECT hour, kwh, running
            FROM (
                SELECT
                    strftime('%Y-%m-%dT%H:00:00', timestamp_from) AS hour,
                    ROUND(SUM({col}), 4)                           AS kwh,
                    ROUND(SUM(SUM({col})) OVER (
                        ORDER BY strftime('%Y-%m-%dT%H:00:00', timestamp_from)
                    ), 4)                                          AS running
                FROM consumption
                GROUP BY hour
            )
            {where_sql}
            ORDER BY hour
        """
        rows = conn.execute(query, params).fetchall()

    result = []
    for hour, kwh, running in rows:
        dt = datetime.fromisoformat(hour).replace(tzinfo=TZ)
        result.append({"start": dt.isoformat(), "sum": running, "state": kwh or 0.0})
    return result


async def import_series(ws, msg_id: int, statistic_id: str, name: str, col: str,
                        since_hour: str | None, until_hour: str | None) -> tuple[int, bool]:
    stats = load_hourly(col, since_hour, until_hour)
    print(f"{name}: {len(stats)} entries")
    all_ok = True
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
        if not ok:
            all_ok = False
        end = min(i + CHUNK, len(stats))
        print(f"  {'✅' if ok else '❌'} entries {i + 1}–{end}" +
              (f"  error: {result.get('error')}" if not ok else ""))
        msg_id += 1
    return msg_id, all_ok


async def main(full: bool = False, last_period: str | None = None):
    missing = [k for k, v in [("HA_URL", HA_URL), ("HA_TOKEN", HA_TOKEN)] if not v]
    if missing:
        raise SystemExit(f"{', '.join(missing)} not set — add to .env")
    if not HA_URL.startswith("ws://") and not HA_URL.startswith("wss://"):
        raise SystemExit(f"HA_URL must start with ws:// or wss://, got: {HA_URL}")

    until_hour = get_complete_until_hour()
    if not until_hour:
        print("No complete day data found — nothing to import.")
        return

    if full:
        if last_period == "1m":
            now = datetime.now()
            first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            first_of_last_month = (first_of_this_month - timedelta(days=1)).replace(day=1)
            since_hour = first_of_last_month.strftime("%Y-%m-%dT%H:00:00")
            print(f"Full reimport for last month from {since_hour} → {until_hour}\n")
        else:
            since_hour = None
            print(f"Full reimport — sending complete history up to {until_hour}\n")
    else:
        last_import = get_meta("ha_last_import")
        last_run    = get_meta("last_run")  # unix timestamp written by db_update.py

        if last_import and last_run:
            last_import_dt = datetime.fromisoformat(last_import)
            last_run_dt    = datetime.fromtimestamp(float(last_run))
            if last_import_dt >= last_run_dt:
                print("Database unchanged since last import — nothing to do.")
                return

        last_until = get_meta("ha_last_until")
        if last_until:
            since_hour = last_until
            print(f"Incremental import from {since_hour} → {until_hour} (complete days only)\n")
        else:
            since_hour = None
            print(f"First import — sending full history up to {until_hour}\n")

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

            all_ok = True
            msg_id = 1
            for statistic_id, name, col in SERIES:
                msg_id, ok = await import_series(ws, msg_id, statistic_id, name, col,
                                                 since_hour, until_hour)
                if not ok:
                    all_ok = False
                print()

        if all_ok:
            set_last_import(until_hour)
            print("Import complete.")
        else:
            print("Import finished with errors — ha_last_import not updated, will retry next run.")

    except OSError as e:
        raise SystemExit(f"Cannot reach Home Assistant at {HA_URL}\n  {e}")
    except websockets.exceptions.InvalidURI:
        raise SystemExit(f"Invalid HA_URL format: {HA_URL}\n  Expected: ws://<host>:8123/api/websocket")
    except websockets.exceptions.WebSocketException as e:
        raise SystemExit(f"WebSocket error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
