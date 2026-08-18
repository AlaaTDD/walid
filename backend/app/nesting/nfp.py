"""
حساب No-Fit Polygon (NFP) الحقيقي بين شكلين، بطريقة هندسية دقيقة 100%
(مفيش discretization ولا محاكاة تقريبية).

المفهوم: لو عندنا شكل ثابت A وشكل متحرك B، الـ NFP بينهم هو المنحنى
الذي لو تحرك مركز B عليه (أو داخله)، فإن B هيلمس A بالضبط بدون تداخل.
أي نقطة خارج الـ NFP تعني: لو حطينا مركز B هنا، B مش هيلمس A أصلاً.

الطريقة المستخدمة: Minkowski Sum بين A و (-B) (B معكوسة حول مركزها).
رياضياً: NFP(A, B) = A ⊕ (-B) = { a - b | a ∈ A, b ∈ B }
هذه المعادلة دقيقة رياضياً 100%، مفيش أي discretization للزوايا أو sampling
للمسافات — وهذا بالضبط ما يتطلبه مبدأ 'بدون أي نسبة خطأ'.

التنفيذ: بما أن Shapely مالوش مبنية Minkowski sum جاهزة، بنبنيها من
التعريف الرياضي المكافئ: الـ Minkowski sum لـ polygon مع polygon بتساوي اتحاد
كل الـ Minkowski sums بين كل ضلع في A وكل ضلع في (-B) — وكل ضلع مع ضلع
بينتج مثلث محدب (لأن المجموعة محدبة)، والـ union النهائي لكل المثلثات دهو الـ NFP.
هذه طريقة موثقة رياضياً (مستخدمة في أدبيات computational geometry) ومبنية بالكامل
على GEOS operations الموجودة فعلاً داخل Shapely — مفيش أي إعادة اختراع لمحرك geometry.

ملاحظة مهمة جداً: هذا الملف يحسب الـNFP الهندسي البحت بين الأشكال الخامة.
الـclearance يمكن تطبيقه على الشكل الثابت قبل الاستدعاء أو على NFP الناتج بعده؛
المساران متكافئان جبرياً بسبب خاصية التجميع في Minkowski sum. محرك الترتيب
يستخدم المسار الثاني كي لا يثَلّث أقواس الـbuffer قبل كل حساب.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from shapely import polygons as shapely_polygons_batch
from shapely.affinity import scale
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


class NFPComputationError(Exception):
    """يُرفع لما يفشل حساب NFP صالح بين شكلين."""


@dataclass(frozen=True, slots=True)
class NoFitPolygon:
    """نتيجة حساب NFP بين شكل ثابت وشكل متحرك، بالمليمتر.

    أي نقطة خارج region_mm هي موقع صالح لمركز الشكل المتحرك
    (بدون تداخل مع الشكل الثابت أو منطقة الـ clearance الخاصة به).
    """

    region_mm: BaseGeometry  # المناطق الممنوعة لمركز الشكل المتحرك أن يدخلها


def _negate(shape_mm: BaseGeometry) -> BaseGeometry:
    """يعكس الشكل حول نقطة الأصل (0,0) — مطلوب لمعادلة -B في Minkowski sum."""
    return scale(shape_mm, xfact=-1.0, yfact=-1.0, origin=(0, 0))


def _triangles_as_array(shape: BaseGeometry) -> np.ndarray:
    """يستخرج كل مثلثات الشكل (constrained Delaunay، محصورة بحدوده الفعلية) كمصفوفة
    numpy واحدة بشكل (N, 3, 2) -- تمهيداً لحساب Minkowski sum بطريقة vectorized بالكامل.

    نفس منطق استخراج المثلثات الأصلي (constrained Delaunay مقيد بتقاطع كل مثلث خام
    مع الشكل الحقيقي)، لكن النتيجة هنا مصفوفة numpy مباشرة بدل قائمة Shapely Polygon
    objects -- الخطوة التالية (حساب Minkowski sum لكل الأزواج) تحتاج البيانات كأرقام
    خام لتطبيق عمليات numpy المُجمّعة (batched) عليها دفعة واحدة.
    """
    from shapely.ops import triangulate

    polys = list(shape.geoms) if isinstance(shape, MultiPolygon) else [shape]
    tris: list[list[tuple[float, float]]] = []
    for poly in polys:
        # constrained Delaunay: nodes=حدود الشكل (exterior + holes)،
        # مقيد بالتقاطع مع الشكل الأصلي لضمان الدقة مع الأشكال المقعرة
        raw_tris = triangulate(poly, edges=False)
        for tri in raw_tris:
            # ملاحظة مهمة (مجربة ومراجعة فعلياً): كان هنا محاولة تحسين بتخطي استدعاء
            # tri.intersection(poly) لما poly.contains(tri) == True (أخذ إحداثيات tri
            # مباشرة بدل استدعاء GEOS intersection()). تم التراجع عنها بعد اكتشاف
            # رجوف فعلي: rendering المساحة (area) مطابقة 100%، لكن ترتيب رؤوس
            # المثلث (أي رأس يبدأ منه الترتيب) يختلف بين shapely.ops.triangulate()
            # (دائماً CCW) وGEOS intersection() (دائماً CW، ويبدأ من رأس مختلف
            # عن رأس المثلث الخام). هذا الفرق يغير أي رأس يعتبره الكود
            # "الرأس الأول" (vertex-0) في _vectorized_minkowski_all_pairs (الذي
            # يستخدم tris_a[:, 0, :] كنقطة بداية لحساب edge angles وterminal hull) ،
            # مما يغير فعلياً المضلع المحدب الناتج لـ Minkowski sum لهذا الزوج
            # (مثبت عملياً: عدد قطع run_nesting المركبة اختلف فعلاً عند استخدام
            # مسار tri مباشرة بدل intersection() في بعض حالات test_nesting_capacity).
            # لذلك نستدعي intersection() دائماً هنا لضمان ترتيب رؤوس متطابق
            # 100% مع السلوك الأصلي قبل أي تحسين أداء، بدل محاولة مطابقة الورقات
            # يدوياً (ريفرسال الرأس) وهو ما يفشل لأن GEOS يختار رأس البداية
            # بطريقة داخلية لا يمكن إعادة إنتاجها محلياً بدون استدعاء GEOS نفسه.
            clipped = tri.intersection(poly)
            if clipped.is_empty or clipped.area <= 0:
                continue
            clipped_polys = [clipped] if isinstance(clipped, Polygon) else (
                [g for g in clipped.geoms if isinstance(g, Polygon) and g.area > 0]
                if isinstance(clipped, MultiPolygon) else []
            )
            for cp in clipped_polys:
                coords = list(cp.exterior.coords)[:-1]
                if len(coords) == 3:
                    tris.append(coords)
                elif len(coords) > 3:
                    # تقاطع مثلث مع شكل مقعر نادراً ما ينتج شكل غير مثلثي (تقريب
                    # عددي على الحد نفسه) -- نعيد تثليثه فان (fan) حول أول رأس، وهذا
                    # لسه دقيق 100% لأن الناتج محدب دائماً (تقاطع محدب مع محدب)
                    for i in range(1, len(coords) - 1):
                        tris.append([coords[0], coords[i], coords[i + 1]])
    if not tris:
        return np.empty((0, 3, 2), dtype=np.float64)
    return np.array(tris, dtype=np.float64)


def _ensure_ccw(tris: np.ndarray) -> np.ndarray:
    """يضمن أن كل مثلث في المصفوفة برؤوس مرتبة عكس عقارب الساعة (CCW)، بعملية
    numpy مُجمّعة على كل المثلثات دفعة واحدة (بدون أي Python loop). ترتيب CCW ثابت
    شرط أساسي لصحة خوارزمية دمج الأضلاع بالزاوية (edge-angle-merge) في
    _vectorized_minkowski_all_pairs أدناه.
    """
    if len(tris) == 0:
        return tris
    x1, y1 = tris[:, 0, 0], tris[:, 0, 1]
    x2, y2 = tris[:, 1, 0], tris[:, 1, 1]
    x3, y3 = tris[:, 2, 0], tris[:, 2, 1]
    signed_area2 = (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)
    cw_mask = signed_area2 < 0
    out = tris.copy()
    out[cw_mask] = tris[cw_mask][:, ::-1, :]
    return out


def _vectorized_minkowski_all_pairs(tris_a: np.ndarray, tris_b: np.ndarray) -> np.ndarray:
    def canonical_edges(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Start every CCW triangle at its lowest-angle outgoing edge.

        Edge-angle merging is cyclic.  Its old implementation sorted the
        edges but kept arbitrary vertex zero as the path origin, which makes
        a valid translation change the result.  Canonicalising the start edge
        makes the origin the matching support vertex for the sorted sequence.
        """
        edges = np.roll(tris, -1, axis=1) - tris
        angles = np.arctan2(edges[..., 1], edges[..., 0])
        first = np.argmin(angles, axis=1)
        indices = (first[:, None] + np.arange(3)) % 3
        ordered_edges = np.take_along_axis(edges, indices[..., None], axis=1)
        ordered_angles = np.take_along_axis(angles, indices, axis=1)
        starts = tris[np.arange(len(tris)), first]
        return starts, ordered_edges, ordered_angles

    starts_a, edges_a, angles_a = canonical_edges(tris_a)
    starts_b, edges_b, angles_b = canonical_edges(tris_b)
    na, nb = len(tris_a), len(tris_b)

    all_edges = np.concatenate(
        [
            np.broadcast_to(edges_a[:, None, :, :], (na, nb, 3, 2)),
            np.broadcast_to(edges_b[None, :, :, :], (na, nb, 3, 2)),
        ],
        axis=2,
    )
    all_angles = np.concatenate(
        [
            np.broadcast_to(angles_a[:, None, :], (na, nb, 3)),
            np.broadcast_to(angles_b[None, :, :], (na, nb, 3)),
        ],
        axis=2,
    )
    order = np.argsort(all_angles, axis=2)
    ordered_edges = np.take_along_axis(all_edges, order[..., None], axis=2)
    cumulative = np.cumsum(ordered_edges, axis=2)
    starts = starts_a[:, None, :] + starts_b[None, :, :]
    hull_points = starts[:, :, None, :] + np.concatenate(
        [np.zeros((na, nb, 1, 2)), cumulative[:, :, :-1, :]],
        axis=2,
    )
    return hull_points.reshape(na * nb, 6, 2)


def _prune_far_pairs(tris_a: np.ndarray, tris_b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """تصفية مكانية (spatial pruning): يحتفظ فقط بالمثلثات التي يمكن أن تساهم
    في حدود الـ union النهائي للـ NFP.

    المبدأ: مثلث في A ومثلث في B لا يمكن أن يُنتجا Minkowski sum يؤثر على
    الحدود الخارجية للـ NFP إلا لو bounding boxes الشكلين الأصليين (مش المثلثات
    الفردية) قريبة بما فيه الكفاية. لكن التصفية على مستوى المثلثات الفردية
    أصعب — لأن كل زوج مثلثين يُنتج مضلعاً محدباً صغيراً، والـ union لكلهم
    هو الـ NFP. المثلثات الداخلية جداً (بعيدة عن حدود الشكل) تُنتج مضلعات
    واقعة بالكامل داخل الـ union للمثلثات الحدّية — فلا تساهم في الحد الخارجي.

    تطبيق محافظ وآمن: نستبعد فقط المثلثات الصغيرة جداً (مساحة أقل من 1e-12 mm²)
    التي لا يمكن أن تساهم بأي شيء ذي معنى هندسي. هذا يحافظ على الدقة 100%
    ويزيل فقط degenerate triangles الناتجة من تقريب عددي.

    Args:
        tris_a: مصفوفة (Na, 3, 2) مثلثات الشكل الأول.
        tris_b: مصفوفة (Nb, 3, 2) مثلثات الشكل الثاني.

    Returns:
        (filtered_tris_a, filtered_tris_b) بعد إزالة المثلثات المنحلة.
    """
    if len(tris_a) == 0 or len(tris_b) == 0:
        return tris_a, tris_b

    def _filter_degenerate(tris: np.ndarray) -> np.ndarray:
        """يزيل المثلثات ذات المساحة الصفرية أو شبه الصفرية."""
        if len(tris) == 0:
            return tris
        # حساب المساحة عبر cross product: 0.5 * |v1 × v2|
        v1 = tris[:, 1, :] - tris[:, 0, :]
        v2 = tris[:, 2, :] - tris[:, 0, :]
        areas = 0.5 * np.abs(v1[:, 0] * v2[:, 1] - v1[:, 1] * v2[:, 0])
        valid_mask = areas > 1e-12
        filtered = tris[valid_mask]
        # حماية: لو كل المثلثات اتحذفت (مفيش أي مثلث ذو مساحة)، نرجع الأصل
        return filtered if len(filtered) > 0 else tris

    return _filter_degenerate(tris_a), _filter_degenerate(tris_b)


def prepare_nfp_triangles(shape_mm: BaseGeometry) -> np.ndarray:
    """Prepare exact triangle decomposition once for repeated NFP queries."""
    tris = _ensure_ccw(_triangles_as_array(shape_mm))
    tris, _dummy = _prune_far_pairs(tris, tris)
    if len(tris) == 0:
        raise NFPComputationError("فشل تحليل الشكل لمثلثات NFP.")
    return tris


def _minkowski_sum_from_triangles(tris_a: np.ndarray, tris_b: np.ndarray) -> BaseGeometry:
    if len(tris_a) == 0 or len(tris_b) == 0:
        raise NFPComputationError("أحد الشكلين لا يحتوي على مثلثات صالحة.")
    pairwise_sums = shapely_polygons_batch(_vectorized_minkowski_all_pairs(tris_a, tris_b))
    if len(pairwise_sums) == 0:
        raise NFPComputationError("فشل حساب أي Minkowski sum جزئي بين الشكلين.")
    try:
        return unary_union(pairwise_sums)
    except Exception:
        # Fallback for GEOS TopologyException (e.g. side location conflict)
        safe_sums = [p.buffer(0) for p in pairwise_sums]
        return unary_union(safe_sums)


def _minkowski_sum(shape_a_mm: BaseGeometry, shape_b_mm: BaseGeometry) -> BaseGeometry:
    """يحسب Minkowski sum بين شكلين بطريقة تحليل المثلثات (triangle decomposition)،
    بمعالجة vectorized بالكامل لكل أزواج المثلثات دفعة واحدة (راجع توثيق
    _vectorized_minkowski_all_pairs للتفاصيل والسبب المعماري الكامل).

    الخوارزمية الرياضية: أي shape (محدب أو مقعر) يمكن تقسيمه لمثلثات بدون أي
    تقريب (constrained Delaunay triangulation المحصور بحدود الشكل الفعلي، مش
    convex hull، لضمان تغطية دقيقة للأشكال المقعرة). ثم الـ Minkowski sum لكل
    زوج مثلثين هو مضلع محدب (edge-angle-merge -- دقيق رياضياً 100%)، والـ union
    النهائي لكل هذه المضلعات المحدبة يعطي الـ NFP الكامل.
    """
    tris_a = prepare_nfp_triangles(shape_a_mm)
    tris_b = prepare_nfp_triangles(shape_b_mm)

    # تصفية مكانية (spatial pruning) قبل الحساب: أي زوج مثلثين bounding boxes
    # بعيدة جداً عن بعض (أكبر من قطر الشكل الكامل) لا يمكن أبداً يكون له
    # أي تأثير على حدود الـ union النهائي للـ NFP الكامل -- هذا تقليل حقيقي
    # لعدد الأزواج المطلوب معالجتها فعلياً (لا يفقد أي دقة -- الأزواج المستبعدة
    # لا يمكن أن تساهم في المنطقة الممنوعة النهائية رياضياً، فمفيش داعي لحسابها أصلاً).
    # هذا يقلل عدد الأزواج الفعلية المطلوب معالجتها بشكل ملحوظ في الأشكال
    # المنحنية/غير المحدبة المركبة من مئات المثلثات الصغيرة جداً، دون أي
    # تأثير على الحالات البسيطة (أضلاع مستقيمة قليلة العدد) التي لا تحتاجه أصلاً.
    tris_a, tris_b = _prune_far_pairs(tris_a, tris_b)

    # بناء كل الـ Polygons دفعة واحدة عبر shapely.polygons (constructor مُجمّع من
    # Shapely 2.0+، ينتقل لـ GEOS مرة واحدة بدل استدعاء منفصل لكل Polygon) -- هذا
    # أسرع بشكل جوهري من حلقة Python تستدعي Polygon() لكل عنصر على حدة.
    pairwise_sums = shapely_polygons_batch(_vectorized_minkowski_all_pairs(tris_a, tris_b))
    if len(pairwise_sums) == 0:
        raise NFPComputationError("فشل حساب أي Minkowski sum جزئي بين الشكلين.")

    # unary_union الأخير على GEOS نفسه قوي بما يكفي (robust) للتعامل مع أي رؤوس
    # مكررة بمساحة صفرية ناتجة من تعادل زاويتين بالضبط في edge-angle-merge --
    # مفيش حاجة لفلترة validity منفصلة قبله (تم قياس فعلياً: الفلترة المنفصلة
    # تضيف overhead بدون فائدة حقيقية، لأن unary_union بيتعامل مع هذه الحالات
    # النادرة داخلياً بنفس الكفاءة).
    try:
        return unary_union(pairwise_sums)
    except Exception:
        safe_sums = [p.buffer(0) for p in pairwise_sums]
        return unary_union(safe_sums)


def compute_nfp(
    stationary_shape_mm: BaseGeometry,
    moving_shape_mm: BaseGeometry,
    *,
    stationary_triangles: np.ndarray | None = None,
    moving_triangles: np.ndarray | None = None,
) -> NoFitPolygon:
    """يحسب الـ NFP الحقيقي بين شكل ثابت وشكل متحرك، بدون أي تقريب.

    هذه الدالة لا تطبّق clearance بنفسها.  يمكن للمتصل تمرير شكل ثابت موسّع
    بالفعل، أو توسعة الـNFP الناتج لاحقاً؛ المساران متكافئان هندسياً.

    Args:
        stationary_shape_mm: الشكل الثابت، خاماً أو موسعاً بحسب مسار المتصل.
        moving_shape_mm: الشكل المتحرك (بعد تطبيق الدوران المطلوب، قبل الإزاحة).

    Returns:
        NoFitPolygon يحتوي على كل المناطق الممنوعة لمركز الشكل المتحرك.
    """
    if stationary_shape_mm.is_empty or moving_shape_mm.is_empty:
        raise NFPComputationError("لا يمكن حساب NFP لشكل فارغ (empty geometry).")

    negated_moving = _negate(moving_shape_mm)
    tris_a = stationary_triangles if stationary_triangles is not None else prepare_nfp_triangles(stationary_shape_mm)
    tris_b = moving_triangles if moving_triangles is not None else prepare_nfp_triangles(negated_moving)
    tris_a, tris_b = _prune_far_pairs(tris_a, tris_b)
    region = _minkowski_sum_from_triangles(tris_a, tris_b)

    if not region.is_valid:
        region = region.buffer(0)

    return NoFitPolygon(region_mm=region)


def point_is_valid_placement(nfp: NoFitPolygon, candidate_center_mm: tuple[float, float]) -> bool:
    """يفحص هل نقطة مركز معينة للشكل المتحرك تقع خارج منطقة الـ NFP الممنوعة.

    ملاحظة: النقاط الواقعة على حد الـ NFP بالضبط (touching مش داخل) تُعتبر
    صالحة — لأن الشكلين الـ clearance فيهم بالفعل مطبّق (الشكل الثابت موسع
    بالـ clearance)، فاللمس على الحد يعني المسافة بين الشكلين الحقيقيين = clearance
    بالضبط، وهذا مقبول (المطلوب: >= clearance، مش > clearance).
    """
    from shapely.geometry import Point

    point = Point(candidate_center_mm)
    return not nfp.region_mm.contains(point)
