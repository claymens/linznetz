# LINZ NETZ Power Data Archiver

![Vibe Coded](https://img.shields.io/badge/coded-by%20vibes-purple?style=for-the-badge)

> **Disclaimer:** This project was built purely for private pleasure to quickly have something working. It was not developed with any real requirements for code quality, robustness, or production readiness. Use at your own risk.

Scrapes quarter-hourly power consumption data from the LINZ NETZ portal, stores it in a SQLite database, and provides a browser-based viewer. Optionally pushes the data to Home Assistant. Intended to run daily as a cron job.

## Files

| File | Purpose |
|------|---------|
| `main.py` | Runs `get_data.py` then `db_update.py` in sequence |
| `get_data.py` | Downloads CSVs from the LINZ NETZ portal via Playwright |
| `db_update.py` | Imports CSVs from `power_archive/` into `power_data.db` |
| `viewer.html` | Browser-based viewer for the SQLite database |
| `ha_import.py` | Pushes history to Home Assistant for the Energy Dashboard (optional) |

## Setup

### 1. Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate.fish  # or activate (bash)
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
playwright install
```

### 3. Configure Credentials

Copy `.env.example` to `.env` and fill in:
```
LINZNETZ_USER=your_username
LINZNETZ_PWD=your_password
START_MONTH=2024-01
# END_MONTH defaults to the current month if omitted
# END_MONTH=2024-12
```

## Usage

### Run everything at once
```bash
python main.py        # download + update database
python main.py --ha   # download + update database + push to Home Assistant
```

Or run the steps individually:

### 1. Download data from portal
```bash
python get_data.py           # normal run
python get_data.py --debug   # also print page URL and title at each navigation step
```
- Skips months already in `power_archive/` — no login needed if everything is up to date
- Always re-downloads the current month (readings accumulate throughout the month)
- Months with no portal data get a `_NO_DATA` marker and are silently skipped on future runs

### 2. Update the database
```bash
python db_update.py           # import any files changed since last run
python db_update.py --last    # also print the last 20 entries
```
- Checks each file's modification time against the last run timestamp
- For any newer file: drops that month's rows and re-imports clean
- On first run, imports all files

### 3. View the data
Open `viewer.html` directly in a browser:
- First open: click **Open power_data.db** and locate the file — remembered for the session
- Reloads within the same browser session auto-load without prompting

Or serve via HTTP for fully automatic loading on every open:
```bash
python -m http.server 8000
# open http://localhost:8000/viewer.html
```

## Output Files

```
power_archive/power_data_YYYY-MM_<original_filename>.csv   downloaded data
power_archive/power_data_YYYY-MM_NO_DATA                   no-data marker
power_data.db                                               SQLite database
```

## Database Schema

Table: `consumption`

| Column | Type | Description |
|--------|------|-------------|
| `timestamp_from` | TEXT (PK) | Interval start (ISO 8601) |
| `timestamp_to` | TEXT | Interval end |
| `energy_kwh` | REAL | Total consumption in kWh |
| `is_estimated` | INTEGER | 1 if marked as substitute value (Ersatzwert) |
| `grid_kwh` | REAL | kWh drawn from the public grid after community offset |
| `community_kwh` | REAL | kWh supplied by the energy community (`energy_kwh − grid_kwh`) |
| `community_status` | TEXT | Community processing status (see legend below) |

### Community Status Legend

| Code | Meaning |
|------|---------|
| `M` | Measured |
| `EB` | Non-measured, trusted |
| `NV` | Value missing |
| `EN` | Non-measured, not trusted |
| `F` | Community data absent — `grid_kwh` equals `energy_kwh`, `community_kwh` is 0 |
| *(null)* | No community processing yet — `grid_kwh` equals `energy_kwh`, `community_kwh` is 0 |

## Home Assistant Integration (optional)

Two things work together to give the Energy Dashboard full historical data:

1. **SQL sensors** in `configuration.yaml` — query `power_data.db` directly in Home Assistant to register the entities and keep their current value up to date.

2. **`ha_import.py`** — triggered via `python main.py --ha`, reads `power_data.db`, and pushes the complete hourly history into HA's statistics database under those same entity IDs via the WebSocket API.

The SQL sensors must exist in HA before running the import. Re-running `ha_import.py` after a new `python main.py` is safe — existing entries are updated, new ones are appended.

### Step 1 — Add sensors to `configuration.yaml`

Add the following and **restart Home Assistant** (**Developer Tools → YAML → Restart**).

```yaml
sql:
  - name: "Linz Netz Energy"
    db_url: sqlite:////config/power_data.db
    query: "SELECT ROUND(SUM(energy_kwh), 4) AS value FROM consumption;"
    column: value
    unit_of_measurement: kWh
    device_class: energy
    state_class: total_increasing

  - name: "Linz Netz Grid"
    db_url: sqlite:////config/power_data.db
    query: "SELECT ROUND(SUM(grid_kwh), 4) AS value FROM consumption;"
    column: value
    unit_of_measurement: kWh
    device_class: energy
    state_class: total_increasing

  - name: "Linz Netz Community"
    db_url: sqlite:////config/power_data.db
    query: "SELECT ROUND(SUM(community_kwh), 4) AS value FROM consumption;"
    column: value
    unit_of_measurement: kWh
    device_class: energy
    state_class: total_increasing
```

After the restart, verify the entities appear under **Developer Tools → States** as `sensor.linz_netz_energy`, `sensor.linz_netz_grid`, `sensor.linz_netz_community`.

### Step 2 — Create a Long-Lived Access Token in HA

**Profile → Security → Long-Lived Access Tokens → Create token** — copy it, shown only once.

### Step 3 — Configure `.env`

```
HA_URL=ws://homeassistant.local:8123/api/websocket
HA_TOKEN=your_long_lived_access_token
```

### Step 4 — Run the import

```bash
python ha_import.py
```

Pushes the full hourly history (~26 000 entries per series) to HA. No restart required afterwards.

### Step 5 — Configure the Energy Dashboard

1. **Settings → Dashboards → Energy**
2. Under **Electricity grid → Grid consumption**, click **Add consumption**
3. Search for **Linz Netz Grid** and select it
4. Optionally add **Linz Netz Community** as a second source or Solar source.
5. Save — historical data appears immediately

## License

MIT — see [LICENSE](LICENSE).
