import os
import re
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Load .env file if present
load_dotenv()

DEBUG = "--debug" in sys.argv

OK   = "✅ "
FAIL = "❌ "
WARN = "⚠️  "

# --- CONFIGURATION ---
USER = os.getenv("LINZNETZ_USER")
PWD = os.getenv("LINZNETZ_PWD")
if not USER or not PWD or USER == "YOUR_USERNAME" or PWD == "YOUR_PASSWORD":
    raise SystemExit(
        "LINZNETZ_USER and LINZNETZ_PWD must be set in .env — "
        "edit .env (see .env.example) and re-run."
    )

DOWNLOAD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "power_archive")
URL = "https://services.linznetz.at/verbrauchsdateninformation/consumption.jsf"
TIMEOUT      = 10_000   # general UI interactions
TIMEOUT_AJAX = 15_000   # date-picker AJAX redraws
TIMEOUT_CSV  = 20_000   # CSV button appearance after data load
TIMEOUT_NAV  = 30_000   # initial page navigation

# NO_DATA markers are excluded from CSV-downloaded checks so they never block retries.
# Markers older than this many days are automatically deleted for a retry.
NO_DATA_RETRY_DAYS = 30

# Month range configuration (YYYY-MM format)
START_MONTH = os.getenv("START_MONTH", "2024-01")
END_MONTH = os.getenv("END_MONTH") or datetime.now().strftime("%Y-%m")
DATE_FORMAT = "%d.%m.%Y"  # Format expected by the web form

Path(DOWNLOAD_PATH).mkdir(exist_ok=True)

def generate_monthly_ranges(start_month_str, end_month_str):
    start = datetime.strptime(start_month_str, "%Y-%m")
    end = datetime.strptime(end_month_str, "%Y-%m")

    ranges = []
    current = start
    while current <= end:
        month_end = current + relativedelta(months=1) - timedelta(days=1)
        ranges.append({
            "from_date": current.strftime(DATE_FORMAT),
            "to_date": month_end.strftime(DATE_FORMAT),
            "month": current.strftime("%Y-%m")
        })
        current += relativedelta(months=1)
    return ranges

def debug_page(page, step_name):
    """Debug helper: Print page URL and title"""
    print(f"  [{step_name}] URL: {page.url}")
    print(f"  [{step_name}] Title: {page.title()}")


def _no_data_marker_path(month):
    return os.path.join(DOWNLOAD_PATH, f"power_data_{month}_NO_DATA")


def _csv_files_for_month(entries, month):
    """Return CSV entries for a given month (excludes NO_DATA markers)."""
    prefix = f"power_data_{month}_"
    return [f for f in entries if f.startswith(prefix) and f.endswith(".csv") and not f.endswith("_NO_DATA")]


def _has_csv_for_month(entries, month):
    return bool(_csv_files_for_month(entries, month))


def _create_no_data_marker(month):
    marker_path = _no_data_marker_path(month)
    Path(marker_path).touch()
    print(f"    {WARN}Marked {month} as no data — will be retried after {NO_DATA_RETRY_DAYS} days")


def _remove_no_data_marker(month):
    marker_path = _no_data_marker_path(month)
    if os.path.exists(marker_path):
        os.remove(marker_path)


def _stale_no_data_markers(entries):
    """Return NO_DATA marker filenames older than NO_DATA_RETRY_DAYS."""
    now = datetime.now().timestamp()
    threshold = now - NO_DATA_RETRY_DAYS * 86400
    return [
        f for f in entries
        if f.endswith("_NO_DATA")
        and os.path.getmtime(os.path.join(DOWNLOAD_PATH, f)) < threshold
    ]


def run_archiver():

    monthly_ranges = generate_monthly_ranges(START_MONTH, END_MONTH)

    # Snapshot directory once — all subsequent checks use this snapshot
    all_entries = os.listdir(DOWNLOAD_PATH)

    # Re-fetch the current month only if its CSV wasn't downloaded today
    current_month = datetime.now().strftime("%Y-%m")
    today = datetime.now().date()
    current_month_csvs = _csv_files_for_month(all_entries, current_month)
    current_month_fresh = any(
        os.path.getsize(os.path.join(DOWNLOAD_PATH, f)) > 0
        and datetime.fromtimestamp(os.path.getmtime(os.path.join(DOWNLOAD_PATH, f))).date() >= today
        for f in current_month_csvs
        if os.path.exists(os.path.join(DOWNLOAD_PATH, f))  # Safe check: file might be deleted between list and stat
    )
    if not current_month_fresh:
        for f in current_month_csvs:
            filepath = os.path.join(DOWNLOAD_PATH, f)
            if os.path.exists(filepath):  # Safe check
                os.remove(filepath)
        marker = _no_data_marker_path(current_month)
        if os.path.exists(marker):
            os.remove(marker)
        all_entries = os.listdir(DOWNLOAD_PATH)  # refresh snapshot

    # Pending months: months without CSV files (NO_DATA markers do NOT count as CSVs)
    pending = [r for r in monthly_ranges if not _has_csv_for_month(all_entries, r['month'])]

    # Retry stale NO_DATA markers (older than NO_DATA_RETRY_DAYS)
    stale_markers = _stale_no_data_markers(all_entries)
    for marker in stale_markers:
        marker_path = os.path.join(DOWNLOAD_PATH, marker)
        if os.path.exists(marker_path):  # Safe check
            os.remove(marker_path)
            all_entries = os.listdir(DOWNLOAD_PATH)  # refresh snapshot
            month = marker[len("power_data_"):-len("_NO_DATA")]
            matching = [r for r in monthly_ranges if r['month'] == month]
            if matching and matching[0] not in pending:
                pending.append(matching[0])
                print(f"  Retrying previously no-data month {month} (marker expired)\n")

    if not pending:
        csv_files = [f for f in os.listdir(DOWNLOAD_PATH) if f.endswith(".csv")]
        archive_size = sum(
            os.path.getsize(os.path.join(DOWNLOAD_PATH, f)) for f in csv_files
        ) / (1024 * 1024)
        print(f"{OK}All {len(monthly_ranges)} month(s) are up to date.")
        print(f"   {len(monthly_ranges)} months · {len(csv_files)} CSV files · {archive_size:.1f} MB on disk")
        print(f"   Last file: {sorted(csv_files)[-1][:35]}…\n")
        return

    already = len(monthly_ranges) - len(pending)
    if already:
        print(f"Skipping {already} already-downloaded month(s), downloading {len(pending)} remaining.\n")

    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            print("[1/5] Navigating to portal...")
            page.goto(URL, wait_until="networkidle", timeout=TIMEOUT_NAV)
            if DEBUG: debug_page(page, "After Initial Load")

            # Login process
            print("[2/5] Performing login...")
            
            try:
                # Try to find input fields
                username_field = page.locator("input[type='text']").first
                password_field = page.locator("input[type='password']")
                
                if username_field.is_visible() and password_field.is_visible():
                    username_field.fill(USER)
                    password_field.fill(PWD)
                    print(f"  {OK}Login fields filled")
                    
                    # Search for submit button
                    submit_button = page.locator("button[type='submit'], input[type='submit']").first
                    if submit_button.is_visible():
                        submit_button.click()
                        print(f"  {OK}Submit button clicked")
                        page.wait_for_load_state("networkidle", timeout=TIMEOUT)
                        if DEBUG: debug_page(page, "After Login")
                    else:
                        print(f"  {FAIL}Submit button not found")
                        return
                else:
                    print(f"  {FAIL}Login fields not visible")
                    return
            except Exception as e:
                print(f"  {FAIL}Login error: {e}")
                return

            # After successful login: verify the data form is present
            print("[3/5] Verifying post-login state...")
            try:
                page.locator('input[id="myForm1:calendarFromRegion"]').wait_for(state="attached", timeout=TIMEOUT)
                print(f"  {OK}Data form loaded — login confirmed")
            except Exception as e:
                print(f"  {FAIL}Data form not found after login: {e}")
                return

            # Dismiss cookie consent dialog if present
            try:
                cookie_btn = page.locator("button", has_text="essentielle Cookies").first
                if cookie_btn.is_visible(timeout=3000):
                    cookie_btn.click()
                    print(f"  {OK}Dismissed cookie consent")
            except Exception:
                pass
            
            # Select "Viertelstundenwerte" once before the loop.
            # Its AJAX handler (u:"myForm1") redraws the whole form, resetting date fields —
            # so must be done before filling dates.
            # NOTE: avoid selecting by j_idt* — those IDs are dynamic JSF tokens that differ
            # across Chromium versions and page renders, causing silent fallback to daily data.
            print("[4/5] Selecting Viertelstundenwerte granularity...")
            try:
                viertel_label = page.get_by_text("Viertelstundenwerte", exact=True).first
                viertel_label.wait_for(state="visible", timeout=TIMEOUT)
                # Find the associated radio input via the label's `for` attribute
                radio_id = page.evaluate(
                    'Array.from(document.querySelectorAll("label")).find(l => l.textContent.trim() === "Viertelstundenwerte")?.htmlFor'
                )
                is_checked = page.evaluate(
                    f'document.getElementById("{radio_id}")?.checked'
                ) if radio_id else False
                if not is_checked:
                    viertel_label.click()
                    page.wait_for_load_state("networkidle", timeout=TIMEOUT)
                # Verify the radio is now actually checked
                is_checked_after = page.evaluate(
                    f'document.getElementById("{radio_id}")?.checked'
                ) if radio_id else None
                if not is_checked_after:
                    raise RuntimeError(
                        f"Viertelstundenwerte radio (id={radio_id!r}) still not checked after click — "
                        "portal would return daily data; aborting to avoid wrong granularity."
                    )
                print(f"  {OK}Selected 'Viertelstundenwerte'")
            except Exception as e:
                print(f"  {FAIL}Viertelstundenwerte error: {e}")
                return

            # Prime the server-side date bean with one Anzeigen click using the form's
            # current (default) dates. Without this, the first real iteration always sends
            # wrong dates because the bean is uninitialized after the Viertelstundenwerte
            # form redraw, causing changeToDate AJAX to overwrite our date inputs.
            print("  Priming server state...")
            try:
                page.evaluate("""
                    PrimeFaces.ab({
                        s: document.querySelector('input[id="myForm1:btnIdA1"]'),
                        e: "action",
                        f: "myForm1",
                        p: "myForm1:btnIdA1",
                        u: "myForm1:list",
                        onco: function(xhr, status, args, data) { netzLoaderOverlayDeactivate(); }
                    });
                """)
                page.wait_for_load_state("networkidle", timeout=TIMEOUT)
                print(f"  {OK}Server state primed")
            except Exception as e:
                print(f"  {WARN}Priming error (continuing): {e}")

            # Download CSV for each pending month
            print(f"[5/5] Downloading {len(pending)} month(s)...\n")

            for i, date_range in enumerate(pending):
                month_str = date_range['month']
                from_date = date_range['from_date']
                to_date = date_range['to_date']

                print(f"  [{i+1}/{len(pending)}] Processing {month_str}...")
                print(f"    Date range: {from_date} to {to_date}")

                try:
                    # Step 1: Trigger "von" DateTimePicker to fire changeFromDate AJAX,
                    # which reinitializes the "bis" calendar on the server side.
                    page.evaluate(f"""
                        $('[id="myForm1:calendarFromRegion"]').data("DateTimePicker").date(moment('{from_date}', 'DD.MM.YYYY'));
                    """)
                    page.wait_for_load_state("networkidle", timeout=TIMEOUT_AJAX)

                    # Step 2: Trigger "bis" DateTimePicker similarly and wait for its AJAX.
                    page.evaluate(f"""
                        $('[id="myForm1:calendarToRegion"]').data("DateTimePicker").date(moment('{to_date}', 'DD.MM.YYYY'));
                    """)
                    page.wait_for_load_state("networkidle", timeout=TIMEOUT_AJAX)

                    # Step 3: Force-set both input values and fire PrimeFaces.ab in a single
                    # synchronous JS block so no async AJAX can overwrite them between the two.
                    anzeigen_button = page.locator('input[id="myForm1:btnIdA1"]')
                    if anzeigen_button.is_visible(timeout=TIMEOUT):
                        page.evaluate(f"""
                            document.querySelector('[id="myForm1:calendarFromRegion"]').value = '{from_date}';
                            document.querySelector('[id="myForm1:calendarToRegion"]').value = '{to_date}';
                            PrimeFaces.ab({{
                                s: document.querySelector('input[id="myForm1:btnIdA1"]'),
                                e: "action",
                                f: "myForm1",
                                p: "myForm1:calendarFromRegion myForm1:calendarToRegion myForm1:btnIdA1",
                                u: "myForm1:list",
                                onco: function(xhr, status, args, data) {{ netzLoaderOverlayDeactivate(); }}
                            }});
                        """)
                        print(f"    {OK}Set {from_date} → {to_date}, triggered 'Anzeigen'")
                        page.wait_for_load_state("networkidle", timeout=TIMEOUT)
                        page.get_by_text("CSV", exact=False).wait_for(state="visible", timeout=TIMEOUT_CSV)
                        print(f"    {OK}CSV button appeared")
                    else:
                        print(f"    {FAIL}'Anzeigen' button not found")
                        continue

                    # Export CSV — matched by text
                    export_button = page.get_by_text("CSV", exact=False).first

                    if export_button.is_visible(timeout=TIMEOUT):
                        print(f"    {OK}Found CSV export button")
                        with page.expect_download(timeout=TIMEOUT) as download_info:
                            export_button.click()
                        download = download_info.value
                        original_filename = download.suggested_filename
                        # Filename with a single date (no range) means the portal
                        # ignored our date range — data likely not available for that period
                        if not re.search(r'\d{8}_\d{8}', original_filename):
                            print(f"    {WARN}No data for {month_str} (portal returned: {original_filename})\n")
                            _create_no_data_marker(month_str)
                            continue
                        new_filename = f"power_data_{month_str}_{original_filename}"
                        save_path = os.path.join(DOWNLOAD_PATH, new_filename)
                        download.save_as(save_path)
                        # Verify download is non-empty
                        if os.path.getsize(save_path) == 0:
                            os.remove(save_path)
                            print(f"    {WARN}Downloaded file is empty for {month_str}, removed\n")
                            _create_no_data_marker(month_str)
                            continue
                        _remove_no_data_marker(month_str)
                        print(f"    {OK}Downloaded: {new_filename}\n")
                    else:
                        print(f"    {WARN}CSV button not visible for {month_str}\n")
                        _create_no_data_marker(month_str)

                except Exception as e:
                    print(f"    {FAIL}Error processing month {month_str}: {e}")
                    continue
            
            print(f"\n{OK}Download complete!\n")

        except Exception as e:
            print(f"\n{FAIL}Error: {e}")
            traceback.print_exc()
        finally:
            browser.close()
            print(f"{OK}Browser closed.\n")

if __name__ == "__main__":
    print("\n=== LINZ NETZ Power Data Archiver ===\n")
    run_archiver()