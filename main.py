import sys
from get_data import run_archiver, OK
from db_update import main as update_db

ha = "--ha" in sys.argv
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
    asyncio.run(ha_import())

print(f"{OK}All done.\n")
