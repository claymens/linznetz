from __future__ import annotations

import csv
import os
import sqlite3
import sys
from datetime import datetime

ARCHIVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "power_archive")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "power_data.db")

DATE_FORMAT = "%d.%m.%Y %H:%M"


def parse_float(value: str) -> float | None:
    v = value.strip().replace(",", ".")
    return float(v) if v else None


def parse_dt(value: str) -> str:
    return datetime.strptime(value.strip(), DATE_FORMAT).isoformat(sep=" ")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consumption (
            timestamp_from   TEXT PRIMARY KEY,
            timestamp_to     TEXT NOT NULL,
            energy_kwh       REAL,
            is_estimated     INTEGER NOT NULL DEFAULT 0,
            grid_kwh         REAL,
            community_kwh    REAL,
            community_status TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Column migrations for older databases
    cols = {row[1] for row in conn.execute("PRAGMA table_info(consumption)")}
    if "community_kwh" in cols and "grid_kwh" not in cols:
        conn.execute("ALTER TABLE consumption RENAME COLUMN community_kwh TO grid_kwh")
        cols = {row[1] for row in conn.execute("PRAGMA table_info(consumption)")}
    if "community_kwh" not in cols:
        conn.execute("ALTER TABLE consumption ADD COLUMN community_kwh REAL")
    conn.commit()


def get_last_run(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_run'").fetchone()
    return float(row[0]) if row else 0.0


def set_last_run(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_run', ?)",
        (str(datetime.now().timestamp()),)
    )
    conn.commit()


def load_csv(path: str) -> list[tuple]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            ts_from = parse_dt(row["Datum von"])
            ts_to   = parse_dt(row["Datum bis"])
            energy  = parse_float(row["Energiemenge in kWh"])
            estimated = 1 if row.get("Ersatzwert", "").strip() else 0
            grid_kwh         = parse_float(row.get("Restnetzbezug in kWh (Energiegemeinschaft)", ""))
            community_status = row.get("Status (Energiegemeinschaft)", "").strip() or None
            if community_status in ("F", None):
                grid_kwh, community_kwh = energy, 0.0
            else:
                community_kwh = round(energy - grid_kwh, 6) if energy is not None and grid_kwh is not None else None
            rows.append((ts_from, ts_to, energy, estimated, grid_kwh, community_kwh, community_status))
    return rows


def print_last_entries(conn: sqlite3.Connection, n: int = 20) -> None:
    rows = conn.execute("""
        SELECT timestamp_from, timestamp_to, energy_kwh, grid_kwh, community_kwh, community_status
        FROM consumption
        ORDER BY timestamp_from DESC
        LIMIT ?
    """, (n,)).fetchall()

    print(f"\nLast {n} entries:")
    print(f"  {'timestamp_from':<20} {'timestamp_to':<20} {'energy':>8} {'grid':>8} {'community':>10} {'community_status'}")
    print(f"  {'-'*20} {'-'*20} {'-'*8} {'-'*8} {'-'*10} {'-'*16}")
    for r in reversed(rows):
        ts_from, ts_to, energy, grid, community, status = r
        print(f"  {ts_from:<20} {ts_to:<20} {energy or 0:>8.4f} {grid or 0:>8.4f} {community or 0:>10.4f} {status or '-'}")


def main(show_last: bool = False) -> None:
    csv_files = sorted(
        f for f in os.listdir(ARCHIVE_PATH)
        if f.endswith(".csv")
    )

    if not csv_files:
        print("No CSV files found in power_archive/.")
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    last_run = get_last_run(conn)
    updated = 0

    for filename in csv_files:
        path = os.path.join(ARCHIVE_PATH, filename)
        if os.path.getmtime(path) <= last_run:
            continue

        # filename format: power_data_YYYY-MM_<original>.csv
        month = filename[11:18]  # characters 11-17 are always YYYY-MM

        rows = load_csv(path)
        conn.execute("DELETE FROM consumption WHERE timestamp_from LIKE ?", (f"{month}%",))
        conn.executemany("""
            INSERT OR IGNORE INTO consumption
                (timestamp_from, timestamp_to, energy_kwh, is_estimated, grid_kwh, community_kwh, community_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()

        updated += 1
        print(f"  {filename[:40]:<40}  {len(rows):>4} rows")

    if updated == 0:
        print("Database is up to date — no files changed since last run.")
    else:
        total = conn.execute("SELECT COUNT(*) FROM consumption").fetchone()[0]
        print(f"\n✅ Done — {updated} file(s) imported, {total} total rows in DB")

    set_last_run(conn)
    if show_last:
        print_last_entries(conn)
    conn.close()


if __name__ == "__main__":
    main(show_last="--last" in sys.argv)
