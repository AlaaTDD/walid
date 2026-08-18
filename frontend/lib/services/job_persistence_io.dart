import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

class JobPersistence {
  const JobPersistence();

  static const _manifestKey = 'nesting_job_manifest_v2';

  Future<Directory> _root() async {
    final base = await getApplicationSupportDirectory();
    final root = Directory('${base.path}${Platform.pathSeparator}nesting_jobs');
    await root.create(recursive: true);
    return root;
  }

  Future<String?> getManifest() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString(_manifestKey);
  }

  Future<void> saveManifest(String json) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_manifestKey, json);
  }

  Future<String> stageFile({
    required String jobId,
    required String localId,
    required String fileName,
    required String sourcePath,
    Uint8List? bytes,
  }) async {
    final root = await _root();
    final safeJob = jobId.replaceAll(RegExp(r'[^A-Za-z0-9_-]'), '_');
    final jobDir = Directory('${root.path}${Platform.pathSeparator}$safeJob');
    await jobDir.create(recursive: true);

    final extension = _extension(fileName);
    final safeId = localId.replaceAll(RegExp(r'[^A-Za-z0-9_-]'), '_');
    final destination = File(
      '${jobDir.path}${Platform.pathSeparator}$safeId$extension',
    );

    if (await destination.exists()) return destination.path;

    if (sourcePath.isNotEmpty) {
      final source = File(sourcePath);
      if (await source.exists()) {
        await source.copy(destination.path);
        return destination.path;
      }
    }

    if (bytes != null) {
      await destination.writeAsBytes(bytes, flush: true);
      return destination.path;
    }

    throw StateError('لا يمكن حفظ الملف محليًا للاستئناف: $fileName');
  }

  Future<void> clear() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(_manifestKey);
    await prefs.remove(_manifestKey);
    if (raw == null) return;

    try {
      final map = jsonDecode(raw);
      final jobId = map is Map ? map['jobId']?.toString() : null;
      if (jobId == null || jobId.isEmpty) return;
      final root = await _root();
      final safeJob = jobId.replaceAll(RegExp(r'[^A-Za-z0-9_-]'), '_');
      final dir = Directory('${root.path}${Platform.pathSeparator}$safeJob');
      if (await dir.exists()) await dir.delete(recursive: true);
    } catch (_) {
      // Manifest cleanup must not block a new job.
    }
  }

  String _extension(String name) {
    final index = name.lastIndexOf('.');
    if (index <= 0 || index == name.length - 1) return '.bin';
    final value = name.substring(index).toLowerCase();
    return RegExp(r'^\.[a-z0-9]{1,8}$').hasMatch(value) ? value : '.bin';
  }
}
