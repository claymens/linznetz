from get_data import run_archiver, OK
from db_update import main as update_db

print("━" * 50)
print("  Step 1/2 — Downloading data from portal")
print("━" * 50)
run_archiver()

print("━" * 50)
print("  Step 2/2 — Updating database")
print("━" * 50)
update_db()

print(f"{OK}All done.\n")
