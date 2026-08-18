import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../core/theme/app_theme.dart';
import '../providers/server_provider.dart';

class ServerLogsSheet extends StatefulWidget {
  const ServerLogsSheet({super.key});

  @override
  State<ServerLogsSheet> createState() => _ServerLogsSheetState();
}

class _ServerLogsSheetState extends State<ServerLogsSheet> {
  final ScrollController _scrollController = ScrollController();

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  void _scrollToBottom() {
    if (_scrollController.hasClients) {
      _scrollController.animateTo(
        _scrollController.position.maxScrollExtent,
        duration: const Duration(milliseconds: 300),
        curve: Curves.easeOut,
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final serverProvider = context.watch<ServerProvider>();

    // Scroll to bottom whenever logs update (using post frame to allow list to build)
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _scrollToBottom();
    });

    return Container(
      height: MediaQuery.of(context).size.height * 0.7,
      decoration: BoxDecoration(
        color: const Color(0xFF1E1E1E),
        borderRadius: const BorderRadius.vertical(top: Radius.circular(22)),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.35),
            blurRadius: 28,
            offset: const Offset(0, -6),
          ),
        ],
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _buildHeader(context, serverProvider),
          Expanded(
            child: _buildLogsView(serverProvider.logs),
          ),
        ],
      ),
    );
  }

  Widget _buildHeader(BuildContext context, ServerProvider provider) {
    Color statusColor;
    String statusText;

    switch (provider.state) {
      case ServerState.running:
        statusColor = AppColors.success;
        statusText = 'متصل (Running)';
        break;
      case ServerState.stopped:
        statusColor = AppColors.slate400;
        statusText = 'متوقف (Stopped)';
        break;
      case ServerState.starting:
        statusColor = AppColors.warning;
        statusText = 'جاري التشغيل (Starting)';
        break;
      case ServerState.error:
        statusColor = AppColors.danger;
        statusText = 'خطأ (Error)';
        break;
    }

    return Container(
      padding: const EdgeInsets.fromLTRB(16, 14, 12, 14),
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: Color(0xFF2E2E2E))),
      ),
      child: Row(
        children: [
          Container(
            width: 32,
            height: 32,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.06),
              borderRadius: BorderRadius.circular(9),
            ),
            child: Icon(Icons.terminal_rounded, color: Colors.white.withValues(alpha: 0.85), size: 18),
          ),
          const SizedBox(width: 10),
          const Text(
            'سجلات السيرفر',
            style: TextStyle(
              color: Colors.white,
              fontWeight: FontWeight.w700,
              fontSize: 15.5,
            ),
          ),
          const SizedBox(width: 14),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
            decoration: BoxDecoration(
              color: statusColor.withValues(alpha: 0.16),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: statusColor.withValues(alpha: 0.45)),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  width: 8,
                  height: 8,
                  decoration: BoxDecoration(shape: BoxShape.circle, color: statusColor),
                ),
                const SizedBox(width: 6),
                Text(
                  statusText,
                  style: TextStyle(color: statusColor, fontSize: 12, fontWeight: FontWeight.w600),
                ),
              ],
            ),
          ),
          const Spacer(),
          IconButton(
            icon: const Icon(Icons.copy_rounded, color: Colors.white70),
            tooltip: 'نسخ جميع السجلات',
            onPressed: () {
              Clipboard.setData(ClipboardData(text: provider.logs.join('\n')));
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(
                  content: Text('تم نسخ السجلات للحافظة'),
                  duration: Duration(seconds: 2),
                ),
              );
            },
          ),
          IconButton(
            icon: const Icon(Icons.refresh_rounded, color: Colors.white70),
            tooltip: 'إعادة التشغيل',
            onPressed: () => provider.restartServer(),
          ),
          IconButton(
            icon: const Icon(Icons.power_settings_new_rounded, color: AppColors.danger),
            tooltip: 'إيقاف',
            onPressed: () => provider.stopServer(),
          ),
          const SizedBox(width: 8),
          IconButton(
            icon: const Icon(Icons.close_rounded, color: Colors.white70),
            onPressed: () => Navigator.of(context).pop(),
          ),
        ],
      ),
    );
  }

  Widget _buildLogsView(List<String> logs) {
    return Container(
      color: const Color(0xFF0D0D0D),
      child: ListView.builder(
        controller: _scrollController,
        padding: const EdgeInsets.all(12),
        itemCount: logs.length,
        itemBuilder: (context, index) {
          final line = logs[index];
          final isError = line.startsWith('[ERROR]');

          return Container(
            margin: const EdgeInsets.only(bottom: 4),
            padding: isError
                ? const EdgeInsets.symmetric(horizontal: 8, vertical: 3)
                : EdgeInsets.zero,
            decoration: isError
                ? BoxDecoration(
                    color: AppColors.danger.withValues(alpha: 0.10),
                    borderRadius: BorderRadius.circular(5),
                  )
                : null,
            child: SelectableText(
              line,
              style: TextStyle(
                fontFamily: 'monospace',
                fontSize: 12,
                color: isError ? AppColors.danger : Colors.white.withValues(alpha: 0.8),
              ),
              textDirection: TextDirection.ltr, // Logs should be LTR
            ),
          );
        },
      ),
    );
  }
}
