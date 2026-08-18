import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../services/nesting_api.dart';

enum ServerState { stopped, starting, running, error }

class ServerProvider extends ChangeNotifier {
  ServerProvider() {
    if (_canStartLocalProcess) {
      unawaited(startServer());
    } else {
      _state = ServerState.stopped;
    }
  }

  Process? _process;
  ServerState _state = ServerState.stopped;
  final List<String> _logs = <String>[];

  static bool get _canStartLocalProcess => !kIsWeb && (Platform.isMacOS || Platform.isWindows || Platform.isLinux);

  ServerState get state => _state;
  List<String> get logs => List.unmodifiable(_logs);
  bool get canManageProcess => _canStartLocalProcess;

  void _addLog(String log) {
    final value = log.trim();
    if (value.isEmpty) return;
    _logs.add(value);
    // Logs are diagnostic only; keeping hundreds of long upload filenames in
    // memory offers no value during a large layout calculation.
    if (_logs.length > 300) _logs.removeRange(0, _logs.length - 300);
    notifyListeners();
  }

  Future<void> _freePort8000() async {
    try {
      if (Platform.isWindows) {
        await Process.run('cmd', ['/c', 'for /f "tokens=5" %a in (\'netstat -aon ^| findstr :8000\') do taskkill /F /PID %a']);
      } else {
        final result = await Process.run('lsof', ['-ti:8000']);
        final pids = result.stdout.toString().trim().split(RegExp(r'\s+')).where((e) => e.isNotEmpty);
        for (final pid in pids) {
          await Process.run('kill', ['-9', pid]);
        }
      }
    } catch (_) {}
  }

  /// Locate the bundled nesting_server executable shipped alongside the
  /// Flutter desktop application.  The search order is:
  ///
  /// 1. **macOS .app bundle**: `Runner.app/Contents/Resources/nesting_server/nesting_server`
  /// 2. **Windows**: `<exe_dir>/nesting_server/nesting_server.exe`
  /// 3. **Linux**: `<exe_dir>/nesting_server/nesting_server`
  /// 4. **Development fallback**: `../backend/dist/nesting_server/nesting_server`
  ///    (for running `flutter run` during development after a PyInstaller build).
  /// 5. **Legacy fallback**: `.venv/bin/python -m uvicorn` (for development
  ///    without a PyInstaller build — requires a Python venv).
  String? _findServerExecutable() {
    final sep = Platform.pathSeparator;
    final exeName = Platform.isWindows ? 'nesting_server.exe' : 'nesting_server';

    final candidates = <String>[];

    // --- Bundled executable paths ---
    if (Platform.resolvedExecutable.isNotEmpty) {
      final binDir = File(Platform.resolvedExecutable).parent.path;

      if (Platform.isMacOS) {
        // Inside .app bundle: Runner.app/Contents/MacOS/ -> ../Resources/
        candidates.add('$binDir${sep}..${sep}Resources${sep}nesting_server${sep}$exeName');
        candidates.add('$binDir${sep}nesting_server${sep}$exeName');
      } else if (Platform.isWindows) {
        candidates.add('$binDir${sep}nesting_server${sep}$exeName');
        candidates.add('$binDir${sep}..${sep}nesting_server${sep}$exeName');
      } else {
        // Linux
        candidates.add('$binDir${sep}nesting_server${sep}$exeName');
        candidates.add('$binDir${sep}..${sep}lib${sep}nesting_server${sep}$exeName');
      }
    }

    // Development: the PyInstaller output sits in backend/dist/nesting_server/
    candidates.add('${Directory.current.path}${sep}..${sep}backend${sep}dist${sep}nesting_server${sep}$exeName');
    candidates.add('${Directory.current.path}${sep}backend${sep}dist${sep}nesting_server${sep}$exeName');

    for (final candidate in candidates) {
      final file = File(candidate);
      if (file.existsSync()) return file.absolute.path;
    }
    return null;
  }

  /// Legacy fallback: find a Python venv and return the python path plus the
  /// backend directory.  Used only during development when the developer has
  /// not built the PyInstaller bundle yet.
  (String pythonPath, String backendDir)? _findLegacyPythonBackend() {
    final sep = Platform.pathSeparator;
    final candidates = <String>[
      '${Directory.current.path}$sep..${sep}backend',
      '${Directory.current.path}${sep}backend',
    ];
    if (Platform.resolvedExecutable.isNotEmpty) {
      final binDir = File(Platform.resolvedExecutable).parent.path;
      candidates.add('$binDir$sep..${sep}backend');
    }
    for (final candidate in candidates) {
      final dir = Directory(candidate);
      if (dir.existsSync() && File('${dir.path}${sep}pyproject.toml').existsSync()) {
        final pythonPath = Platform.isWindows
            ? '${dir.absolute.path}$sep.venv${sep}Scripts${sep}python.exe'
            : '${dir.absolute.path}$sep.venv${sep}bin${sep}python';
        if (File(pythonPath).existsSync()) {
          return (pythonPath, dir.absolute.path);
        }
      }
    }
    return null;
  }

  Future<void> startServer() async {
    if (!_canStartLocalProcess) {
      _state = ServerState.stopped;
      _addLog('التشغيل المحلي غير متاح على هذه المنصة؛ استخدم FastAPI خارجيًا.');
      return;
    }
    if (_state == ServerState.starting || _state == ServerState.running) return;

    _state = ServerState.starting;
    notifyListeners();
    _addLog('--- بدء تشغيل backend ---');

    try {
      await _freePort8000();

      // Prefer the bundled PyInstaller executable, fall back to legacy venv.
      final serverExe = _findServerExecutable();

      if (serverExe != null) {
        _addLog('وجد nesting_server المدمج: $serverExe');
        _process = await Process.start(
          serverExe,
          const [],
          workingDirectory: File(serverExe).parent.path,
          runInShell: false,
        );
      } else {
        // Legacy development fallback
        final legacy = _findLegacyPythonBackend();
        if (legacy == null) {
          throw StateError(
            'لم يتم العثور على nesting_server المدمج أو مجلد backend بجوار التطبيق.\n'
            'لبناء الـ backend كـ executable شغّل: cd backend && python build_backend.py',
          );
        }
        final (pythonPath, backendDir) = legacy;
        _addLog('وضع التطوير: استخدام Python venv من $backendDir');
        _process = await Process.start(
          pythonPath,
          const ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000', '--no-access-log'],
          workingDirectory: backendDir,
          runInShell: true,
        );
      }

      _state = ServerState.running;
      notifyListeners();
      _listen(_process!.stdout, false);
      _listen(_process!.stderr, true);
      unawaited(_process!.exitCode.then((code) {
        _state = code == 0 ? ServerState.stopped : ServerState.error;
        _addLog('--- backend توقف (code=$code) ---');
        _process = null;
        notifyListeners();
      }));

      // Startup timing varies by machine.  Retry the one health probe a few
      // times instead of showing a false warning while Uvicorn is still
      // binding the socket; this is startup-only, not periodic polling.
      var healthy = false;
      for (var attempt = 0; attempt < 5 && !healthy; attempt++) {
        await Future<void>.delayed(const Duration(milliseconds: 250));
        try {
          await NestingApiClient().healthCheck(timeout: const Duration(seconds: 1));
          healthy = true;
        } catch (_) {}
      }
      _addLog(healthy
          ? '--- backend جاهز على http://127.0.0.1:8000 ---'
          : '[WARNING] السيرفر بدأ لكن health check لم ينجح بعد.');
    } catch (error) {
      _state = ServerState.error;
      _addLog('[ERROR] فشل تشغيل backend: $error');
      notifyListeners();
    }
  }

  void _listen(Stream<List<int>> stream, bool stderr) {
    stream.transform(utf8.decoder).listen((data) {
      for (final line in data.split('\n')) {
        final trimmed = line.trim();
        if (trimmed.isEmpty) continue;
        _addLog(stderr ? '[STDERR] $trimmed' : trimmed);
      }
    });
  }

  Future<void> stopServer() async {
    final process = _process;
    if (process == null) return;
    _addLog('--- إيقاف backend ---');
    process.kill(ProcessSignal.sigterm);
    await Future<void>.delayed(const Duration(milliseconds: 250));
    _process = null;
    _state = ServerState.stopped;
    notifyListeners();
  }

  Future<void> restartServer() async {
    await stopServer();
    await Future<void>.delayed(const Duration(milliseconds: 350));
    await startServer();
  }
}
