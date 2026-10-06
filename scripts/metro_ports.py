"""Emit the Metro ports status.sh should check, split by whether anything starts them.

status.sh used to hardcode 8081-8084. 8083 (Business staging) is declared in
project-environments.json but has no project row, and the Metro watchdog starts one
packager per project row keyed on app_bundle_id -- so nothing ever starts 8083 and the
dashboard reported a permanent red line for a service no component owns.

OWNED   = a port some project row will cause the watchdog to start.
UNCLAIMED = declared in the config but no project row maps to it.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.projects.builder import app_builder
from automation.database.config import SessionLocal
from automation.database.models import TestProject

declared = {app_builder.METRO_PORT}
declared |= set(app_builder._APP_METRO_PORTS.values())

owned = set()
with SessionLocal() as db:
    for p in db.query(TestProject).all():
        if p.app_bundle_id:
            owned.add(app_builder.metro_port_for(p.app_bundle_id))

print(f"METRO_OWNED='{' '.join(str(x) for x in sorted(owned))}'")
print(f"METRO_UNCLAIMED='{' '.join(str(x) for x in sorted(declared - owned))}'")
