#!/usr/bin/env python3
"""
سكريبت استكشافي: يقيس الزمن الفعلي لاستخراج الـ contours ولخطوة
run_nesting الأولى (شيت واحد فقط)، مع كتابة تقدم حي إلى ملف log
حتى يمكن متابعته من عملية منفصلة دون حجب أي أداة.
"""
import sys
import time
sys.path.insert(0, "/Volumes/alaassD/walid/backend")

from pathlib import Path
from PIL import Image
from app.geometry.contour import extract_contour_from_image
from app.geometry.units import Resolution
from app.nesting.engine import PartInput, run_nesting

LOG = Path("/Volumes/alaassD/walid/backend/probe_progress.log")


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


LOG.write_text("", encoding="utf-8")  # reset
SRC = Path("/Volumes/alaassD/untitled folder/New folder")
resolution = Resolution(dpi=300.0)

files = sorted(SRC.glob("*.png"))
log(f"START عدد الملفات: {len(files)}")

parts: dict[str, PartInput] = {}
failed = []
t0 = time.perf_counter()
for i, f in enumerate(files):
    try:
        with Image.open(f) as im:
            im.load()
            extracted = extract_contour_from_image(im, resolution)
        pid = str(i)
        parts[pid] = PartInput(
            shape_mm=extracted.polygon_mm,
            source_image_path=str(f),
            source_centroid_px=extracted.source_centroid_px,
            alpha_bbox_px=extracted.alpha_bbox_px,
        )
    except Exception as e:
        failed.append((f.name, repr(e)))
    if (i + 1) % 25 == 0:
        log(f"CONTOUR_PROGRESS {i+1}/{len(files)}")

log(f"CONTOURS_DONE نجح={len(parts)} فشل={len(failed)} زمن={time.perf_counter()-t0:.1f}s")
if failed:
    for name, err in failed[:10]:
        log(f"  FAILED: {name}: {err}")

def progress_cb(done, part_total, placed_count):
    log(f"NESTING_PROGRESS done={done}/{part_total} placed={placed_count}")

log("NESTING_START شيت واحد فقط (استكشافي، بدون LNS)")
t1 = time.perf_counter()
result = run_nesting(
    parts,
    sheet_width_mm=790.0,
    sheet_height_mm=1190.0,
    sheet_margin_mm=5.0,
    clearance_mm=4.10,
    on_progress=progress_cb,
)
elapsed = time.perf_counter() - t1

log(f"NESTING_DONE زمن={elapsed:.1f}s موضوعة={len(result.placed)} غير_موضوعة={len(result.unplaced_part_ids)} sheet_full={result.sheet_full}")
log("FINISHED")
