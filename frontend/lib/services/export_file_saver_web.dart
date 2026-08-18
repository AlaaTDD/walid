import 'dart:js_interop';
import 'dart:typed_data';

import 'package:web/web.dart' as web;

/// Browser saver: create a normal TIFF download without a native picker.
Future<bool> saveExportedTiff({
  required Uint8List bytes,
  required String fileName,
}) async {
  final blob = web.Blob(
    <JSUint8Array>[bytes.toJS].toJS,
    web.BlobPropertyBag(type: 'image/tiff'),
  );
  final url = web.URL.createObjectURL(blob);
  final link = web.HTMLAnchorElement()
    ..href = url
    ..download = fileName
    ..style.display = 'none';
  web.document.body?.appendChild(link);
  link.click();
  link.remove();
  web.URL.revokeObjectURL(url);
  return true;
}
