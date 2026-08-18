import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../models/nesting_job.dart';
import '../models/sheet_part.dart';
import '../services/job_persistence.dart';
import '../services/nesting_api.dart';

/// Which stage's progress fields the shared SSE subscription should update.
enum _ProgressTarget { compute, export }

class NestingJobProvider extends ChangeNotifier {
  NestingJobProvider({bool loadPersistedSettings = true}) {
    if (loadPersistedSettings) unawaited(_initialize());
  }

  // A large source file is streamed per request, so large print images do
  // not compete for memory and bandwidth with other simultaneous files.
  static const int _uploadBatchSize = 1;
  static const int _uploadRetryCount = 3;

  final JobPersistence _persistence = const JobPersistence();

  NestingJob _job = NestingJob.initial();
  NestingApiClient _api = NestingApiClient();
  Future<void> _uploadQueue = Future<void>.value();

  String? _jobId;
  StreamSubscription<Map<String, dynamic>>? _progressStreamSubscription;
  int _progressStreamGeneration = 0;
  int _progressStreamReconnectAttempts = 0;
  int? _computeProgressDone;
  int? _computeProgressTotal;
  String? _computeProgressMessage;
  int? _exportProgressDone;
  int? _exportProgressTotal;
  String? _exportProgressMessage;

  bool _uploading = false;
  double _uploadProgress = 0;
  bool _serverReachable = false;
  bool _initializing = true;

  NestingJob get job => _job;
  String get baseUrl => _api.baseUrl;
  String? get jobId => _jobId;
  int? get computeProgressDone => _computeProgressDone;
  int? get computeProgressTotal => _computeProgressTotal;
  String? get computeProgressMessage => _computeProgressMessage;
  int? get exportProgressDone => _exportProgressDone;
  int? get exportProgressTotal => _exportProgressTotal;
  String? get exportProgressMessage => _exportProgressMessage;
  bool get uploading => _uploading;
  double get uploadProgress => _uploadProgress;
  bool get serverReachable => _serverReachable;
  bool get initializing => _initializing;
  bool get hasResumableJob => _job.uploadedParts.isNotEmpty;

  Future<void> _initialize() async {
    try {
      await _loadSettings();
      await _restorePersistedJob();
      await checkServer();
      if (_serverReachable && _job.uploadedParts.isNotEmpty) {
        if (_jobId != null) await _recoverRemoteState();
        if (_job.uploadedParts.any((part) => part.isPending)) {
          unawaited(resumePendingUploads());
        }
      }
    } finally {
      _initializing = false;
      notifyListeners();
    }
  }

  Future<void> _loadSettings() async {
    final prefs = await SharedPreferences.getInstance();
    final serverUrl = prefs.getString('serverUrl');
    if (serverUrl != null && serverUrl.trim().isNotEmpty) {
      _api = NestingApiClient(baseUrl: serverUrl);
    }

    final settings = NestingJobSettings(
      sheetWidthMm: prefs.getDouble('sheetWidthMm') ?? 790.0,
      sheetHeightMm: prefs.getDouble('sheetHeightMm') ?? 1190.0,
      sheetMarginMm: prefs.getDouble('sheetMarginMm') ?? 5.0,
      clearanceMm: prefs.getDouble('clearanceMm') ?? 4.10,
      dpi: prefs.getDouble('dpi') ?? 300.0,
      exportMode: prefs.getString('exportMode') ?? 'RGB',
      backgroundColor: prefs.getString('backgroundColor') ?? '#FFFFFF',
      processedImagesPath: prefs.getString('processedImagesPath') ?? '',
      packingAttempts: prefs.getInt('packingAttempts') ?? 1,
    );
    _job = _job.copyWith(settings: settings);
  }

  Future<void> updateServerUrl(String url) async {
    final normalized = url.trim();
    if (normalized.isEmpty) return;
    _api = NestingApiClient(baseUrl: normalized);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('serverUrl', normalized);
    await checkServer();
    if (_jobId != null) unawaited(_recoverRemoteState());
  }

  Future<void> updateSettings(NestingJobSettings settings) async {
    final old = _job.settings;
    final dpiChanged = old.dpi != settings.dpi;

    _job = _job.copyWith(
      settings: settings,
      clearComputeResult: true,
      clearQaReport: true,
      clearExportedFile: true,
      stage: NestingJobStage.upload,
    );
    notifyListeners();

    final prefs = await SharedPreferences.getInstance();
    await Future.wait([
      prefs.setDouble('sheetWidthMm', settings.sheetWidthMm),
      prefs.setDouble('sheetHeightMm', settings.sheetHeightMm),
      prefs.setDouble('sheetMarginMm', settings.sheetMarginMm),
      prefs.setDouble('clearanceMm', settings.clearanceMm),
      prefs.setDouble('dpi', settings.dpi),
      prefs.setString('exportMode', settings.exportMode),
      prefs.setString('backgroundColor', settings.backgroundColor),
      prefs.setString('processedImagesPath', settings.processedImagesPath),
      prefs.setInt('packingAttempts', settings.packingAttempts),
    ]);

    if (dpiChanged && _job.uploadedParts.isNotEmpty) {
      _markPartsPending('تغيير DPI يتطلب إعادة رفع الصور على job جديد.');
      _jobId = null;
    }
    await _persistManifest();
  }

  void _markPartsPending(String message) {
    _job = _job.copyWith(
      stage: NestingJobStage.upload,
      uploadedParts: _job.uploadedParts
          .map(
            (part) => part.copyWith(
              backendPartId: null,
              clearBackendPartId: true,
              validationStatus: PartValidationStatus.pending,
              rejectionReason: message,
            ),
          )
          .toList(growable: false),
      clearComputeResult: true,
      clearQaReport: true,
      clearExportedFile: true,
    );
    notifyListeners();
  }

  Future<void> checkServer() async {
    try {
      await _api.healthCheck();
      _serverReachable = true;
    } catch (_) {
      _serverReachable = false;
    }
    notifyListeners();
  }

  /// Fetch the latest backend state after reconnecting or updating the app.
  Future<void> refreshCurrentJob() => _recoverRemoteState();

  Future<void> addUploadedParts(List<UploadedPart> parts) async {
    if (parts.isEmpty) return;

    _job = _job.copyWith(
      stage: NestingJobStage.upload,
      uploadedParts: [..._job.uploadedParts, ...parts],
      clearError: true,
      clearComputeResult: true,
      clearQaReport: true,
      clearExportedFile: true,
    );
    notifyListeners();

    // Serialize all upload work. Every batch is durable before the next one starts.
    _uploadQueue = _uploadQueue.then((_) => _prepareAndUpload(parts));
    await _uploadQueue.catchError((_) {});
  }

  Future<void> _prepareAndUpload(List<UploadedPart> parts) async {
    _uploading = true;
    _uploadProgress = 0;
    notifyListeners();

    try {
      await _ensureRemoteJob();

      final staged = <UploadedPart>[];
      for (final part in parts) {
        final stagedPath = await _persistence.stageFile(
          jobId: _jobId!,
          localId: part.id,
          fileName: part.fileName,
          sourcePath: part.filePath,
          bytes: part.bytes,
        );
        final updated = part.copyWith(filePath: stagedPath);
        staged.add(updated);
      }

      _replaceParts(staged);
      await _persistManifest();

      for (var start = 0; start < staged.length; start += _uploadBatchSize) {
        final end = (start + _uploadBatchSize).clamp(0, staged.length).toInt();
        final batch = staged.sublist(start, end);
        await _uploadWithRetry(batch);
        await _persistManifest();
      }

      await _recoverRemoteState();
    } on ApiException catch (error) {
      _job = _job.copyWith(errorMessage: error.message);
      if (error.statusCode == 404) {
        _job = _job.copyWith(
          errorMessage:
              'الـjob المحفوظ لم يعد موجودًا على السيرفر. أنشئ مهمة جديدة.',
        );
      }
      _serverReachable = error.statusCode != null;
    } catch (error) {
      _job = _job.copyWith(errorMessage: 'توقف الرفع مع حفظ التقدم: $error');
      _serverReachable = false;
    } finally {
      _uploadProgress = 0;
      _uploading = false;
      await _persistManifest();
      notifyListeners();
    }
  }

  Future<void> resumePendingUploads() async {
    final pending = _job.uploadedParts
        .where((part) => part.isPending)
        .toList(growable: false);
    if (pending.isEmpty) {
      await _recoverRemoteState();
      return;
    }
    _uploadQueue = _uploadQueue.then((_) => _prepareAndUpload(pending));
    await _uploadQueue.catchError((_) {});
  }

  Future<void> _ensureRemoteJob() async {
    if (_jobId != null && _jobId!.isNotEmpty) return;
    final data = await _api.createJob();
    _jobId = data['job_id']?.toString();
    if (_jobId == null || _jobId!.isEmpty) {
      throw const ApiException('السيرفر لم يرجع معرف job صالح.');
    }
    _serverReachable = true;
    await _persistManifest();
  }

  Future<void> _uploadWithRetry(List<UploadedPart> parts) async {
    for (var attempt = 1; attempt <= _uploadRetryCount; attempt++) {
      try {
        final payloads = parts
            .map(
              (part) => UploadPayload(
                fileName: part.fileName,
                bytes: kIsWeb ? part.bytes : null,
                filePath: kIsWeb
                    ? null
                    : (part.filePath.isEmpty ? null : part.filePath),
              ),
            )
            .toList(growable: false);

        final data = await _api.uploadImages(
          files: payloads,
          clientPartIds: parts.map((p) => p.id).toList(growable: false),
          originalSourcePaths: parts
              .map((part) => part.originalSourcePath)
              .toList(growable: false),
          dpi: _job.settings.dpi,
          jobId: _jobId!,
          onProgress: (sent, total) {
            final localProgress = total <= 0 ? 0.0 : sent / total;
            _uploadProgress = localProgress;
            notifyListeners();
          },
        );

        _serverReachable = true;
        _applyUploadResults(parts, data);
        await _persistManifest();
        return;
      } catch (error) {
        if (attempt == _uploadRetryCount) rethrow;
        await Future<void>.delayed(Duration(seconds: attempt * attempt));
      }
    }
  }

  void _applyUploadResults(
    List<UploadedPart> batch,
    Map<String, dynamic> data,
  ) {
    final results = (data['parts'] as List? ?? const [])
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList(growable: false);
    final byLocalId = <String, Map<String, dynamic>>{
      for (final item in results)
        if (item['client_part_id'] != null)
          item['client_part_id'].toString(): item,
    };

    final updated = _job.uploadedParts
        .map((part) {
          final response = byLocalId[part.id];
          if (response == null) return part;
          final valid = response['is_valid'] == true;
          return part.copyWith(
            backendPartId: response['part_id']?.toString(),
            validationStatus: valid
                ? PartValidationStatus.valid
                : PartValidationStatus.rejected,
            rejectionReason: response['rejection_reason']?.toString(),
          );
        })
        .toList(growable: false);
    _job = _job.copyWith(uploadedParts: updated, clearError: true);
    notifyListeners();
  }

  void _replaceParts(List<UploadedPart> replacements) {
    final byId = {for (final part in replacements) part.id: part};
    _job = _job.copyWith(
      uploadedParts: _job.uploadedParts
          .map((part) => byId[part.id] ?? part)
          .toList(growable: false),
    );
    notifyListeners();
  }

  Future<void> _restorePersistedJob() async {
    final raw = await _persistence.getManifest();
    if (raw == null || raw.trim().isEmpty) return;

    try {
      final map = jsonDecode(raw);
      if (map is! Map) return;
      final jobId = map['jobId']?.toString();
      final settingsMap = map['settings'];
      final partsRaw = map['parts'];
      if (partsRaw is! List) return;

      final settings = settingsMap is Map
          ? NestingJobSettings(
              sheetWidthMm: _asDouble(settingsMap['sheetWidthMm']),
              sheetHeightMm: _asDouble(settingsMap['sheetHeightMm']),
              sheetMarginMm: _asDouble(settingsMap['sheetMarginMm']),
              clearanceMm: _asDouble(settingsMap['clearanceMm']),
              dpi: _asDouble(settingsMap['dpi']),
              exportMode: settingsMap['exportMode']?.toString() ?? 'RGB',
              backgroundColor:
                  settingsMap['backgroundColor']?.toString() ?? '#FFFFFF',
              processedImagesPath:
                  settingsMap['processedImagesPath']?.toString() ?? '',
              // محاولة واحدة فقط: الـ LNS optimizer يتولى التحسين.
              packingAttempts:
                  _asInt(settingsMap['packingAttempts'])?.clamp(1, 1).toInt() ??
                  1,
            )
          : _job.settings;

      final parts = partsRaw
          .whereType<Map>()
          .map((item) {
            final status = item['validationStatus']?.toString();
            return UploadedPart(
              id: item['id']?.toString() ?? '',
              fileName: item['fileName']?.toString() ?? 'unknown',
              filePath: item['filePath']?.toString() ?? '',
              originalSourcePath: item['originalSourcePath']?.toString(),
              backendPartId: item['backendPartId']?.toString(),
              validationStatus: PartValidationStatus.values.firstWhere(
                (value) => value.name == status,
                orElse: () => PartValidationStatus.pending,
              ),
              rejectionReason: item['rejectionReason']?.toString(),
            );
          })
          .where((part) => part.id.isNotEmpty)
          .toList(growable: false);

      _jobId = (jobId == null || jobId.isEmpty) ? null : jobId;
      _job = _job.copyWith(
        settings: settings,
        uploadedParts: parts,
        stage: NestingJobStage.upload,
        clearError: true,
      );
      notifyListeners();

      if (await _hasPendingRemoteSync()) {
        unawaited(_recoverRemoteState());
      }
    } catch (_) {
      // A corrupt local manifest must never prevent opening the application.
    }
  }

  Future<bool> _hasPendingRemoteSync() async {
    if (_jobId == null) return false;
    try {
      await _api.getJob(_jobId!);
      return true;
    } catch (_) {
      return false;
    }
  }

  Future<void> _recoverRemoteState() async {
    final id = _jobId;
    if (id == null) return;

    try {
      final data = await _api.getJob(id);
      _serverReachable = true;
      _reconcileWithServer(data);
      if (data['stage']?.toString() == 'computed' ||
          data['stage']?.toString() == 'confirmed') {
        _applyComputeData(data, id);
        if (data['output_available'] == true) {
          try {
            final cachedExport = await _api.confirmLayout(
              jobId: id,
              mode: _job.settings.exportMode,
              backgroundColor: _job.settings.backgroundColor,
              processedImagesPath: _job.settings.processedImagesPath,
            );
            _applyExportData(cachedExport);
            _job = _job.copyWith(
              errorMessage:
                  'تم استعادة نتيجة التصدير المحفوظة. يمكنك إعادة تنزيل ملف TIFF.',
            );
          } catch (_) {
            _job = _job.copyWith(
              errorMessage:
                  'تم حفظ نتيجة التصدير على السيرفر. يمكنك إعادة تنزيلها.',
            );
          }
        }
      }
      await _persistManifest();
      notifyListeners();
    } on ApiException catch (error) {
      _job = _job.copyWith(
        errorMessage: error.statusCode == 404
            ? 'الـjob المحفوظ غير موجود على السيرفر.'
            : 'السيرفر متاح جزئيًا، وسيتم الاستكمال تلقائيًا عند عودته: ${error.message}',
      );
      _serverReachable = error.statusCode != null;
      notifyListeners();
    }
  }

  void _reconcileWithServer(Map<String, dynamic> data) {
    final remoteParts = (data['parts'] as List? ?? const [])
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList(growable: false);
    final byClient = {
      for (final item in remoteParts)
        item['client_part_id']?.toString() ?? '': item,
    };

    final merged = <UploadedPart>[];
    for (final local in _job.uploadedParts) {
      final remote = byClient[local.id];
      if (remote == null) {
        merged.add(local);
        continue;
      }
      merged.add(
        local.copyWith(
          backendPartId: remote['part_id']?.toString(),
          validationStatus: remote['is_valid'] == true
              ? PartValidationStatus.valid
              : PartValidationStatus.rejected,
          rejectionReason: remote['rejection_reason']?.toString(),
        ),
      );
    }

    for (final remote in remoteParts) {
      final id = remote['client_part_id']?.toString() ?? '';
      if (id.isEmpty || merged.any((part) => part.id == id)) continue;
      merged.add(
        UploadedPart(
          id: id,
          fileName: remote['original_filename']?.toString() ?? 'server-image',
          filePath: '',
          backendPartId: remote['part_id']?.toString(),
          validationStatus: remote['is_valid'] == true
              ? PartValidationStatus.valid
              : PartValidationStatus.rejected,
          rejectionReason: remote['rejection_reason']?.toString(),
        ),
      );
    }

    _job = _job.copyWith(
      uploadedParts: merged,
      stage:
          data['stage']?.toString() == 'computed' ||
              data['stage']?.toString() == 'confirmed'
          ? NestingJobStage.proofPreview
          : NestingJobStage.upload,
      clearError: true,
    );
  }

  Future<void> _persistManifest() async {
    final id = _jobId;
    final payload = {
      'version': 3,
      'jobId': id,
      'settings': {
        'sheetWidthMm': _job.settings.sheetWidthMm,
        'sheetHeightMm': _job.settings.sheetHeightMm,
        'sheetMarginMm': _job.settings.sheetMarginMm,
        'clearanceMm': _job.settings.clearanceMm,
        'dpi': _job.settings.dpi,
        'exportMode': _job.settings.exportMode,
        'backgroundColor': _job.settings.backgroundColor,
        'processedImagesPath': _job.settings.processedImagesPath,
        'packingAttempts': _job.settings.packingAttempts,
      },
      'parts': _job.uploadedParts
          .map(
            (part) => {
              'id': part.id,
              'fileName': part.fileName,
              'filePath': part.filePath,
              'originalSourcePath': part.originalSourcePath,
              'backendPartId': part.backendPartId,
              'validationStatus': part.validationStatus.name,
              'rejectionReason': part.rejectionReason,
            },
          )
          .toList(growable: false),
    };
    await _persistence.saveManifest(jsonEncode(payload));
  }

  Future<void> removeUploadedPart(String localId) async {
    final matches = _job.uploadedParts.where((part) => part.id == localId);
    final target = matches.isEmpty ? null : matches.first;
    final remoteJob = _jobId;
    if (target != null && remoteJob != null && target.backendPartId != null) {
      try {
        await _api.deleteJobPart(jobId: remoteJob, clientPartId: target.id);
      } catch (error) {
        _job = _job.copyWith(
          errorMessage:
              'تعذر حذف الصورة من السيرفر، لذلك لم يتم حذفها محليًا: $error',
        );
        notifyListeners();
        return;
      }
    }
    _job = _job.copyWith(
      uploadedParts: _job.uploadedParts
          .where((part) => part.id != localId)
          .toList(growable: false),
      clearComputeResult: true,
      clearQaReport: true,
      clearExportedFile: true,
      stage: NestingJobStage.upload,
    );
    await _persistManifest();
    notifyListeners();
  }

  Future<void> clearAllParts() async {
    final remoteJob = _jobId;
    if (remoteJob != null) {
      try {
        await _api.deleteJob(remoteJob);
      } on ApiException catch (error) {
        // الـjob غير موجود أصلاً على السيرفر (تم حذفه من قبل، أو انتهت صلاحيته):
        // لا يوجد شيء نحذفه فعليًا، لذلك نكمل ونمسح الصور محليًا بدلاً من عرض خطأ.
        if (error.statusCode != 404) {
          _job = _job.copyWith(
            errorMessage: 'تعذر حذف الـjob من السيرفر: $error',
          );
          notifyListeners();
          return;
        }
      } catch (error) {
        _job = _job.copyWith(
          errorMessage: 'تعذر حذف الـjob من السيرفر: $error',
        );
        notifyListeners();
        return;
      }
    }
    _stopProgressStreaming();
    _resetProgress();
    _job = _job.copyWith(
      uploadedParts: const [],
      stage: NestingJobStage.upload,
      clearComputeResult: true,
      clearQaReport: true,
      clearExportedFile: true,
      clearError: true,
    );
    _jobId = null;
    await _persistence.clear();
    notifyListeners();
  }

  void resetJob() {
    final settings = _job.settings;
    _stopProgressStreaming();
    _resetProgress();
    _job = NestingJob.initial().copyWith(settings: settings);
    _jobId = null;
    _uploadProgress = 0;
    _uploading = false;
    unawaited(_persistence.clear());
    notifyListeners();
  }

  void _resetProgress() {
    _computeProgressDone = null;
    _computeProgressTotal = null;
    _computeProgressMessage = null;
    _exportProgressDone = null;
    _exportProgressTotal = null;
    _exportProgressMessage = null;
  }

  /// The one job stage each progress target is valid for. Used to gate
  /// reconnection so a dropped export stream does not try to reconnect after
  /// the job has moved on (or vice versa for compute).
  NestingJobStage _stageFor(_ProgressTarget target) => switch (target) {
    _ProgressTarget.compute => NestingJobStage.computing,
    _ProgressTarget.export => NestingJobStage.exporting,
  };

  void _stopProgressStreaming() {
    // Cancelling the subscription closes the single SSE request.  The
    // generation also makes late callbacks from an old connection harmless.
    _progressStreamGeneration++;
    final subscription = _progressStreamSubscription;
    _progressStreamSubscription = null;
    if (subscription != null) unawaited(subscription.cancel());
  }

  void _startProgressStreaming(String jobId, {_ProgressTarget target = _ProgressTarget.compute}) {
    _stopProgressStreaming();
    final generation = _progressStreamGeneration;
    _progressStreamReconnectAttempts = 0;
    _openProgressStream(jobId, generation, target);
  }

  void _openProgressStream(String jobId, int generation, _ProgressTarget target) {
    if (generation != _progressStreamGeneration) return;
    var receivedTerminalEvent = false;
    var reconnectScheduled = false;
    void recover() {
      if (reconnectScheduled) return;
      reconnectScheduled = true;
      _reconnectProgressStream(jobId, generation, target);
    }

    _progressStreamSubscription = _api
        .streamLayoutProgress(jobId)
        .listen(
          (data) {
            if (generation != _progressStreamGeneration) return;
            final done = _asInt(data['done']);
            final total = _asInt(data['total']);
            final message = data['message']?.toString();
            final bool changed;
            if (target == _ProgressTarget.compute) {
              changed =
                  done != _computeProgressDone ||
                  total != _computeProgressTotal ||
                  message != _computeProgressMessage;
              _computeProgressDone = done;
              _computeProgressTotal = total;
              _computeProgressMessage = message;
            } else {
              changed =
                  done != _exportProgressDone ||
                  total != _exportProgressTotal ||
                  message != _exportProgressMessage;
              _exportProgressDone = done;
              _exportProgressTotal = total;
              _exportProgressMessage = message;
            }
            receivedTerminalEvent = data['complete'] == true;
            if (changed) notifyListeners();
          },
          onError: (_) => recover(),
          onDone: () {
            if (!receivedTerminalEvent) recover();
          },
          cancelOnError: true,
        );
  }

  void _reconnectProgressStream(String jobId, int generation, _ProgressTarget target) {
    if (generation != _progressStreamGeneration || _job.stage != _stageFor(target)) {
      return;
    }
    // This is only recovery from a broken stream, never a recurring progress
    // poll.  Normal calculation/export has one persistent HTTP connection.
    _progressStreamReconnectAttempts++;
    final delaySeconds = _progressStreamReconnectAttempts.clamp(1, 5).toInt();
    unawaited(
      Future<void>.delayed(Duration(seconds: delaySeconds)).then((_) {
        if (generation == _progressStreamGeneration && _job.stage == _stageFor(target)) {
          _openProgressStream(jobId, generation, target);
        }
      }),
    );
  }

  Future<void> cancelCompute() async {
    final id = _jobId;
    if (id == null) return;
    try {
      await _api.cancelLayout(id);
    } catch (_) {}
  }

  Future<void> computeLayout() async {
    if (!_job.canProceedToCompute || _jobId == null || _uploading) return;
    final jobId = _jobId!;

    _stopProgressStreaming();
    _resetProgress();
    _job = _job.copyWith(
      stage: NestingJobStage.computing,
      clearError: true,
      clearComputeResult: true,
      clearQaReport: true,
      clearExportedFile: true,
    );
    notifyListeners();
    _startProgressStreaming(jobId);

    try {
      final data = await _api.computeLayout(
        jobId: jobId,
        sheetWidthMm: _job.settings.sheetWidthMm,
        sheetHeightMm: _job.settings.sheetHeightMm,
        sheetMarginMm: _job.settings.sheetMarginMm,
        clearanceMm: _job.settings.clearanceMm,
        dpi: _job.settings.dpi,
        packingAttempts: _job.settings.packingAttempts,
      );
      _applyComputeData(data, jobId);
      await _persistManifest();
    } on ApiException catch (error) {
      // The HTTP response can be lost after the backend has finished. Ask the
      // durable job state before declaring the compute failed.
      final recovered = await _tryRecoverCompletedCompute(jobId);
      if (!recovered) {
        _job = _job.copyWith(
          stage: error.statusCode == 499
              ? NestingJobStage.upload
              : NestingJobStage.failed,
          errorMessage: error.statusCode == 499
              ? 'تم إلغاء الحساب.'
              : error.message,
        );
      }
    } catch (error) {
      final recovered = await _tryRecoverCompletedCompute(jobId);
      if (!recovered) {
        _job = _job.copyWith(
          stage: NestingJobStage.failed,
          errorMessage: 'تعذر إتمام الحساب: $error',
        );
        _serverReachable = false;
      }
    } finally {
      _stopProgressStreaming();
      _resetProgress();
      await _persistManifest();
      notifyListeners();
    }
  }

  Future<bool> _tryRecoverCompletedCompute(String jobId) async {
    try {
      final state = await _api.getJob(jobId);
      final stage = state['stage']?.toString();
      if (stage == 'computed' || stage == 'confirmed') {
        _applyComputeData(state, jobId);
        return true;
      }
    } catch (_) {}
    return false;
  }

  void _applyComputeData(Map<String, dynamic> data, String fallbackJobId) {
    final jobId = data['job_id']?.toString() ?? fallbackJobId;
    _jobId = jobId;
    final placedData = (data['placed_parts'] as List? ?? const [])
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList(growable: false);
    final unplaced = (data['unplaced_part_ids'] as List? ?? const [])
        .map((e) => e.toString())
        .toList(growable: false);
    final violationsData = (data['violations'] as List? ?? const [])
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item));

    List<PlacedPart> decodePlaced(List<Map<String, dynamic>> source) => source
        .map((item) {
          final backendId = item['part_id']?.toString() ?? '';
          final original = _job.uploadedParts.firstWhere(
            (part) => part.backendPartId == backendId,
            orElse: () =>
                const UploadedPart(id: '', fileName: '', filePath: ''),
          );
          final minX = _asDouble(item['bounds_min_x_mm']);
          final minY = _asDouble(item['bounds_min_y_mm']);
          final maxX = _asDouble(item['bounds_max_x_mm']);
          final maxY = _asDouble(item['bounds_max_y_mm']);
          return PlacedPart(
            partId: backendId,
            rotation: LockedRotation.fromDegrees(
              _asInt(item['rotation_deg']) ?? 0,
            ),
            contourMm: [
              ContourPointMm(minX, minY),
              ContourPointMm(maxX, minY),
              ContourPointMm(maxX, maxY),
              ContourPointMm(minX, maxY),
            ],
            boundsMm: (minX, minY, maxX, maxY),
            centroidMm: ContourPointMm(
              _asDouble(item['centroid_x_mm']),
              _asDouble(item['centroid_y_mm']),
            ),
            sourceThumbnail: original.bytes,
          );
        })
        .toList(growable: false);

    final sheetsData = (data['sheets'] as List? ?? const [])
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList(growable: false);
    final sheets = sheetsData
        .map((sheet) {
          final pageParts = (sheet['placed_parts'] as List? ?? const [])
              .whereType<Map>()
              .map((item) => Map<String, dynamic>.from(item))
              .toList(growable: false);
          return NestingSheetLayout(
            pageNumber: _asInt(sheet['page_number']) ?? 1,
            placedParts: decodePlaced(pageParts),
            collisionReportValid: sheet['collision_report_valid'] == true,
          );
        })
        .toList(growable: false);
    // Old backend responses contain only placed_parts.  Treat them as one
    // page so a saved job remains viewable after the client upgrade.
    final resolvedSheets = sheets.isEmpty
        ? [
            NestingSheetLayout(
              pageNumber: 1,
              placedParts: decodePlaced(placedData),
              collisionReportValid: data['collision_report_valid'] == true,
            ),
          ]
        : sheets;
    final placed = resolvedSheets.first.placedParts;

    final violations = violationsData
        .map(
          (item) => NestingViolation(
            severity: item['severity']?.toString() ?? 'unknown',
            detail: item['detail']?.toString() ?? 'مخالفة غير معروفة',
            partIdA: item['part_id_a']?.toString(),
            partIdB: item['part_id_b']?.toString(),
            measuredDistanceMm: _asDoubleNullable(item['measured_distance_mm']),
          ),
        )
        .toList(growable: false);

    _job = _job.copyWith(
      stage: NestingJobStage.proofPreview,
      computeResult: NestingComputeResult(
        jobId: jobId,
        placedParts: placed,
        sheets: resolvedSheets,
        unplacedPartIds: unplaced,
        collisionViolations: violations,
        allPlaced: data['all_placed'] == true || unplaced.isEmpty,
        collisionReportValid: data['collision_report_valid'] == true,
        readyToConfirm: data['ready_to_confirm'] == true,
        sheetFull: data['sheet_full'] == true,
        processedCount:
            _asInt(data['processed_count']) ?? placed.length + unplaced.length,
        totalCount:
            _asInt(data['total_count']) ?? placed.length + unplaced.length,
        layoutMessage: data['layout_message']?.toString() ?? 'اكتمل الحساب.',
      ),
      clearError: true,
    );
  }

  String? _pendingFolderName;

  Future<void> confirmAndExport({String? folderName}) async {
    if (!_job.canConfirmExport || _jobId == null) return;
    final jobId = _jobId!;
    _pendingFolderName = folderName;

    _stopProgressStreaming();
    _resetProgress();
    _job = _job.copyWith(stage: NestingJobStage.exporting, clearError: true);
    notifyListeners();
    // Opened once up front and closed in the finally below, so it stays open
    // through the lost-response retry path further down — both attempts are
    // progress for the same backend job_id, not two separate operations.
    _startProgressStreaming(jobId, target: _ProgressTarget.export);

    try {
      final data = await _api.confirmLayout(
        jobId: jobId,
        mode: _job.settings.exportMode,
        backgroundColor: _job.settings.backgroundColor,
        processedImagesPath: _job.settings.processedImagesPath,
        folderName: _pendingFolderName,
      );
      _applyExportData(data);
      await downloadExportedFile();
      await _persistManifest();
    } on ApiException catch (error) {
      // A lost response after successful export is recoverable through GET /jobs.
      try {
        final remote = await _api.getJob(jobId);
        if (remote['output_available'] == true) {
          final confirm = await _api.confirmLayout(
            jobId: jobId,
            mode: _job.settings.exportMode,
            backgroundColor: _job.settings.backgroundColor,
            processedImagesPath: _job.settings.processedImagesPath,
            folderName: _pendingFolderName,
          );
          _applyExportData(confirm);
          await downloadExportedFile();
          return;
        }
      } catch (_) {}
      _job = _job.copyWith(
        stage: NestingJobStage.failed,
        errorMessage: error.message,
      );
    } catch (error) {
      _job = _job.copyWith(
        stage: NestingJobStage.failed,
        errorMessage: 'فشل التصدير: $error',
      );
      _serverReachable = false;
    } finally {
      _stopProgressStreaming();
      _resetProgress();
    }
    notifyListeners();
  }

  void _applyExportData(Map<String, dynamic> data) {
    final qaData = (data['qa_violations'] as List? ?? const [])
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList(growable: false);
    final violations = qaData
        .map(
          (item) => NestingViolation(
            severity: item['severity']?.toString() ?? 'unknown',
            detail: item['detail']?.toString() ?? 'مخالفة QA غير معروفة',
            expected: item['expected']?.toString(),
            actual: item['actual']?.toString(),
          ),
        )
        .toList(growable: false);

    final outputServerPath = data['output_tiff_path']?.toString() ?? '';
    final placedCount = _job.computeResult?.placedParts.length ?? 0;
    _job = _job.copyWith(
      stage: NestingJobStage.completed,
      qaReport: QaReport(
        filePath: outputServerPath,
        violations: violations,
        checkedDimension: !violations.any((v) => v.severity == 'dimension_mismatch'),
        checkedDpi: !violations.any((v) => v.severity == 'dpi_mismatch'),
        checkedClearancePairs: placedCount > 1
            ? placedCount * (placedCount - 1) ~/ 2
            : 0,
        checkedIccAndMode: !violations.any(
          (v) => v.severity == 'invalid_mode' || v.severity == 'missing_icc_profile',
        ),
        checkedLayers: !violations.any((v) => v.severity == 'invalid_layers'),
        exportAccepted: data['export_accepted'] == true,
        widthPx: _asInt(data['width_px']) ?? 0,
        heightPx: _asInt(data['height_px']) ?? 0,
        dpi: _asDouble(data['dpi']),
        layerCount: _asInt(data['layer_count']) ?? 0,
        processedImagesDirectory: data['processed_images_directory']
            ?.toString(),
        movedProcessedImagesCount:
            _asInt(data['moved_processed_images_count']) ?? 0,
      ),
      exportedFilePath: outputServerPath,
      clearError: true,
    );
    notifyListeners();
  }

  Future<bool> downloadExportedFile() async {
    final id = _jobId;
    if (id == null) {
      _job = _job.copyWith(errorMessage: 'معرف المهمة غير موجود.');
      notifyListeners();
      return false;
    }
    try {
      final bytes = await _api.downloadTiff(id);
      _job = _job.copyWith(exportedFileBytes: bytes, clearError: true);
      notifyListeners();
      return true;
    } catch (error) {
      _job = _job.copyWith(
        errorMessage:
            'تم إنشاء الملف على السيرفر، لكن تعذر تنزيله الآن: $error',
      );
      notifyListeners();
      return false;
    }
  }

  void backToUpload() {
    _stopProgressStreaming();
    _job = _job.copyWith(
      stage: NestingJobStage.upload,
      clearError: true,
      clearComputeResult: true,
      clearQaReport: true,
      clearExportedFile: true,
    );
    unawaited(_persistManifest());
    notifyListeners();
  }

  void startNewJob() => resetJob();

  static int? _asInt(dynamic value) =>
      value is int ? value : int.tryParse(value?.toString() ?? '');
  static double _asDouble(dynamic value) =>
      double.tryParse(value?.toString() ?? '') ?? 0;
  static double? _asDoubleNullable(dynamic value) =>
      value == null ? null : double.tryParse(value.toString());

  @override
  void dispose() {
    _stopProgressStreaming();
    super.dispose();
  }
}
