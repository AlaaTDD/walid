import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

class ApiException implements Exception {
  const ApiException(this.message, {this.statusCode});

  final String message;
  final int? statusCode;

  @override
  String toString() => message;
}

class NestingApiClient {
  NestingApiClient({String? baseUrl}) : _baseUrl = _normalizeBaseUrl(baseUrl ?? _defaultBaseUrl);

  final String _baseUrl;

  String get baseUrl => _baseUrl;

  static String get _defaultBaseUrl {
    if (kIsWeb) return 'http://127.0.0.1:8000';
    if (defaultTargetPlatform == TargetPlatform.android) return 'http://10.0.2.2:8000';
    return 'http://127.0.0.1:8000';
  }

  static String _normalizeBaseUrl(String value) {
    final trimmed = value.trim();
    if (trimmed.isEmpty) return _defaultBaseUrl;
    return trimmed.endsWith('/') ? trimmed.substring(0, trimmed.length - 1) : trimmed;
  }

  Future<void> healthCheck({Duration timeout = const Duration(seconds: 3)}) async {
    final response = await http.get(Uri.parse('$_baseUrl/health')).timeout(timeout);
    _ensure2xx(response);
  }

  Future<Map<String, dynamic>> createJob() async {
    final response = await http.post(Uri.parse('$_baseUrl/jobs')).timeout(const Duration(seconds: 15));
    _ensure2xx(response);
    return _asMap(response.body);
  }

  Future<Map<String, dynamic>> getJob(String jobId) async {
    final response = await http.get(Uri.parse('$_baseUrl/jobs/$jobId')).timeout(const Duration(seconds: 10));
    _ensure2xx(response);
    return _asMap(response.body);
  }

  Future<Map<String, dynamic>> uploadImages({
    required List<UploadPayload> files,
    required List<String> clientPartIds,
    required List<String?> originalSourcePaths,
    required double dpi,
    required String jobId,
    void Function(int sentBytes, int totalBytes)? onProgress,
  }) async {
    if (files.isEmpty) throw const ApiException('لا توجد صور لإرسالها.');
    if (files.length != clientPartIds.length || files.length != originalSourcePaths.length) {
      throw const ApiException('بيانات الرفع غير متطابقة: عدد الصور مختلف عن عدد المعرفات.');
    }

    final request = http.MultipartRequest('POST', Uri.parse('$_baseUrl/upload'));
    request.fields['dpi'] = _number(dpi);
    request.fields['job_id'] = jobId;
    request.fields['client_part_ids_json'] = jsonEncode(clientPartIds);
    request.fields['original_source_paths_json'] = jsonEncode(originalSourcePaths);

    var totalBytes = 0;
    for (final payload in files) {
      totalBytes += payload.bytes?.length ?? 0;
      if (payload.bytes != null) {
        request.files.add(http.MultipartFile.fromBytes('files', payload.bytes!, filename: payload.fileName));
      } else if (payload.filePath != null && payload.filePath!.isNotEmpty) {
        request.files.add(await http.MultipartFile.fromPath('files', payload.filePath!, filename: payload.fileName));
      } else {
        throw ApiException('تعذر قراءة الملف: ${payload.fileName}');
      }
    }

    onProgress?.call(0, totalBytes);
    final streamed = await request.send().timeout(const Duration(minutes: 15));
    final body = await streamed.stream.bytesToString();
    if (streamed.statusCode < 200 || streamed.statusCode >= 300) {
      throw ApiException(_detailFromBody(body, streamed.statusCode), statusCode: streamed.statusCode);
    }
    onProgress?.call(totalBytes, totalBytes);
    return _asMap(body);
  }

  Future<void> deleteJobPart({required String jobId, required String clientPartId}) async {
    final response = await http.delete(
      Uri.parse('$_baseUrl/jobs/$jobId/parts/${Uri.encodeComponent(clientPartId)}'),
    ).timeout(const Duration(seconds: 10));
    _ensure2xx(response);
  }

  Future<void> deleteJob(String jobId) async {
    final response = await http.delete(Uri.parse('$_baseUrl/jobs/$jobId')).timeout(const Duration(seconds: 10));
    _ensure2xx(response);
  }

  Future<Map<String, dynamic>> computeLayout({
    required String jobId,
    required double sheetWidthMm,
    required double sheetHeightMm,
    required double sheetMarginMm,
    required double clearanceMm,
    required double dpi,
    required int packingAttempts,
  }) async {
    final response = await http.post(
      Uri.parse('$_baseUrl/layout/compute/$jobId'),
      headers: const {'Content-Type': 'application/json'},
      body: jsonEncode({
        'sheet_width_mm': sheetWidthMm,
        'sheet_height_mm': sheetHeightMm,
        'sheet_margin_mm': sheetMarginMm,
        'clearance_mm': clearanceMm,
        'dpi': dpi,
        'packing_attempts': packingAttempts,
      }),
    ).timeout(const Duration(minutes: 30));
    _ensure2xx(response);
    return _asMap(response.body);
  }

  Future<Map<String, dynamic>> getProgress(String jobId) async {
    final response = await http.get(Uri.parse('$_baseUrl/layout/progress/$jobId')).timeout(const Duration(seconds: 3));
    _ensure2xx(response);
    return _asMap(response.body);
  }

  Stream<Map<String, dynamic>> streamLayoutProgress(String jobId) async* {
    final request = http.Request('GET', Uri.parse('$_baseUrl/layout/progress/stream/$jobId'))
      ..headers['Accept'] = 'text/event-stream'
      ..headers['Cache-Control'] = 'no-cache';
    final response = await request.send().timeout(const Duration(seconds: 15));
    if (response.statusCode < 200 || response.statusCode >= 300) {
      final body = await response.stream.bytesToString();
      throw ApiException(_detailFromBody(body, response.statusCode), statusCode: response.statusCode);
    }

    // The backend emits compact, one-line JSON SSE messages.  A comment-only
    // heartbeat is intentionally ignored, so it does not rebuild the UI.
    await for (final line in response.stream.transform(utf8.decoder).transform(const LineSplitter())) {
      if (!line.startsWith('data:')) continue;
      try {
        final decoded = jsonDecode(line.substring(5).trim());
        if (decoded is Map<String, dynamic>) yield decoded;
      } on FormatException {
        // Ignore a malformed transient event; the next SSE event is an
        // independent JSON message and does not require reconnecting.
      }
    }
  }

  Future<void> cancelLayout(String jobId) async {
    final response = await http.post(Uri.parse('$_baseUrl/layout/cancel/$jobId')).timeout(const Duration(seconds: 5));
    _ensure2xx(response);
  }

  Future<Map<String, dynamic>> confirmLayout({
    required String jobId,
    required String mode,
    required String backgroundColor,
    required String processedImagesPath,
    String? folderName,
  }) async {
    final response = await http.post(
      Uri.parse('$_baseUrl/layout/confirm/$jobId'),
      headers: const {'Content-Type': 'application/json'},
      body: jsonEncode({
        'mode': mode,
        'background_color': backgroundColor,
        'processed_images_path': processedImagesPath.isEmpty ? null : processedImagesPath,
        if (folderName != null && folderName.isNotEmpty) 'folder_name': folderName,
      }),
    ).timeout(const Duration(minutes: 30));
    _ensure2xx(response);
    return _asMap(response.body);
  }

  Future<Uint8List> downloadTiff(String jobId) async {
    final response = await http.get(Uri.parse('$_baseUrl/download/$jobId')).timeout(const Duration(minutes: 10));
    _ensure2xx(response);
    return response.bodyBytes;
  }

  static void _ensure2xx(http.Response response) {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw ApiException(_detailFromResponse(response), statusCode: response.statusCode);
    }
  }

  static Map<String, dynamic> _asMap(String body) {
    final decoded = jsonDecode(body);
    if (decoded is Map<String, dynamic>) return decoded;
    throw const ApiException('استجابة غير صالحة من الخادم.');
  }

  static String _number(double value) => value % 1 == 0 ? value.toInt().toString() : value.toString();

  static String _detailFromResponse(http.Response response) => _detailFromBody(response.body, response.statusCode);

  static String _detailFromBody(String body, int statusCode) {
    try {
      final decoded = jsonDecode(body);
      final detail = decoded is Map ? decoded['detail'] : null;
      if (detail is String && detail.trim().isNotEmpty) return detail;
    } catch (_) {}
    return 'فشل طلب الخادم (HTTP $statusCode).';
  }
}

class UploadPayload {
  const UploadPayload({required this.fileName, this.bytes, this.filePath});

  final String fileName;
  final Uint8List? bytes;
  final String? filePath;
}
