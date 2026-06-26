"""
Interactive tool to clear Home Assistant statistics for a sensor.

Usage:
    python clear_sensor_stats.py              # list all sensors, pick interactively
    python clear_sensor_stats.py <search>     # filter list by search string first

Date range:
    Without range  → uses recorder/clear_statistics (wipes everything for that sensor)
    With range     → SSHes into the HA host and deletes rows directly from the SQLite DB
                     (requires passwordless SSH access; host is derived from HA_URL)

.env keys used: HA_URL, HA_TOKEN, HA_SSH_USER (optional, default: root)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import websockets
from dotenv import load_dotenv

load_dotenv()

HA_URL      = os.getenv("HA_URL", "ws://homeassistant.local:8123/api/websocket")
HA_TOKEN    = os.getenv("HA_TOKEN")
HA_SSH_USER = os.getenv("HA_SSH_USER", "root")
HA_DB_PATH  = os.getenv("HA_DB_PATH", "/config/home-assistant_v2.db")

# Extract host from HA_URL (ws://192.168.x.x:8123/...)
_m = re.match(r"wss?://([^:/]+)", HA_URL)
HA_HOST = _m.group(1) if _m else None


# ── helpers ──────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    try:
        val = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def pick_from_list(items: list[str], noun: str = "sensor") -> str:
    for i, item in enumerate(items, 1):
        print(f"  {i:>3}.  {item}")
    print()
    while True:
        raw = ask(f"Select {noun} (number): ")
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        print(f"  Please enter a number between 1 and {len(items)}.")


def parse_date(s: str) -> str:
    """Return ISO-8601 UTC string from YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS input."""
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass
    raise ValueError(f"Cannot parse date: {s!r}  (use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")


# ── WebSocket helpers ─────────────────────────────────────────────────────────

async def ws_connect():
    ws = await websockets.connect(HA_URL)
    msg = json.loads(await ws.recv())
    assert msg.get("type") == "auth_required", f"Unexpected: {msg}"
    await ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
    msg = json.loads(await ws.recv())
    assert msg["type"] == "auth_ok", "HA authentication failed — check HA_TOKEN"
    return ws


async def list_statistic_ids(ws) -> list[dict]:
    await ws.send(json.dumps({"id": 1, "type": "recorder/list_statistic_ids"}))
    msg = json.loads(await ws.recv())
    return msg.get("result", [])


async def clear_all(ws, statistic_id: str) -> bool:
    await ws.send(json.dumps({
        "id": 2,
        "type": "recorder/clear_statistics",
        "statistic_ids": [statistic_id],
    }))
    result = json.loads(await ws.recv())
    return result.get("success", False)


# ── SSH / SQLite date-range deletion ─────────────────────────────────────────

def ssh_delete_range(statistic_id: str, from_dt: str, to_dt: str) -> None:
    """Delete statistics rows for statistic_id between from_dt and to_dt (inclusive) via SSH."""
    if not HA_HOST:
        sys.exit("Cannot determine HA host from HA_URL — set HA_HOST manually.")

    sql = (
        "DELETE FROM statistics "
        "WHERE metadata_id = ("
        f"  SELECT id FROM statistics_meta WHERE statistic_id = '{statistic_id}'"
        ") "
        f"AND start_ts >= strftime('%s', '{from_dt}') "
        f"AND start_ts <= strftime('%s', '{to_dt}');"
    )

    cmd = ["ssh", f"{HA_SSH_USER}@{HA_HOST}", f"sqlite3 {HA_DB_PATH} \"{sql}\""]
    print(f"\nRunning via SSH on {HA_HOST}:")
    print(f"  {' '.join(cmd)}\n")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"SSH/sqlite3 error:\n{result.stderr}")
        sys.exit(1)
    print("Rows deleted. HA will pick up the change on next recorder cycle.")


def ssh_count_range(statistic_id: str, from_dt: str, to_dt: str) -> int | None:
    """Return number of rows that would be deleted, or None on SSH error."""
    if not HA_HOST:
        return None
    sql = (
        "SELECT COUNT(*) FROM statistics "
        "WHERE metadata_id = ("
        f"  SELECT id FROM statistics_meta WHERE statistic_id = '{statistic_id}'"
        ") "
        f"AND start_ts >= strftime('%s', '{from_dt}') "
        f"AND start_ts <= strftime('%s', '{to_dt}');"
    )
    cmd = ["ssh", f"{HA_SSH_USER}@{HA_HOST}", f"sqlite3 {HA_DB_PATH} \"{sql}\""]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    search = sys.argv[1].lower() if len(sys.argv) > 1 else ""

    if not HA_TOKEN:
        sys.exit("HA_TOKEN not set in .env")

    print(f"Connecting to {HA_URL} …")
    ws = await ws_connect()
    print("Authenticated.\n")

    all_stats = await list_statistic_ids(ws)
    all_ids = sorted(s["statistic_id"] for s in all_stats)

    if search:
        filtered = [sid for sid in all_ids if search in sid.lower()]
        if not filtered:
            print(f"No sensors matching {search!r}. All available sensors:\n")
            filtered = all_ids
    else:
        filtered = all_ids

    if not filtered:
        sys.exit("No statistics found in Home Assistant.")

    print(f"Found {len(filtered)} sensor(s){f' matching {search!r}' if search else ''}:\n")
    statistic_id = pick_from_list(filtered)
    print(f"\nSelected: {statistic_id}")

    # Date range
    print("\nDate range (leave blank = clear ALL data for this sensor):")
    from_raw = ask("  From (YYYY-MM-DD): ")
    to_raw   = ask("  To   (YYYY-MM-DD): ")

    use_range = bool(from_raw or to_raw)

    if use_range:
        try:
            from_dt = parse_date(from_raw) if from_raw else "1970-01-01T00:00:00+00:00"
            to_dt   = parse_date(to_raw)   if to_raw   else datetime.now(timezone.utc).isoformat()
        except ValueError as e:
            sys.exit(str(e))

        count = ssh_count_range(statistic_id, from_dt, to_dt)
        count_str = f"{count} rows" if count is not None else "an unknown number of rows"

        print(f"\nThis will delete {count_str} for  {statistic_id}")
        print(f"  from  {from_dt}")
        print(f"  to    {to_dt}")
        print(f"  via SSH ({HA_SSH_USER}@{HA_HOST})")
    else:
        print(f"\nThis will wipe ALL historical data for  {statistic_id}")
        print("  (uses recorder/clear_statistics — irreversible)")

    confirm = ask("\nType 'yes' to confirm: ")
    if confirm.lower() != "yes":
        print("Aborted.")
        await ws.close()
        return

    if use_range:
        await ws.close()
        ssh_delete_range(statistic_id, from_dt, to_dt)
    else:
        ok = await clear_all(ws, statistic_id)
        await ws.close()
        if ok:
            print("Done — all statistics cleared.")
        else:
            print("Error clearing statistics.")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
