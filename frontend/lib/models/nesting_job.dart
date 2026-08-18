import 'dart:typed_data';

import 'sheet_part.dart';

enum NestingJobStage {
  upload,
  computing,
  proofPreview,
  exporting,
  completed,
  failed,
}

class NestingViolation {
  const NestingViolation({
    required this.severity,
    required this.detail,
    this.partIdA,
    this.partIdB,
    this.expected,
    this.actual,
    this.measuredDistanceMm,
  });

  final String severity;
  final String detail;
  final String? partIdA;
  final String? partIdB;
  final String? expected;
  final String? actual;
  final double? measuredDistanceMm;
}

class NestingSheetLayout {
  const NestingSheetLayout({
    required this.pageNumber,
    required this.placedParts,
    required this.collisionReportValid,
  });

  final int pageNumber;
  final List<PlacedPart> placedParts;
  final bool collisionReportValid;
}

class NestingComputeResult {
  const NestingComputeResult({
    required this.jobId,
    required this.placedParts,
    required this.sheets,
    required this.unplacedPartIds,
    required this.collisionViolations,
    required this.allPlaced,
    required this.collisionReportValid,
    required this.readyToConfirm,
    required this.sheetFull,
    required this.processedCount,
    required this.totalCount,
    required this.layoutMessage,
  });

  final String jobId;
  final List<PlacedPart> placedParts;
  final List<NestingSheetLayout> sheets;
  final List<String> unplacedPartIds;
  final List<NestingViolation> collisionViolations;
  final bool allPlaced;
  final bool collisionReportValid;
  final bool readyToConfirm;
  final bool sheetFull;
  final int processedCount;
  final int totalCount;
  final String layoutMessage;

  int get sheetCount => sheets.length;
  int get placedCount =>
      sheets.fold(0, (count, sheet) => count + sheet.placedParts.length);
  bool get partial => !allPlaced;
  bool get isCollisionValid =>
      collisionReportValid && collisionViolations.isEmpty;
  bool get canExport =>
      readyToConfirm && placedParts.isNotEmpty && isCollisionValid;
}

class QaReport {
  const QaReport({
    required this.filePath,
    required this.violations,
    required this.checkedDimension,
    required this.checkedDpi,
    required this.checkedClearancePairs,
    required this.checkedIccAndMode,
    required this.checkedLayers,
    required this.exportAccepted,
    required this.widthPx,
    required this.heightPx,
    required this.dpi,
    required this.layerCount,
    this.processedImagesDirectory,
    this.movedProcessedImagesCount = 0,
  });

  final String filePath;
  final List<NestingViolation> violations;
  final bool checkedDimension;
  final bool checkedDpi;
  final int checkedClearancePairs;
  final bool checkedIccAndMode;
  final bool checkedLayers;
  final bool exportAccepted;
  final int widthPx;
  final int heightPx;
  final double dpi;
  final int layerCount;
  final String? processedImagesDirectory;
  final int movedProcessedImagesCount;

  bool get isValid =>
      exportAccepted &&
      checkedDimension &&
      checkedDpi &&
      checkedIccAndMode &&
      checkedLayers &&
      violations.isEmpty;
}

class NestingJobSettings {
  const NestingJobSettings({
    this.sheetWidthMm = 790.0,
    this.sheetHeightMm = 1190.0,
    this.sheetMarginMm = 5.0,
    this.clearanceMm = 4.10,
    this.dpi = 300.0,
    this.exportMode = 'RGB',
    this.backgroundColor = '#FFFFFF',
    this.processedImagesPath = '',
    this.packingAttempts = 1,
  });

  final double sheetWidthMm;
  final double sheetHeightMm;
  final double sheetMarginMm;
  final double clearanceMm;
  final double dpi;
  final String exportMode;
  final String backgroundColor;
  final String processedImagesPath;
  final int packingAttempts;

  NestingJobSettings copyWith({
    double? sheetWidthMm,
    double? sheetHeightMm,
    double? sheetMarginMm,
    double? clearanceMm,
    double? dpi,
    String? exportMode,
    String? backgroundColor,
    String? processedImagesPath,
    int? packingAttempts,
  }) => NestingJobSettings(
    sheetWidthMm: sheetWidthMm ?? this.sheetWidthMm,
    sheetHeightMm: sheetHeightMm ?? this.sheetHeightMm,
    sheetMarginMm: sheetMarginMm ?? this.sheetMarginMm,
    clearanceMm: clearanceMm ?? this.clearanceMm,
    dpi: dpi ?? this.dpi,
    exportMode: exportMode ?? this.exportMode,
    backgroundColor: backgroundColor ?? this.backgroundColor,
    processedImagesPath: processedImagesPath ?? this.processedImagesPath,
    packingAttempts: packingAttempts ?? this.packingAttempts,
  );
}

class NestingJob {
  const NestingJob({
    required this.stage,
    required this.uploadedParts,
    required this.settings,
    this.computeResult,
    this.qaReport,
    this.exportedFilePath,
    this.exportedFileBytes,
    this.errorMessage,
  });

  factory NestingJob.initial() => const NestingJob(
    stage: NestingJobStage.upload,
    uploadedParts: [],
    settings: NestingJobSettings(),
  );

  final NestingJobStage stage;
  final List<UploadedPart> uploadedParts;
  final NestingJobSettings settings;
  final NestingComputeResult? computeResult;
  final QaReport? qaReport;

  /// المسار الذي أرجعه السيرفر بعد التصدير (للتشخيص فقط).
  final String? exportedFilePath;
  final Uint8List? exportedFileBytes;
  final String? errorMessage;

  List<UploadedPart> get validParts =>
      uploadedParts.where((p) => p.isValid).toList(growable: false);
  List<UploadedPart> get rejectedParts =>
      uploadedParts.where((p) => p.isRejected).toList(growable: false);
  bool get hasPendingParts => uploadedParts.any((p) => p.isPending);

  bool get canProceedToCompute =>
      uploadedParts.isNotEmpty &&
      !hasPendingParts &&
      validParts.isNotEmpty;
  // ملاحظة: عدم وجود صور مرفوضة ليس شرطًا للمتابعة. الباك اند نفسه (انظر
  // part_inputs_from_state في job_storage.py) يتجاهل الصور المرفوضة تلقائيًا
  // ويحسب الترتيب فقط للصور الصالحة، فمن غير المنطقي أن يمنع الفرونت اند
  // المتابعة بسبب صورة واحدة مرفوضة وسط دفعة كبيرة من الصور الصالحة.
  bool get canConfirmExport => computeResult?.canExport ?? false;

  NestingJob copyWith({
    NestingJobStage? stage,
    List<UploadedPart>? uploadedParts,
    NestingJobSettings? settings,
    NestingComputeResult? computeResult,
    QaReport? qaReport,
    String? exportedFilePath,
    Uint8List? exportedFileBytes,
    String? errorMessage,
    bool clearError = false,
    bool clearComputeResult = false,
    bool clearQaReport = false,
    bool clearExportedFile = false,
  }) => NestingJob(
    stage: stage ?? this.stage,
    uploadedParts: uploadedParts ?? this.uploadedParts,
    settings: settings ?? this.settings,
    computeResult: clearComputeResult
        ? null
        : (computeResult ?? this.computeResult),
    qaReport: clearQaReport ? null : (qaReport ?? this.qaReport),
    exportedFilePath: clearExportedFile
        ? null
        : (exportedFilePath ?? this.exportedFilePath),
    exportedFileBytes: clearExportedFile
        ? null
        : (exportedFileBytes ?? this.exportedFileBytes),
    errorMessage: clearError ? null : (errorMessage ?? this.errorMessage),
  );
}
