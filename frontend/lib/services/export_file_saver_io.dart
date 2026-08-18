import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';

/// Cross-platform native document saver for desktop and mobile.
///
/// file_picker 12 writes [bytes] through the platform's own save workflow on
/// macOS, Windows, Linux, Android and iOS. This deliberately avoids the older
/// macOS-only behaviour that rejected the `bytes` argument.
Future<bool> saveExportedTiff({
  required Uint8List bytes,
  required String fileName,
}) async {
  final saved = await FilePicker.saveFile(
    dialogTitle: 'حفظ ملف TIFF النهائي',
    fileName: fileName,
    type: FileType.custom,
    allowedExtensions: const ['tiff', 'tif'],
    bytes: bytes,
  );
  return saved != null;
}
