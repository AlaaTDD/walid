import 'dart:typed_data';

/// Fallback for an unsupported host. Current Flutter targets select either the
/// IO or browser implementation through the conditional export.
Future<bool> saveExportedTiff({
  required Uint8List bytes,
  required String fileName,
}) => throw UnsupportedError('الحفظ غير مدعوم على هذا النظام.');
