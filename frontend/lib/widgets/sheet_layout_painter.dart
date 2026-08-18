import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../core/theme/app_theme.dart';
import '../models/sheet_part.dart';

class SheetLayoutPainter extends CustomPainter {
  SheetLayoutPainter({
    required this.sheetWidthMm,
    required this.sheetHeightMm,
    required this.sheetMarginMm,
    required this.clearanceMm,
    required this.placedParts,
    required this.selectedPartId,
    required this.showClearanceZones,
    this.highlightProgress = 0,
  });

  final double sheetWidthMm;
  final double sheetHeightMm;
  final double sheetMarginMm;
  final double clearanceMm;
  final List<PlacedPart> placedParts;
  final String? selectedPartId;
  final bool showClearanceZones;
  final double highlightProgress;

  @override
  void paint(Canvas canvas, Size size) {
    final scale = _computeScale(size);
    final renderedWidth = sheetWidthMm * scale;
    final renderedHeight = sheetHeightMm * scale;
    final offset = Offset((size.width - renderedWidth) / 2, (size.height - renderedHeight) / 2);

    canvas.save();
    canvas.translate(offset.dx, offset.dy);
    final sheetRect = Rect.fromLTWH(0, 0, renderedWidth, renderedHeight);

    canvas.drawRect(sheetRect, Paint()..color = AppColors.sheetCanvas);
    canvas.drawRect(
      sheetRect,
      Paint()
        ..color = AppColors.sheetBorder
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.4,
    );

    if (sheetMarginMm > 0) _drawMargin(canvas, scale);
    if (showClearanceZones) {
      for (final part in placedParts) {
        _drawClearance(canvas, part, scale);
      }
    }
    for (final part in placedParts) {
      _drawPart(canvas, part, scale, selected: part.partId == selectedPartId);
    }

    canvas.restore();
  }

  double _computeScale(Size size) {
    final x = size.width / sheetWidthMm;
    final y = size.height / sheetHeightMm;
    return x < y ? x : y;
  }

  void _drawMargin(Canvas canvas, double scale) {
    final rect = Rect.fromLTWH(
      sheetMarginMm * scale,
      sheetMarginMm * scale,
      (sheetWidthMm - 2 * sheetMarginMm).clamp(0, sheetWidthMm) * scale,
      (sheetHeightMm - 2 * sheetMarginMm).clamp(0, sheetHeightMm) * scale,
    );
    final paint = Paint()
      ..color = AppColors.slate400.withValues(alpha: .7)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1;
    canvas.drawRect(rect, paint);
  }

  void _drawClearance(Canvas canvas, PlacedPart part, double scale) {
    final (minX, minY, maxX, maxY) = part.boundsMm;
    final rect = Rect.fromLTRB(
      (minX - clearanceMm) * scale,
      (minY - clearanceMm) * scale,
      (maxX + clearanceMm) * scale,
      (maxY + clearanceMm) * scale,
    );
    canvas.drawRRect(
      RRect.fromRectAndRadius(rect, const Radius.circular(2)),
      Paint()..color = AppColors.clearanceZoneFill,
    );
  }

  void _drawPart(Canvas canvas, PlacedPart part, double scale, {required bool selected}) {
    if (part.contourMm.isEmpty) return;
    final path = Path();
    final first = part.contourMm.first;
    path.moveTo(first.x * scale, first.y * scale);
    for (final point in part.contourMm.skip(1)) {
      path.lineTo(point.x * scale, point.y * scale);
    }
    path.close();

    if (selected) {
      canvas.drawPath(path.shift(const Offset(0, 2)), Paint()..color = AppColors.primary.withValues(alpha: .10));
    }
    canvas.drawPath(
      path,
      Paint()..color = selected ? AppColors.partSelectedFill.withValues(alpha: .88) : AppColors.partFill.withValues(alpha: .78),
    );
    canvas.drawPath(
      path,
      Paint()
        ..color = selected ? AppColors.primary : AppColors.slate700
        ..style = PaintingStyle.stroke
        ..strokeWidth = selected ? 2.1 : 1.0,
    );
    if (selected && highlightProgress > 0) {
      canvas.drawPath(
        path,
        Paint()
          ..color = AppColors.primary.withValues(alpha: .30 * (1 - highlightProgress))
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2 + 4 * highlightProgress,
      );
    }
  }

  @override
  bool shouldRepaint(covariant SheetLayoutPainter oldDelegate) {
    return oldDelegate.sheetWidthMm != sheetWidthMm ||
        oldDelegate.sheetHeightMm != sheetHeightMm ||
        oldDelegate.sheetMarginMm != sheetMarginMm ||
        oldDelegate.clearanceMm != clearanceMm ||
        oldDelegate.selectedPartId != selectedPartId ||
        oldDelegate.showClearanceZones != showClearanceZones ||
        oldDelegate.highlightProgress != highlightProgress ||
        !listEquals(oldDelegate.placedParts, placedParts);
  }
}
