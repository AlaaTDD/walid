"""
تدوير الشكل الحقيقي (الـ contour) لإحدى الزوايا المُقفلة فقط (integer degrees).

قرار معماري مُحدّث (يوسّع بند 4 في docs/architecture.md دون كسره):
    الدوران محدود لمضاعفات صحيحة من 15° فقط (0, 15, 30, ..., 345 — أي 24
    زاوية بالمجموع). لا يوجد دوران حر (float) بأي زاوية عشوائية.

السبب: القيمة نفسها لازم تفضل int عشان تتوافق 100% مع باقي السيستم
(LockedRotation هو IntEnum، ومُخزّن كـ rotation_deg: int في job_storage.py
وschemas.py). أي زاوية غير 0/90/180/270 تحتاج فعلياً sin/cos داخلياً في
shapely.affinity.rotate، وهذا يُدخل floating-point error دقيق جداً (أصغر
بكثير من 0.001mm) في حساب مسافة الأمان (clearance) — تنازل صريح ومقصود
تمت الموافقة عليه لتقليل الفراغات الناتجة عن قفل الزوايا الأربعة الأصلية،
وليس خطأً أو تراجعاً عن الدقة الهندسية.

التحقق: عند 0/90/180/270 فقط تكون قيم sin/cos بالضبط {-1, 0, 1} (بدون أي
تقريب حقيقي). الزوايا الـ 20 الإضافية (15, 30, 45, ...) تستخدم نفس دالة
shapely.affinity.rotate القياسية، وبالتالي بنفس درجة الدقة العملية التي
تستخدمها أي مكتبة nesting احترافية أخرى — دقة كافية جداً لأي تطبيق طباعة
أو قص فعلي، لكنها ليست "صفر خطأ رياضي مطلق" كالحالة الأربعة الأصلية.

---

إضافة لاحقة (فقرة من المتطلبات رقم 7 — Fine Rotation Refinement):

طبقًا للمتطلب: "عندما يتم العثور على placement جيد، قم بتحسين زاوية الدوران حول
هذه المنطقة بدل الاعتماد فقط على grid ثابت" — مع "trade-off واضح بين الدقة
ووقت الحساب", وبدون "دوران حر (float) بأي زاوية عشوائية" (القيد المذكور
أعلاه).

FINE_ROTATIONS هي مجموعة أعضاء جدُد في نفس الـ LockedRotation IntEnum (ليست nested
أو float منفصلة) بزوايا صحيحة بين مضاعفات 15° الأصلية (مثلاً: 3, 6, 9, 12,
18, 21, ...) — لا تزال int مثل الزوايا الأصلية بالضبط، وبالتالي تمر بنفس التحقق
في rotate_shape وتُخزّن بنفس طريقة rotation_deg:int الموجودة بالفعل في
job_storage.py/schemas.py دون أي تغيير هناك.

CRITICAL: ALL_ROTATIONS (المستخدم في _prepare_rotations للبحث الرئيسي لكل part)
لا يزال يعيد tuple(LockedRotation) — أي الـ 24 زاوية الأصلية فقط، بنفس العدد
ونفس الترتيب كما كانت قبل إضافة FINE_ROTATIONS. هذا مقصود: لو ALL_ROTATIONS ضمت
الزوايا الدقيقة تلقائياً (لأن tuple(LockedRotation) يشمل كل أعضاء الـ enum)، لأصبح
_prepare_rotations يجرّب كل rotation لكل part في البحث الرئيسي بدل 24 فقط، وهذا
بالضبط الـ "brute-force بلا داعٍ" الممنوع صراحةً في بند 7 من prompet.md، وكان
سيكسر كل اختبار موجود يعتمد على عدد/محتوى 24 زاوية بالضبط (مثل
 test_nesting_capacity.py). لذلك ALL_ROTATIONS أصبحت مبنية صراحةً من القائمة الثابتة
_COARSE_ROTATION_VALUES بدل tuple(LockedRotation)، وFINE_ROTATIONS منفصلة تماماً ولا
تُستخدم إلا صراحةً من مرحلة التحسين (refinement) بعد إيجاد placement جيد بالفعل.
"""

from __future__ import annotations

from enum import IntEnum

from shapely.affinity import rotate
from shapely.geometry.base import BaseGeometry


class LockedRotation(IntEnum):
    """كل الزوايا المسموحة (الزوايا الأصلية 24 بمضاعفات 15°، بالإضافة إلى زوايا
    التحسين الدقيق الإضافية المدرجة تحت "FINE_" أدناه — كلها int degrees صحيحة،
    لا يوجد أي float في أي موضع). القيمة = درجة الدوران (counter-clockwise)."""

    DEG_0 = 0
    DEG_15 = 15
    DEG_30 = 30
    DEG_45 = 45
    DEG_60 = 60
    DEG_75 = 75
    DEG_90 = 90
    DEG_105 = 105
    DEG_120 = 120
    DEG_135 = 135
    DEG_150 = 150
    DEG_165 = 165
    DEG_180 = 180
    DEG_195 = 195
    DEG_210 = 210
    DEG_225 = 225
    DEG_240 = 240
    DEG_255 = 255
    DEG_270 = 270
    DEG_285 = 285
    DEG_300 = 300
    DEG_315 = 315
    DEG_330 = 330
    DEG_345 = 345

    # Fine-refinement-only additions (spec section 7). Every coarse 15°
    # multiple above gets four finer neighbours at ±3° and ±6°, still
    # integer degrees, still real LockedRotation/IntEnum members -- these are
    # NEVER included in ALL_ROTATIONS (the main per-part search stays exactly
    # the original 24, unchanged cost, unchanged existing test outcomes) and
    # are only ever consulted by fine_neighbors_of() below, which a
    # refinement stage calls selectively after a coarse placement is already
    # good, not on every candidate. ±3°/±6° (not a denser grid) is a
    # deliberately small, bounded neighbourhood: spec section 7 explicitly
    # warns against unnecessary brute-force, and doubling or tripling the
    # rotation count for every refinement attempt would reintroduce exactly
    # that. A part whose true optimal angle sits further than 6° from the
    # nearest coarse multiple is a rare edge case better served by widening
    # this constant later (with fresh benchmark evidence) than by making
    # every refinement call expensive by default.
    FINE_DEG_3 = 3
    FINE_DEG_6 = 6
    FINE_DEG_9 = 9
    FINE_DEG_12 = 12
    FINE_DEG_18 = 18
    FINE_DEG_21 = 21
    FINE_DEG_24 = 24
    FINE_DEG_27 = 27
    FINE_DEG_33 = 33
    FINE_DEG_36 = 36
    FINE_DEG_39 = 39
    FINE_DEG_42 = 42
    FINE_DEG_48 = 48
    FINE_DEG_51 = 51
    FINE_DEG_54 = 54
    FINE_DEG_57 = 57
    FINE_DEG_63 = 63
    FINE_DEG_66 = 66
    FINE_DEG_69 = 69
    FINE_DEG_72 = 72
    FINE_DEG_78 = 78
    FINE_DEG_81 = 81
    FINE_DEG_84 = 84
    FINE_DEG_87 = 87
    FINE_DEG_93 = 93
    FINE_DEG_96 = 96
    FINE_DEG_99 = 99
    FINE_DEG_102 = 102
    FINE_DEG_108 = 108
    FINE_DEG_111 = 111
    FINE_DEG_114 = 114
    FINE_DEG_117 = 117
    FINE_DEG_123 = 123
    FINE_DEG_126 = 126
    FINE_DEG_129 = 129
    FINE_DEG_132 = 132
    FINE_DEG_138 = 138
    FINE_DEG_141 = 141
    FINE_DEG_144 = 144
    FINE_DEG_147 = 147
    FINE_DEG_153 = 153
    FINE_DEG_156 = 156
    FINE_DEG_159 = 159
    FINE_DEG_162 = 162
    FINE_DEG_168 = 168
    FINE_DEG_171 = 171
    FINE_DEG_174 = 174
    FINE_DEG_177 = 177
    FINE_DEG_183 = 183
    FINE_DEG_186 = 186
    FINE_DEG_189 = 189
    FINE_DEG_192 = 192
    FINE_DEG_198 = 198
    FINE_DEG_201 = 201
    FINE_DEG_204 = 204
    FINE_DEG_207 = 207
    FINE_DEG_213 = 213
    FINE_DEG_216 = 216
    FINE_DEG_219 = 219
    FINE_DEG_222 = 222
    FINE_DEG_228 = 228
    FINE_DEG_231 = 231
    FINE_DEG_234 = 234
    FINE_DEG_237 = 237
    FINE_DEG_243 = 243
    FINE_DEG_246 = 246
    FINE_DEG_249 = 249
    FINE_DEG_252 = 252
    FINE_DEG_258 = 258
    FINE_DEG_261 = 261
    FINE_DEG_264 = 264
    FINE_DEG_267 = 267
    FINE_DEG_273 = 273
    FINE_DEG_276 = 276
    FINE_DEG_279 = 279
    FINE_DEG_282 = 282
    FINE_DEG_288 = 288
    FINE_DEG_291 = 291
    FINE_DEG_294 = 294
    FINE_DEG_297 = 297
    FINE_DEG_303 = 303
    FINE_DEG_306 = 306
    FINE_DEG_309 = 309
    FINE_DEG_312 = 312
    FINE_DEG_318 = 318
    FINE_DEG_321 = 321
    FINE_DEG_324 = 324
    FINE_DEG_327 = 327
    FINE_DEG_333 = 333
    FINE_DEG_336 = 336
    FINE_DEG_339 = 339
    FINE_DEG_342 = 342
    FINE_DEG_348 = 348
    FINE_DEG_351 = 351
    FINE_DEG_354 = 354
    FINE_DEG_357 = 357


# The 24 original coarse multiples of 15°, listed explicitly (NOT derived
# from tuple(LockedRotation), which would now also pick up every FINE_*
# member added above). This is the exact same sequence, in the exact same
# ascending order, as the pre-refinement ALL_ROTATIONS -- _prepare_rotations'
# main per-part search is completely unaffected by the FINE_* additions.
_COARSE_ROTATION_VALUES: tuple[LockedRotation, ...] = (
    LockedRotation.DEG_0,
    LockedRotation.DEG_15,
    LockedRotation.DEG_30,
    LockedRotation.DEG_45,
    LockedRotation.DEG_60,
    LockedRotation.DEG_75,
    LockedRotation.DEG_90,
    LockedRotation.DEG_105,
    LockedRotation.DEG_120,
    LockedRotation.DEG_135,
    LockedRotation.DEG_150,
    LockedRotation.DEG_165,
    LockedRotation.DEG_180,
    LockedRotation.DEG_195,
    LockedRotation.DEG_210,
    LockedRotation.DEG_225,
    LockedRotation.DEG_240,
    LockedRotation.DEG_255,
    LockedRotation.DEG_270,
    LockedRotation.DEG_285,
    LockedRotation.DEG_300,
    LockedRotation.DEG_315,
    LockedRotation.DEG_330,
    LockedRotation.DEG_345,
)

# كل الزوايا المسموحة للبحث الرئيسي (الخشن — coarse)، بترتيب تصاعدي ثابت —
# يُستخدم في enumerate كل زاوية أثناء NFP/nesting. مطابقتماماً للقيمة القديمة
# التي كان tuple(LockedRotation) يُنتجها قبل إضافة FINE_* أعضاء — نفس العدد (24)،
# نفس الترتيب، بلا أي تغيير في سلوك البحث الرئيسي.
ALL_ROTATIONS: tuple[LockedRotation, ...] = _COARSE_ROTATION_VALUES

# كل الزوايا الدقيقة الإضافية (±3°/±6° حول كل مضاعف 15°)، مرتّبة تصاعدياً.
# مستخدمة فقط من قبل fine_neighbors_of() أدناه، ليست جزءًا من البحث الرئيسي.
FINE_ROTATIONS: tuple[LockedRotation, ...] = tuple(
    angle for angle in LockedRotation if angle not in _COARSE_ROTATION_VALUES
)


def fine_neighbors_of(coarse_angle: LockedRotation) -> tuple[LockedRotation, ...]:
    """Return the small set of finer-grained locked angles immediately
    surrounding ``coarse_angle`` (spec section 7's "Fine Rotation Refinement").

    ``coarse_angle`` must be one of the 24 original 15°-multiple members --
    this is the angle a coarse search already found to be a good placement
    for; this function returns ONLY that angle's own ±3°/±6° neighbours
    (up to 4 candidates), never the full FINE_ROTATIONS set, so a caller
    refining one part tries a small, bounded number of extra candidates --
    not a denser sweep of the entire 360° range.

    Raises ValueError for a non-coarse (already-fine, or out-of-range) input,
    since "refine around the winning coarse angle" is only a meaningful
    operation starting from a coarse angle -- refining a refinement is not
    what this function is for.
    """
    if coarse_angle not in _COARSE_ROTATION_VALUES:
        raise ValueError(
            f"fine_neighbors_of يتطلّب زاوية من الـ 24 الأصلية فقط (حصل على "
            f"{coarse_angle}). استخدم إحدى قيم ALL_ROTATIONS."
        )
    base = int(coarse_angle.value)
    offsets = (-6, -3, 3, 6)
    neighbors: list[LockedRotation] = []
    for offset in offsets:
        candidate_value = (base + offset) % 360
        try:
            neighbors.append(LockedRotation(candidate_value))
        except ValueError:
            # Should be structurally unreachable given the fixed +/-3/+/-6
            # offsets defined above always land on one of the FINE_* values
            # deliberately added for every coarse multiple -- checked, not
            # assumed, so a future edit to the offsets or the enum can never
            # silently produce a candidate this function pretends is valid.
            continue
    return tuple(neighbors)


def rotate_shape(shape_mm: BaseGeometry, angle: LockedRotation) -> BaseGeometry:
    """يُدوّر الشكل حول مركز ثقله (centroid) بإحدى الزوايا المقفلة — سواء
    من الـ 24 الأصلية (coarse) أو أي من زوايا التحسين الدقيقة (fine).

    الدوران حول الـ centroid (مش حول نقطة الأصل 0,0) لأن هذا يُبقي الشكل
    قريباً من مكانه الأصلي، وهذا يبسّط خطوة إعادة التموضع (positioning)
    اللاحقة في الـ nesting engine.

    ملاحظة مهمة: shapely.affinity.rotate يستخدم درجات (degrees) وليس راديان،
    والدوران بعكس عقارب الساعة (counter-clockwise) هو الافتراضي — هذا متوافق
    تماماً مع النظام الرياضي القياسي المستخدم في باقي وحدات geometry.

    Args:
        shape_mm: الشكل الأصلي (من extract_contour_from_rgba)، بالمليمتر.
        angle: أي قيمة LockedRotation صالحة — coarse أو fine على حد سواء.

    Returns:
        نسخة جديدة من الشكل بعد الدوران (الشكل الأصلي لا يتغير — shapely immutable).
    """
    if angle not in LockedRotation:
        raise ValueError(
            f"زاوية دوران غير مسموحة: {angle}. المسموح فقط: قيم LockedRotation."
        )

    if angle == LockedRotation.DEG_0:
        return shape_mm  # لا داعي لأي تحويل — نفس الشكل بالضبط، بدون أي عملية حسابية

    # Contours come from raster images whose Y axis grows downward.  Shapely
    # uses the mathematical Y axis (upward), so a visually counter-clockwise
    # image rotation corresponds to a negative mathematical angle here.
    return rotate(shape_mm, -angle.value, origin="centroid", use_radians=False)
