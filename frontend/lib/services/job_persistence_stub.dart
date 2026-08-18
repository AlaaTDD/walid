import 'dart:typed_data';

import 'package:shared_preferences/shared_preferences.dart';

class JobPersistence {
  const JobPersistence();

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
    // Web builds cannot safely stage large binary files through the portable
    // storage API. The current session still works through FilePicker bytes.
    return sourcePath;
  }

  Future<void> clear() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_manifestKey);
  }

  static const _manifestKey = 'nesting_job_manifest_v2';
}
