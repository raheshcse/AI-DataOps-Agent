"""Idempotently import SQL Server simulator incidents into the workflow store through the authenticated API.

    $env:IMPORT_API_TOKEN = "<operator token>"      # never put the token on the command line
    python scripts/import_simulator_incidents.py --yes

* Uses POST /api/v1/simulator/incidents/{id}/import, which needs `incident:create` and writes ONLY to the operations store.
* Safe to repeat: an already-imported incident is reported, not duplicated. Nothing is written to SQL Server.
* An import creates a `new` workflow incident; it records no investigation, approval or governance event.
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.environ.get("DASHBOARD_API_BASE_URL", "http://127.0.0.1:8000/api/v1").rstrip("/")


def main(argv: list[str]) -> int:
    token = os.environ.get("IMPORT_API_TOKEN")
    if not token:
        print("Set IMPORT_API_TOKEN (an operator token) in the environment.")
        return 2
    if "--yes" not in argv:
        print("Dry run. Re-run with --yes to import.")
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=15.0, headers=headers, follow_redirects=False) as c:
        r = c.get(f"{BASE}/simulator/incidents", params={"limit": 200})
        if r.status_code != 200:
            print(f"Could not list simulator incidents (HTTP {r.status_code}).")
            return 1
        items = r.json()["items"]
        print(f"{len(items)} simulator incident(s); {sum(1 for i in items if i['imported'])} already imported.")
        if "--yes" not in argv:
            return 0
        for i in items:
            res = c.post(f"{BASE}/simulator/incidents/{i['incident_id']}/import")
            if res.status_code in (200, 201):
                print(f"  {i['incident_id']}: {'created' if res.json()['created'] else 'already imported'} -> {res.json()['incident']['incident_id']}")
            else:
                print(f"  {i['incident_id']}: refused (HTTP {res.status_code})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
