import 'package:flutter/material.dart';

import '../core/theme/app_theme.dart';
import '../models/nesting_job.dart';

// ملاحظة: الدخول المتحرك (fade+slide) لهذه القائمة مُدار من الـ widget
// الأب (_StaggeredEntry في preview_screen.dart)، عشان التأخير المتدرج
// بين العناصر يتحكم فيه مكان واحد بدل ما يتكرر هنا.

class ViolationListTile extends StatelessWidget {
  const ViolationListTile({super.key, required this.violation});

  final NestingViolation violation;

  Color get _color {
    switch (violation.severity) {
      case 'overlap':
      case 'clearance_violation':
      case 'out_of_bounds':
      case 'dpi_mismatch':
      case 'dimension_mismatch':
      case 'invalid_mode':
      case 'file_unreadable':
        return AppColors.danger;
      case 'missing_icc_profile':
        return AppColors.warning;
      default:
        return AppColors.warning;
    }
  }

  String get _severityLabel {
    switch (violation.severity) {
      case 'overlap':
        return 'تداخل هندسي';
      case 'clearance_violation':
        return 'مسافة أمان غير كافية';
      case 'out_of_bounds':
        return 'خارج حدود الشيت';
      case 'dpi_mismatch':
        return 'عدم تطابق DPI';
      case 'dimension_mismatch':
        return 'عدم تطابق الأبعاد';
      case 'invalid_mode':
        return 'وضع ألوان غير صالح';
      case 'missing_icc_profile':
        return 'ICC profile مفقود';
      case 'file_unreadable':
        return 'الملف غير قابل للقراءة';
      default:
        return violation.severity;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(13),
      decoration: BoxDecoration(
        color: _color.withValues(alpha: 0.07),
        borderRadius: BorderRadius.circular(11),
        border: Border.all(color: _color.withValues(alpha: 0.28)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 26,
            height: 26,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: _color.withValues(alpha: 0.14),
              shape: BoxShape.circle,
            ),
            child: Icon(Icons.error_outline_rounded, color: _color, size: 15),
          ),
          const SizedBox(width: 11),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  _severityLabel,
                  style: TextStyle(
                    fontWeight: FontWeight.w700,
                    fontSize: 13,
                    color: _color,
                    letterSpacing: 0.1,
                  ),
                ),
                const SizedBox(height: 3),
                Text(
                  violation.detail,
                  style: const TextStyle(
                    fontSize: 12.5,
                    height: 1.45,
                    color: AppColors.slate700,
                  ),
                ),
                if (violation.measuredDistanceMm != null) ...[
                  const SizedBox(height: 6),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                    decoration: BoxDecoration(
                      color: _color.withValues(alpha: 0.10),
                      borderRadius: BorderRadius.circular(6),
                    ),
                    child: Text(
                      'المسافة المقاسة: ${violation.measuredDistanceMm!.toStringAsFixed(3)}mm',
                      style: TextStyle(
                        fontSize: 11.5,
                        fontWeight: FontWeight.w700,
                        color: _color,
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}
