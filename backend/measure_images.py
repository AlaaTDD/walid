#!/usr/bin/env python3
"""قياس دقيق لأبعاد كل صورة PNG بالمليمتر على أساس 300 DPI."""
import json
from pathlib import Path
from PIL import Image

SRC = Path("/Volumes/alaassD/untitled folder/New folder")
DPI = 300.0

results = []
total_area_mm2 = 0.0

files = sorted(SRC.glob("*.png"))
print(f"عدد ملفات PNG: {len(files)}")

for f in files:
    with Image.open(f) as im:
        w_px, h_px = im.size
        w_mm = round(w_px / DPI * 25.4, 2)
        h_mm = round(h_px / DPI * 25.4, 2)
        area = round(w_mm * h_mm, 2)
        results.append({
            "file": f.name,
            "w_px": w_px, "h_px": h_px,
            "w_mm": w_mm, "h_mm": h_mm,
            "area_mm2": area,
        })
        total_area_mm2 += area

results.sort(key=lambda r: -r["area_mm2"])

# إحصائيات
areas = [r["area_mm2"] for r in results]
widths = [r["w_mm"] for r in results]
heights = [r["h_mm"] for r in results]

sheet_usable_w = 790 - 2*5
sheet_usable_h = 1190 - 2*5
sheet_usable_area = sheet_usable_w * sheet_usable_h

print(f"\n=== إحصائيات المساحة (مليمتر مربع) ===")
print(f"أصغر صورة: {min(areas):.1f} mm²")
print(f"أكبر صورة: {max(areas):.1f} mm²")
print(f"المتوسط: {sum(areas)/len(areas):.1f} mm²")
print(f"الوسيط: {sorted(areas)[len(areas)//2]:.1f} mm²")
print(f"إجمالي مساحة كل الصور: {total_area_mm2:.0f} mm²")

print(f"\n=== إحصائيات الأبعاد (مليمتر) ===")
print(f"أصغر عرض: {min(widths):.1f}mm  أكبر عرض: {max(widths):.1f}mm")
print(f"أصغر ارتفاع: {min(heights):.1f}mm  أكبر ارتفاع: {max(heights):.1f}mm")

print(f"\n=== مساحة الشيت القابلة للاستخدام ===")
print(f"780 x 1180 = {sheet_usable_area:.0f} mm²")
print(f"عدد الشيتات نظرياً لو المساحة بس (بدون احتساب فراغات النستنج): {total_area_mm2/sheet_usable_area:.2f}")
print(f"متوسط عدد صور نظري في شيت واحد (مساحة فقط): {sheet_usable_area/(sum(areas)/len(areas)):.1f}")

# توزيع الأحجام على فئات
buckets = {"صغير <3000mm²": 0, "متوسط 3000-8000mm²": 0, "كبير 8000-15000mm²": 0, "كبير جداً >15000mm²": 0}
for a in areas:
    if a < 3000: buckets["صغير <3000mm²"] += 1
    elif a < 8000: buckets["متوسط 3000-8000mm²"] += 1
    elif a < 15000: buckets["كبير 8000-15000mm²"] += 1
    else: buckets["كبير جداً >15000mm²"] += 1

print(f"\n=== توزيع الأحجام ===")
for k, v in buckets.items():
    print(f"{k}: {v} صورة")

print(f"\n=== أكبر 10 صور ===")
for r in results[:10]:
    print(f"  {r['file'][:50]:50s} {r['w_mm']:6.1f} x {r['h_mm']:6.1f} mm ({r['area_mm2']:.0f} mm²)")

print(f"\n=== أصغر 10 صور ===")
for r in results[-10:]:
    print(f"  {r['file'][:50]:50s} {r['w_mm']:6.1f} x {r['h_mm']:6.1f} mm ({r['area_mm2']:.0f} mm²)")

# حفظ التقرير الكامل كـ JSON
out_path = Path("/Volumes/alaassD/walid/backend/jobs/_image_measurements.json")
out_path.parent.mkdir(parents=True, exist_ok=True)
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({
        "count": len(results),
        "total_area_mm2": total_area_mm2,
        "sheet_usable_area_mm2": sheet_usable_area,
        "images": results,
    }, fh, ensure_ascii=False, indent=2)
print(f"\n✅ تم حفظ التقرير الكامل في: {out_path}")
