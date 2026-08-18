import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/theme/app_theme.dart';
import '../models/nesting_job.dart';
import '../providers/nesting_job_provider.dart';
import '../services/export_file_saver.dart';
import '../widgets/violation_list_tile.dart';
import '../widgets/workflow_stepper.dart';

class ExportScreen extends StatefulWidget {
  const ExportScreen({super.key});

  @override
  State<ExportScreen> createState() => _ExportScreenState();
}

class _ExportScreenState extends State<ExportScreen> {
  bool _dialogShown = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final provider = context.read<NestingJobProvider>();
      if (provider.job.stage == NestingJobStage.proofPreview && !_dialogShown) {
        _dialogShown = true;
        _askFolderNameThenExport(provider);
      }
    });
  }

  Future<void> _askFolderNameThenExport(NestingJobProvider provider) async {
    // Only ask for folder name if processedImagesPath is configured.
    final hasProcessedPath =
        provider.job.settings.processedImagesPath.trim().isNotEmpty;

    String? folderName;
    if (hasProcessedPath && mounted) {
      folderName = await showDialog<String>(
        context: context,
        barrierDismissible: false,
        builder: (context) => const _FolderNameDialog(),
      );
      // null means the user cancelled the dialog entirely.
      if (folderName == null && mounted) {
        Navigator.of(context).pop();
        return;
      }
    }

    provider.confirmAndExport(folderName: folderName);
  }

  Future<void> _saveCopy(NestingJob job) async {
    final bytes = job.exportedFileBytes;
    if (bytes == null || bytes.isEmpty) return;
    try {
      final saved = await saveExportedTiff(
        bytes: bytes,
        fileName: 'sheet_layout_${DateTime.now().millisecondsSinceEpoch}.tiff',
      );
      if (!saved || !mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('تم حفظ ملف TIFF بنجاح.')));
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('فشل حفظ الملف: $error')));
    }
  }

  @override
  Widget build(BuildContext context) {
    final provider = context.watch<NestingJobProvider>();
    final job = provider.job;
    return PopScope(
      canPop: job.stage != NestingJobStage.exporting,
      onPopInvokedWithResult: (didPop, result) {
        if (didPop) return;
        if (job.stage != NestingJobStage.exporting) Navigator.of(context).pop();
      },
      child: Scaffold(
        appBar: AppBar(
          title: const Text('التصدير والتحقق النهائي'),
          automaticallyImplyLeading: job.stage != NestingJobStage.exporting,
        ),
        body: Column(
          children: [
            const Padding(
              padding: EdgeInsets.fromLTRB(20, 12, 20, 4),
              child: WorkflowStepper(currentStage: NestingJobStage.completed),
            ),
            Expanded(
              child: AnimatedSwitcher(
                duration: AppMotion.base,
                child: _buildBody(job, provider),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildBody(NestingJob job, NestingJobProvider provider) {
    switch (job.stage) {
      case NestingJobStage.exporting:
        return _ExportingState(key: const ValueKey('exporting'), provider: provider);
      case NestingJobStage.completed:
        return _CompletedState(
          key: const ValueKey('completed'),
          job: job,
          onSave: () => _saveCopy(job),
          onDownloadAgain: () => provider.downloadExportedFile(),
          onNew: () {
            provider.startNewJob();
            Navigator.of(context).popUntil((route) => route.isFirst);
          },
        );
      case NestingJobStage.failed:
        return _FailedState(
          key: const ValueKey('failed'),
          message: job.errorMessage ?? 'حدث خطأ غير متوقع',
          onRetry: () => _askFolderNameThenExport(provider),
        );
      default:
        return _ExportingState(key: const ValueKey('exporting'), provider: provider);
    }
  }
}

/// Dialog to ask the user for a folder name before exporting.
class _FolderNameDialog extends StatefulWidget {
  const _FolderNameDialog();

  @override
  State<_FolderNameDialog> createState() => _FolderNameDialogState();
}

class _FolderNameDialogState extends State<_FolderNameDialog> {
  final _controller = TextEditingController();
  String? _errorText;

  String get _preview {
    final name = _controller.text.trim();
    final now = DateTime.now();
    final timestamp =
        '${now.year}-${now.month.toString().padLeft(2, '0')}-${now.day.toString().padLeft(2, '0')}'
        '_${now.hour.toString().padLeft(2, '0')}-${now.minute.toString().padLeft(2, '0')}-${now.second.toString().padLeft(2, '0')}';
    if (name.isEmpty) return timestamp;
    return '${name}_$timestamp';
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('اسم مجلد الصور'),
      content: SizedBox(
        width: 360,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'اكتب اسمًا للمجلد اللي هيتحفظ فيه الصور.\n'
              'التاريخ هيتضاف تلقائيًا بجانب الاسم.',
              style: TextStyle(
                fontSize: 13,
                color: AppColors.slate600,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 14),
            TextField(
              controller: _controller,
              autofocus: true,
              decoration: InputDecoration(
                labelText: 'اسم المجلد',
                hintText: 'مثال: طلبية_أحمد',
                errorText: _errorText,
                errorMaxLines: 2,
              ),
              onChanged: (_) => setState(() => _errorText = null),
              onSubmitted: (_) => _submit(),
            ),
            const SizedBox(height: 10),
            ListenableBuilder(
              listenable: _controller,
              builder: (context, _) {
                return Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: AppColors.slate50,
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(color: AppColors.slate200),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text(
                        'اسم المجلد النهائي:',
                        style: TextStyle(
                          fontSize: 11,
                          color: AppColors.slate500,
                        ),
                      ),
                      const SizedBox(height: 3),
                      Text(
                        _preview,
                        style: const TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w700,
                          fontFamily: 'monospace',
                          color: AppColors.primary,
                        ),
                      ),
                      const SizedBox(height: 6),
                      const Text(
                        'placed/  ← الصور المرتبة\nunplaced/  ← الصور غير المرتبة',
                        style: TextStyle(
                          fontSize: 11,
                          color: AppColors.slate500,
                          fontFamily: 'monospace',
                          height: 1.6,
                        ),
                      ),
                    ],
                  ),
                );
              },
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(null),
          child: const Text('إلغاء'),
        ),
        ElevatedButton(
          onPressed: _submit,
          child: const Text('تأكيد وتصدير'),
        ),
      ],
    );
  }

  void _submit() {
    final name = _controller.text.trim();
    // Empty name is allowed — timestamp-only folder.
    // But check for invalid characters.
    if (name.contains(RegExp(r'[/\\]'))) {
      setState(() => _errorText = 'الاسم لا يمكن أن يحتوي على / أو \\');
      return;
    }
    Navigator.of(context).pop(name);
  }
}

class _ExportingState extends StatelessWidget {
  const _ExportingState({super.key, required this.provider});
  final NestingJobProvider provider;

  @override
  Widget build(BuildContext context) {
    final done = provider.exportProgressDone;
    final total = provider.exportProgressTotal;
    final message = provider.exportProgressMessage;
    final hasFraction = done != null && total != null && total > 0;
    final fraction = hasFraction ? (done / total).clamp(0.0, 1.0) : null;
    final percentLabel = fraction == null ? null : '${(fraction * 100).round()}%';

    return Center(
      child: Padding(
        padding: const EdgeInsets.all(28),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 430),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (fraction == null)
                const SizedBox(
                  width: 54,
                  height: 54,
                  child: CircularProgressIndicator(strokeWidth: 3),
                )
              else
                Column(
                  children: [
                    ClipRRect(
                      borderRadius: BorderRadius.circular(99),
                      child: LinearProgressIndicator(
                        value: fraction,
                        minHeight: 8,
                        backgroundColor: AppColors.slate200,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      percentLabel!,
                      style: const TextStyle(
                        fontSize: 13,
                        fontWeight: FontWeight.w800,
                        color: AppColors.primary,
                      ),
                    ),
                  ],
                ),
              const SizedBox(height: 18),
              const Text(
                'جاري إنشاء TIFF وتشغيل الفحص النهائي',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w800),
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 8),
              Text(
                message ??
                    'الباك إند يعيد فحص الـlayout قبل الـrasterization ثم يتحقق من الأبعاد والـDPI والـICC والـclearance.',
                style: const TextStyle(
                  color: AppColors.slate500,
                  fontSize: 12.2,
                  height: 1.5,
                ),
                textAlign: TextAlign.center,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _CompletedState extends StatelessWidget {
  const _CompletedState({
    super.key,
    required this.job,
    required this.onSave,
    required this.onDownloadAgain,
    required this.onNew,
  });
  final NestingJob job;
  final VoidCallback onSave;
  final VoidCallback onDownloadAgain;
  final VoidCallback onNew;

  @override
  Widget build(BuildContext context) {
    final report = job.qaReport;
    final accepted = report?.isValid ?? false;
    final result = job.computeResult;
    return ListView(
      padding: const EdgeInsets.fromLTRB(18, 18, 18, 28),
      children: [
        Container(
          padding: const EdgeInsets.all(22),
          decoration: BoxDecoration(
            color: (accepted ? AppColors.success : AppColors.warning)
                .withValues(alpha: .06),
            borderRadius: BorderRadius.circular(18),
            border: Border.all(
              color: (accepted ? AppColors.success : AppColors.warning)
                  .withValues(alpha: .26),
            ),
          ),
          child: Column(
            children: [
              Container(
                width: 66,
                height: 66,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: (accepted ? AppColors.success : AppColors.warning)
                      .withValues(alpha: .12),
                  shape: BoxShape.circle,
                ),
                child: Icon(
                  accepted ? Icons.check_rounded : Icons.warning_amber_rounded,
                  color: accepted ? AppColors.success : AppColors.warning,
                  size: 34,
                ),
              ),
              const SizedBox(height: 14),
              Text(
                accepted ? 'تم التصدير والتحقق بنجاح' : 'تم التصدير مع ملاحظات',
                style: TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.w900,
                  color: accepted ? AppColors.success : AppColors.warning,
                ),
              ),
              const SizedBox(height: 7),
              Text(
                result?.layoutMessage ?? 'اكتمل التصدير.',
                textAlign: TextAlign.center,
                style: const TextStyle(
                  color: AppColors.slate600,
                  fontSize: 12.5,
                  height: 1.5,
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 14),
        if (result != null)
          Row(
            children: [
              Expanded(
                child: _MiniResult(
                  value: '${result.placedCount}',
                  label: 'مرتبة',
                  color: AppColors.success,
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: _MiniResult(
                  value: '${result.sheetCount}',
                  label: 'ورقة TIFF',
                  color: AppColors.info,
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: _MiniResult(
                  value: '${report?.widthPx ?? 0}px',
                  label: 'عرض التصدير',
                  color: AppColors.info,
                ),
              ),
            ],
          ),
        const SizedBox(height: 14),
        if (report != null) ...[
          const Text(
            'نتيجة QA',
            style: TextStyle(fontWeight: FontWeight.w800, fontSize: 14),
          ),
          const SizedBox(height: 8),
          _QaRow(label: 'الأبعاد', pass: report.checkedDimension),
          _QaRow(
            label: 'DPI (${report.dpi.toStringAsFixed(0)})',
            pass: report.checkedDpi,
          ),
          _QaRow(label: 'ICC / Color Mode', pass: report.checkedIccAndMode),
          _QaRow(
            label: 'طبقات قابلة للتحرير (${report.layerCount})',
            pass: report.checkedLayers,
          ),
          _QaRow(
            label: 'Clearance',
            pass: !report.violations.any(
              (v) =>
                  v.severity == 'clearance_violation' ||
                  v.severity == 'overlap',
            ),
          ),
          if (report.movedProcessedImagesCount > 0) ...[
            const SizedBox(height: 10),
            _ArchiveNotice(
              count: report.movedProcessedImagesCount,
              directory: report.processedImagesDirectory,
            ),
          ],
        ],
        if (report != null && report.violations.isNotEmpty) ...[
          const SizedBox(height: 12),
          const Text(
            'مخالفات QA',
            style: TextStyle(
              color: AppColors.danger,
              fontWeight: FontWeight.w800,
              fontSize: 14,
            ),
          ),
          const SizedBox(height: 7),
          ...report.violations.map((v) => ViolationListTile(violation: v)),
        ],
        const SizedBox(height: 18),
        if (job.errorMessage != null) ...[
          _DownloadWarning(message: job.errorMessage!),
          const SizedBox(height: 10),
        ],
        ElevatedButton.icon(
          onPressed: job.exportedFileBytes == null ? onDownloadAgain : onSave,
          icon: Icon(
            job.exportedFileBytes == null
                ? Icons.download_rounded
                : Icons.save_alt_rounded,
          ),
          label: Text(
            job.exportedFileBytes == null
                ? 'إعادة تنزيل ملف TIFF'
                : 'حفظ ملف TIFF على الجهاز',
          ),
        ),
        const SizedBox(height: 10),
        OutlinedButton.icon(
          onPressed: onNew,
          icon: const Icon(Icons.add_rounded),
          label: const Text('بدء مهمة جديدة'),
        ),
      ],
    );
  }
}

class _DownloadWarning extends StatelessWidget {
  const _DownloadWarning({required this.message});
  final String message;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(12),
    decoration: BoxDecoration(
      color: AppColors.warning.withValues(alpha: .07),
      borderRadius: BorderRadius.circular(10),
      border: Border.all(color: AppColors.warning.withValues(alpha: .20)),
    ),
    child: Row(
      children: [
        const Icon(Icons.cloud_off_rounded, color: AppColors.warning, size: 18),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            message,
            style: const TextStyle(
              color: AppColors.slate700,
              fontSize: 11.8,
              height: 1.4,
            ),
          ),
        ),
      ],
    ),
  );
}

class _ArchiveNotice extends StatelessWidget {
  const _ArchiveNotice({required this.count, required this.directory});

  final int count;
  final String? directory;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(12),
    decoration: BoxDecoration(
      color: AppColors.success.withValues(alpha: .06),
      borderRadius: BorderRadius.circular(10),
      border: Border.all(color: AppColors.success.withValues(alpha: .20)),
    ),
    child: Row(
      children: [
        const Icon(Icons.drive_file_move_rounded, color: AppColors.success, size: 18),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            directory == null || directory!.isEmpty
                ? 'تم نقل $count صورة أصلية بعد نجاح الفحص.'
                : 'تم نقل $count صورة أصلية إلى: $directory',
            style: const TextStyle(
              color: AppColors.slate700,
              fontSize: 11.8,
              height: 1.4,
            ),
          ),
        ),
      ],
    ),
  );
}

class _MiniResult extends StatelessWidget {
  const _MiniResult({
    required this.value,
    required this.label,
    required this.color,
  });
  final String value;
  final String label;
  final Color color;
  @override
  Widget build(BuildContext context) => Card(
    child: Padding(
      padding: const EdgeInsets.symmetric(vertical: 12),
      child: Column(
        children: [
          Text(
            value,
            style: TextStyle(
              color: color,
              fontSize: 15,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 3),
          Text(
            label,
            style: const TextStyle(color: AppColors.slate500, fontSize: 10.5),
          ),
        ],
      ),
    ),
  );
}

class _QaRow extends StatelessWidget {
  const _QaRow({required this.label, required this.pass});
  final String label;
  final bool pass;
  @override
  Widget build(BuildContext context) => Container(
    margin: const EdgeInsets.only(bottom: 6),
    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
    decoration: BoxDecoration(
      color: pass
          ? AppColors.success.withValues(alpha: .045)
          : AppColors.danger.withValues(alpha: .06),
      borderRadius: BorderRadius.circular(10),
      border: Border.all(
        color: (pass ? AppColors.success : AppColors.danger).withValues(
          alpha: .16,
        ),
      ),
    ),
    child: Row(
      children: [
        Icon(
          pass ? Icons.check_circle_rounded : Icons.cancel_rounded,
          size: 17,
          color: pass ? AppColors.success : AppColors.danger,
        ),
        const SizedBox(width: 8),
        Text(
          label,
          style: TextStyle(
            fontSize: 12.2,
            fontWeight: FontWeight.w700,
            color: pass ? AppColors.slate700 : AppColors.danger,
          ),
        ),
      ],
    ),
  );
}

class _FailedState extends StatelessWidget {
  const _FailedState({super.key, required this.message, required this.onRetry});
  final String message;
  final VoidCallback onRetry;
  @override
  Widget build(BuildContext context) => Center(
    child: Padding(
      padding: const EdgeInsets.all(28),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(
            Icons.error_outline_rounded,
            color: AppColors.danger,
            size: 48,
          ),
          const SizedBox(height: 14),
          Text(
            message,
            textAlign: TextAlign.center,
            style: const TextStyle(
              color: AppColors.slate700,
              fontSize: 13,
              height: 1.5,
            ),
          ),
          const SizedBox(height: 20),
          ElevatedButton.icon(
            onPressed: onRetry,
            icon: const Icon(Icons.refresh_rounded),
            label: const Text('إعادة التصدير'),
          ),
        ],
      ),
    ),
  );
}
