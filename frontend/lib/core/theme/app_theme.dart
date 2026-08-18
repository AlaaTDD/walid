import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

/// مرجع ألوان التطبيق الموحد. مصمم ليناسب أداة إنتاج صناعية
/// دقيقة (طباعة/تصميم)، مش تطبيق استهلاكي عادي — ألوان محايدة
/// (slate/graphite) مع لون تأكيد (precision blue) يرمز للدقة والثقة.
class AppColors {
  AppColors._();

  // اللون الأساسي: أزرق دقة (precision blue) — يرمز للقياس الهندسي الدقيق
  static const Color primary = Color(0xFF2563EB);
  static const Color primaryDark = Color(0xFF1D4ED8);
  static const Color primaryLight = Color(0xFF60A5FA);

  // ألوان الحالة: نجاح/تحذير/خطأ مطابقة لدرجات QaSeverity وCollisionReport
  static const Color success = Color(0xFF16A34A);
  static const Color warning = Color(0xFFD97706);
  static const Color danger = Color(0xFFDC2626);
  static const Color info = Color(0xFF0891B2);

  // أسطح محايدة (slate) — الخلفيات والحدود والنصوص الثانوية
  static const Color slate50 = Color(0xFFF8FAFC);
  static const Color slate100 = Color(0xFFF1F5F9);
  static const Color slate200 = Color(0xFFE2E8F0);
  static const Color slate300 = Color(0xFFCBD5E1);
  static const Color slate400 = Color(0xFF94A3B8);
  static const Color slate500 = Color(0xFF64748B);
  static const Color slate600 = Color(0xFF475569);
  static const Color slate700 = Color(0xFF334155);
  static const Color slate800 = Color(0xFF1E293B);
  static const Color slate900 = Color(0xFF0F172A);
  static const Color slate950 = Color(0xFF020617);

  // لون canvas الشيت في وضع المعاينة (Proof Mode) — أبيض محايد يمثل الورق الفعلي
  static const Color sheetCanvas = Color(0xFFFFFFFF);
  static const Color sheetBorder = Color(0xFFCBD5E1);
  static const Color clearanceZoneFill = Color(0x1A2563EB); // primary بشفافية 10%
  static const Color partFill = Color(0xFF334155);
  static const Color partSelectedFill = Color(0xFF2563EB);

  // ظل موحد خفيف جداً للبطاقات والعناصر المرتفعة — إحساس "عمق" بسيط بدل
  // الاعتماد الكامل على الحدود فقط، بدون ما يبقى ثقيل أو ملحوظ بشكل مبالغ فيه.
  static Color cardShadow = slate900.withValues(alpha: 0.05);
}

/// ثوابت الحركة الموحدة لكل التطبيق — عشان أي انتقال (فتح شاشة، تفعيل
/// زرار، تغيير حالة) يحس بنفس "النَفَس" ومفيش تضارب في السرعات.
class AppMotion {
  AppMotion._();

  static const Duration fast = Duration(milliseconds: 160);
  static const Duration base = Duration(milliseconds: 240);
  static const Duration slow = Duration(milliseconds: 380);

  /// إحساس "دقة هندسية" — يبدأ سريع وينتهي ناعم، مناسب لأداة قياس.
  static const Curve standard = Curves.easeOutCubic;

  /// للعناصر اللي بتدخل الشاشة (fade/slide in).
  static const Curve enter = Curves.easeOutQuart;

  /// للعناصر اللي بتخرج (banners تختفي، إلخ).
  static const Curve exit = Curves.easeInCubic;

  /// نطاط خفيف جداً — للتأكيدات الإيجابية فقط (نجاح التصدير مثلاً).
  static const Curve emphasized = Curves.easeOutBack;
}

class AppTheme {
  AppTheme._();

  static ThemeData get light {
    final base = ThemeData(
      useMaterial3: true,
      brightness: Brightness.light,
      colorScheme: ColorScheme.fromSeed(
        seedColor: AppColors.primary,
        brightness: Brightness.light,
        primary: AppColors.primary,
        error: AppColors.danger,
        surface: AppColors.slate50,
      ),
      scaffoldBackgroundColor: AppColors.slate50,
      fontFamily: GoogleFonts.ibmPlexSansArabic().fontFamily,
    );

    return base.copyWith(
      textTheme: GoogleFonts.ibmPlexSansArabicTextTheme(base.textTheme).apply(
        bodyColor: AppColors.slate900,
        displayColor: AppColors.slate900,
      ),
      appBarTheme: AppBarTheme(
        backgroundColor: AppColors.slate50,
        foregroundColor: AppColors.slate900,
        elevation: 0,
        scrolledUnderElevation: 2,
        shadowColor: AppColors.slate900.withValues(alpha: 0.08),
        surfaceTintColor: Colors.transparent,
        centerTitle: false,
        titleTextStyle: GoogleFonts.ibmPlexSansArabic(
          fontSize: 18,
          fontWeight: FontWeight.w700,
          color: AppColors.slate900,
        ),
      ),
      cardTheme: CardThemeData(
        color: Colors.white,
        elevation: 0,
        shadowColor: AppColors.cardShadow,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14),
          side: const BorderSide(color: AppColors.slate200),
        ),
        margin: EdgeInsets.zero,
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: AppColors.primary,
          foregroundColor: Colors.white,
          disabledBackgroundColor: AppColors.slate300,
          disabledForegroundColor: AppColors.slate500,
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(11)),
          textStyle: GoogleFonts.ibmPlexSansArabic(
            fontSize: 14,
            fontWeight: FontWeight.w600,
          ),
          elevation: 0,
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: AppColors.slate700,
          side: const BorderSide(color: AppColors.slate300, width: 1.2),
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(11)),
          textStyle: GoogleFonts.ibmPlexSansArabic(
            fontSize: 14,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: AppColors.primary,
          textStyle: GoogleFonts.ibmPlexSansArabic(
            fontSize: 14,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: Colors.white,
        contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 13),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(11),
          borderSide: const BorderSide(color: AppColors.slate300),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(11),
          borderSide: const BorderSide(color: AppColors.slate300),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(11),
          borderSide: const BorderSide(color: AppColors.primary, width: 1.8),
        ),
        errorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(11),
          borderSide: const BorderSide(color: AppColors.danger),
        ),
        focusedErrorBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(11),
          borderSide: const BorderSide(color: AppColors.danger, width: 1.8),
        ),
      ),
      dividerTheme: const DividerThemeData(
        color: AppColors.slate200,
        thickness: 1,
        space: 1,
      ),
      chipTheme: base.chipTheme.copyWith(
        backgroundColor: AppColors.slate100,
        labelStyle: GoogleFonts.ibmPlexSansArabic(
          fontSize: 12,
          fontWeight: FontWeight.w600,
          color: AppColors.slate700,
        ),
        side: BorderSide.none,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      ),
      snackBarTheme: SnackBarThemeData(
        backgroundColor: AppColors.slate900,
        contentTextStyle: GoogleFonts.ibmPlexSansArabic(color: Colors.white),
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(11)),
        elevation: 3,
      ),
    );
  }

  static ThemeData get dark {
    final base = ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      colorScheme: ColorScheme.fromSeed(
        seedColor: AppColors.primaryLight,
        brightness: Brightness.dark,
        primary: AppColors.primaryLight,
        error: AppColors.danger,
        surface: AppColors.slate900,
      ),
      scaffoldBackgroundColor: AppColors.slate950,
      fontFamily: GoogleFonts.ibmPlexSansArabic().fontFamily,
    );

    return base.copyWith(
      textTheme: GoogleFonts.ibmPlexSansArabicTextTheme(base.textTheme).apply(
        bodyColor: AppColors.slate100,
        displayColor: AppColors.slate100,
      ),
      appBarTheme: AppBarTheme(
        backgroundColor: AppColors.slate950,
        foregroundColor: AppColors.slate100,
        elevation: 0,
        scrolledUnderElevation: 2,
        surfaceTintColor: Colors.transparent,
        centerTitle: false,
        titleTextStyle: GoogleFonts.ibmPlexSansArabic(
          fontSize: 18,
          fontWeight: FontWeight.w700,
          color: AppColors.slate100,
        ),
      ),
      cardTheme: CardThemeData(
        color: AppColors.slate900,
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14),
          side: const BorderSide(color: AppColors.slate800),
        ),
        margin: EdgeInsets.zero,
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          backgroundColor: AppColors.primaryLight,
          foregroundColor: AppColors.slate950,
          disabledBackgroundColor: AppColors.slate700,
          disabledForegroundColor: AppColors.slate500,
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(11)),
          textStyle: GoogleFonts.ibmPlexSansArabic(
            fontSize: 14,
            fontWeight: FontWeight.w600,
          ),
          elevation: 0,
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: AppColors.slate200,
          side: const BorderSide(color: AppColors.slate700, width: 1.2),
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(11)),
          textStyle: GoogleFonts.ibmPlexSansArabic(
            fontSize: 14,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: AppColors.slate900,
        contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 13),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(11),
          borderSide: const BorderSide(color: AppColors.slate700),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(11),
          borderSide: const BorderSide(color: AppColors.slate700),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(11),
          borderSide: const BorderSide(color: AppColors.primaryLight, width: 1.8),
        ),
      ),
      dividerTheme: const DividerThemeData(
        color: AppColors.slate800,
        thickness: 1,
        space: 1,
      ),
    );
  }
}
