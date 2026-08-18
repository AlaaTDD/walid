/// قيم ثابتة مطابقة للقرارات المعمارية المُقفلة في الـ backend
/// (راجع docs/architecture.md في الـ backend المقابل).
///
/// ملاحظة مهمة: هذه القيم هي القيم الافتراضية المعروضة في الواجهة
/// فقط؛ القيم الحقيقية المستخدمة في الحساب لازم تيجي من الـ backend نفسه
/// (مرة يتربط التطبيق بالـ API)، مش من الرقم الموجود هنا، لضمان مصدر
/// الحقيقة الوحيد للقياسات الفعلية.
class NestingConstants {
  NestingConstants._();

  /// مسافة الأمان الافتراضية بين الأشكال بالمليمتر (مطابقة لـ
  /// CLEARANCE_MM في app/geometry/clearance.py). قابلة للتعديل من شاشة الإعدادات،
  /// لكن هذه هي القيمة الافتراضية المعروضة عند فتح التطبيق لأول مرة.
  static const double defaultClearanceMm = 4.10;

  /// دقة التصدير الافتراضية (مطابقة لـ Resolution(dpi=300) في
  /// app/geometry/units.py). هذه القيمة لازم تطابق الـ DPI الفعلي المستخدم
  /// في التصدير النهائي وإلا أصبحت المعاينة المرئية للأبعاد كاذبة.
  static const double defaultDpi = 300.0;

  /// أبعاد الشيت الافتراضية بالمليمتر: 790mm × 1190mm (79×119 سم).
  static const double defaultSheetWidthMm = 790.0;
  static const double defaultSheetHeightMm = 1190.0;

  /// هامش الأمان من حرف الشيت الافتراضي (مطابق لـ sheet_margin_mm
  /// الافتراضي في run_nesting).
  static const double defaultSheetMarginMm = 5.0;

  /// كل الزوايا الـ24 المسموحة للدوران (مضاعفات 15 درجة صحيحة، مطابقة
  /// لـLockedRotation في app/nesting/rotation.py). قرار معماري مُحدّث
  /// يوسّع القرار الأصلي (4 زوايا فقط) دون كسره — راجع rotation.py للتفاصيل.
  static const List<int> lockedRotations = [
    0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165,
    180, 195, 210, 225, 240, 255, 270, 285, 300, 315, 330, 345,
  ];

  /// محاولة واحدة فقط: الـ LNS optimizer يتولى التحسين العالمي
  /// (destroy/repair/compaction) بدلاً من تكرار محاولات greedy متعددة.
  /// راجع _PACKING_STRATEGIES في app/nesting/engine.py.
  static const int maxPackingAttempts = 1;

  /// أوضاع الألوان المقبولة للتصدير النهائي (مطابقة لـ tiff_export.py).
  static const List<String> allowedExportModes = ['RGB', 'RGBA'];

  /// صيغ الرفع لا تُقيد من الواجهة؛ الـbackend يحوّل أي صورة raster قابلة
  /// للقراءة إلى RGBA في الذاكرة ويحافظ على المصدر الأصلي دون تعديل.
}
