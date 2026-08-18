import 'package:flutter/foundation.dart';

enum ServerState { stopped, starting, running, error }

class ServerProvider extends ChangeNotifier {
  final ServerState _state = ServerState.stopped;
  final List<String> _logs = <String>[];

  ServerState get state => _state;
  List<String> get logs => List.unmodifiable(_logs);
  bool get canManageProcess => false;

  Future<void> startServer() async {}
  Future<void> stopServer() async {}
  Future<void> restartServer() async {}
}
