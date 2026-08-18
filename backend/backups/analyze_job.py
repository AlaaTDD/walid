import os
import json
from pathlib import Path
from shapely.wkt import loads

def analyze_job():
    jobs_root = Path("/Users/alaataha/.gemini/antigravity-ide/backend/app").parent / "jobs"
    if not jobs_root.exists():
        print("jobs_root not found")
        return
    for job_file in jobs_root.glob("*.json"):
        payload = json.loads(job_file.read_text())
        parts = payload.get("parts", [])
        if len(parts) > 10:
            print(f"Job {job_file.name}: {len(parts)} parts")
            areas = set()
            wkbs = set()
            for p in parts:
                geom = loads(p["contour_wkt"])
                areas.add(round(geom.area, 2))
                wkbs.add(geom.wkb)
            print(f"Unique areas: {len(areas)}")
            print(f"Unique wkbs: {len(wkbs)}")

analyze_job()
