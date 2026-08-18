#!/usr/bin/env python3
"""
فحص سريع: تشغيل run_multi_sheet_nesting على كل الـ151 صورة الحقيقية
بالمقاسات الفعلية (790x1190/margin5/clearance4.10) لمعرفة كم صورة
تدخل فعلياً في الشيت الأول قبل الالتزام بالتنفيذ الكامل (الذي قد يأخذ دقائق).
هذا اختبار استكشافي بعدد محاولات=1 (سريع) فقط لمعرفة الاتجاه العام.
"""
import sys
import time
sys.path.insert(0, "/Volumes/alaassD/walid/backend")

from pathlib import Path
from PIL import Image
from app.geometry.contour import extract_contour_from_image
from app.geometry.units import Resolution
from app.nesting.engine import PartInput, run_multi_sheet_nesting

SRC = Path("/Volumes/alaassD/untitled folder/New folder")
resolution = Resolution(dpi=300.0)

files = sorted(SRC.glob("*.png"))
print(f"عدد الملفات: {len(files)}")

parts = {}
failed = []
t0 = time.perf_counter()
for i, f in enumerate(files):
    try:
        with Image.open(f) as im:
            im.load()
            rgba = im.convert("RGBA")
            extracted = extract_contour_from_image(rgba, resolution)
        parts[str(i)] = PartInput(
            shape_mm=extracted.shape_mm,
            source_image_path=str(f),
        )
    except Exception as e:
        failed.append((f.name, str(e)))

print(f"تم استخراج contour بنجاح لـ {len(parts)} صورة من أصل {len(files)}")
if failed:
    print(f"فشل استخراج {len(failed)} صورة:")
    for name, err in failed[:10]:
        print(f"  - {name}: {err}")
print(f"زمن استخراج الـ contours: {time.perf_counter()-t0:.1f}s")

print("\nبدء تشغيل run_multi_sheet_nesting (packing_attempts=1 للسرعة الاستكشافية)...")
t1 = time.perf_counter()
result = run_multi_sheet_nesting(
    parts,
    sheet_width_mm=790.0,
    sheet_height_mm=1190.0,
    sheet_margin_mm=5.0,
    clearance_mm=4.10,
    packing_attempts=1,
)
elapsed = time.perf_counter() - t1

print(f"\n✅ انتهى التشغيل الاستكشافي في {elapsed:.1f} ثانية")
print(f"عدد الشيتات الناتجة: {len(result.sheets)}")
for idx, sheet in enumerate(result.sheets, start=1):
    print(f"  شيت {idx}: {len(sheet.placed)} صورة")
print(f"صور لم توضع في أي شيت: {len(result.unplaced_part_ids)}")
print(f"الإجمالي: {result.total_count}")
