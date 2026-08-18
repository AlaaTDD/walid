"""
تطبيق مسافة الأمان (clearance) كـ geometric offset حقيقي على الـ contour.

المبدأ: نوسّع حدود الشكل الحقيقية للخارج بمقدار CLEARANCE_MM،
ونمنع أي شكل آخر من الدخول داخل هذه المنطقة الممتدة.
هذا يُسمّى في التحليل الأصلي: Morphological Dilation / Offset Geometry.
"""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry

# قيمة ثابتة مطلوبة من المستخدم. مخزّنة كـ mm فقط — مفيش نسخة بالبكسل.
CLEARANCE_MM: float = 4.10

# دقة الـ offset الدائري (join_style=1 في Shapely = round joins).
# quad_segs أعلى = تقريب أدق للزوايا المنحنية، مهم للحفاظ على دقة المسافة الهندسية
OFFSET_QUAD_SEGS: int = 32


def apply_clearance(shape_mm: BaseGeometry, clearance_mm: float = CLEARANCE_MM) -> BaseGeometry:
    """يوسّع حدود الشكل للخارج بمقدار clearance_mm، منتجاً clearance zone.

    أي شكل آخر يجب ألا يدخل داخل الناتج الموسّع هنا.

    Args:
        shape_mm: الشكل الأصلي (من extract_contour_from_rgba)، بالمليمتر.
        clearance_mm: مسافة التوسيع بالمليمتر (افتراضي: 4.10mm).

    Returns:
        المنطقة الممنوعة (forbidden zone) التي يجب ألا يدخلها أي شكل آخر.
    """
    if clearance_mm <= 0:
        raise ValueError(f"clearance_mm يجب أن يكون أكبر من صفر، تم إدخال: {clearance_mm}")

    return shape_mm.buffer(
        clearance_mm,
        quad_segs=OFFSET_QUAD_SEGS,
        join_style="round",
    )


def minimum_distance_mm(shape_a_mm: BaseGeometry, shape_b_mm: BaseGeometry) -> float:
    """يحسب أقل مسافة هندسية بين شكلين (بالمليمتر).

    هذا هو التابع المستخدم في collision validation المستقل (مرحلة 13 في التحليل):
    يجب أن تكون النتيجة >= CLEARANCE_MM لكل زوج أشكال، وإلا يُرفض الـ layout.
    """
    return float(shape_a_mm.distance(shape_b_mm))
