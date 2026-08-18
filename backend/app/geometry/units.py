"""
وحدة القياس الوحيدة للنظام كله: المليمتر (mm).

قاعدة صارمة: أي قيمة هندسية تُخزَّن وتُحسَب بالمليمتر كـ float.
التحويل للبكسل يحدث فقط في مرحلة rasterization الأخيرة ومرة واحدة.

لماذا هذا مهم (من التحليل الأصلي):
    4.10mm @ 300 DPI = 48.4252... px (ليس رقم صحيح)
    لو قرّبنا لـ 48px من البداية -> فقدنا 0.0864mm من كل مسافة
    عبر مئات العناصر في الشيت الواحد، هذا يتراكم لدرجة تكسر القاعدة.

قرار موثق (raster canvas rounding):
    المساحة الهندسية نادراً ما تكون رقم صحيح بكسلات بالضبط.
    1190mm @ 300 DPI = 14055.118px بالضبط — مش 14055 ومش 14056.
    قررنا نستخدم ceil (مش round) — مبدأ 'الشيت ميتقطعش أبداً' أهم من توفير
    جزء من بكسل واحد. هذا يعني الـ canvas قد يكون أكبر بجزء من بكسل واحد
    من المساحة الهندسية الدقيقة، وهذا مقبول تماماً لأنه أمان أكثر.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

MM_PER_INCH: float = 25.4


@dataclass(frozen=True, slots=True)
class Resolution:
    """دقة الـ raster بالـ DPI (نقاط لكل إنش)."""

    dpi: float

    def __post_init__(self) -> None:
        if self.dpi <= 0:
            raise ValueError(f"DPI يجب أن يكون أكبر من صفر، تم إدخال: {self.dpi}")

    @property
    def px_per_mm(self) -> float:
        return self.dpi / MM_PER_INCH


def mm_to_px(value_mm: float, resolution: Resolution) -> float:
    """تحويل مليمتر إلى بكسل بدقة كاملة (float، بدون rounding).

    هذا التابع يُستخدم داخل الحسابات الوسيطة فقط.
    للتحويل النهائي للـ raster canvas، استخدم mm_to_px_ceil.
    """
    return value_mm * resolution.px_per_mm


def px_to_mm(value_px: float, resolution: Resolution) -> float:
    """تحويل بكسل إلى مليمتر بدقة كاملة."""
    return value_px / resolution.px_per_mm


def mm_to_px_ceil(value_mm: float, resolution: Resolution) -> int:
    """تحويل مليمتر إلى عدد بكسلات صحيح بالتقريب للأعلى (ceil).

    مستخدم فقط عند تحديد أبعاد الـ raster canvas النهائية (مرة واحدة فقط،
    في آخر خطوة قبل التصدير). ceil من أجل ضمان الشيت لا يُقطع
    (أفضل نزيد 1 بكسل واحد أمان من أن ننقص ونخسر جزء من الشكل). قرار موثق.
    """
    return ceil(mm_to_px(value_mm, resolution))


def sheet_canvas_size_px(width_mm: float, height_mm: float, resolution: Resolution) -> tuple[int, int]:
    """يحسب أبعاد الـ canvas النهائية بالبكسل لشيت معين.

    مثال: 790×1190mm @ 300 DPI -> (9331, 14056) px باستخدام ceil.
    هذا الرقم هو التمثيل الرقمي الأقرب للمساحة (مع ضمان عدم القطع)،
    وليس الحقيقة الهندسية نفسها. الحقيقة الهندسية تظل دائماً:
    width_mm × height_mm @ resolution.dpi.
    """
    return (
        mm_to_px_ceil(width_mm, resolution),
        mm_to_px_ceil(height_mm, resolution),
    )
