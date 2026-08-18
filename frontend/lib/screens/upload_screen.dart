import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/theme/app_theme.dart';
import '../models/nesting_job.dart';
import '../models/sheet_part.dart';
import '../providers/nesting_job_provider.dart';
import '../services/image_folder_picker.dart';
import '../widgets/workflow_stepper.dart';
import 'preview_screen.dart';
import 'settings_sheet.dart';
import 'server_logs_sheet.dart';

class UploadScreen extends StatelessWidget {
  const UploadScreen({super.key});

  Future<void> _pickFiles(BuildContext context) async {
    final provider = context.read<NestingJobProvider>();
    final result = await FilePicker.pickFiles(
      // Do not block normal image formats at the operating-system picker.
      // The backend decodes and normalizes readable raster images to RGBA.
      type: FileType.any,
    );
    if (result.isEmpty) return;

    final base = DateTime.now().microsecondsSinceEpoch;
    final files = <UploadedPart>[];
    for (final entry in result.asMap().entries) {
      final file = entry.value;
      files.add(
        UploadedPart(
          id: '${base}_${entry.key}_${file.name}',
          fileName: file.name,
          filePath: file.path ?? '',
          originalSourcePath: file.path,
          // Desktop/mobile upload from the picked path. Web has no durable
          // local path, so load each selected file only when it is needed.
          bytes: kIsWeb ? await file.readAsBytes() : null,
        ),
      );
    }

    provider.addUploadedParts(files);
  }

  Future<void> _pickFolder(BuildContext context) async {
    final provider = context.read<NestingJobProvider>();
    final messenger = ScaffoldMessenger.of(context);
    if (kIsWeb) {
      messenger.showSnackBar(
        const SnackBar(
          content: Text('اختيار مجلد كامل متاح في تطبيقات سطح المكتب فقط.'),
        ),
      );
      return;
    }
    final folder = await FilePicker.getDirectoryPath();
    if (folder == null || folder.isEmpty) return;
    final sourcePaths = await imagePathsInFolder(folder);
    if (sourcePaths.isEmpty) {
      messenger.showSnackBar(
        const SnackBar(
          content: Text('لم نجد صورًا مدعومة مباشرة داخل المجلد المحدد.'),
        ),
      );
      return;
    }

    final base = DateTime.now().microsecondsSinceEpoch;
    provider.addUploadedParts([
      for (final entry in sourcePaths.asMap().entries)
        UploadedPart(
          id: '${base}_${entry.key}_${entry.value}',
          fileName: entry.value.split(RegExp(r'[/\\]')).last,
          filePath: entry.value,
          originalSourcePath: entry.value,
        ),
    ]);
  }

  @override
  Widget build(BuildContext context) {
    final provider = context.watch<NestingJobProvider>();
    final job = provider.job;
    final valid = job.validParts.length;
    final rejected = job.rejectedParts.length;
    final pending = job.uploadedParts.where((p) => p.isPending).length;
    final canProceed = job.canProceedToCompute;

    return Scaffold(
      appBar: AppBar(
        title: const Text('تجهيز الصور'),
        actions: [
          _ServerBadge(provider: provider),
          IconButton(
            tooltip: 'إعدادات الشيت',
            icon: const Icon(Icons.tune_rounded),
            onPressed: () => showModalBottomSheet<void>(
              context: context,
              isScrollControlled: true,
              backgroundColor: Colors.transparent,
              builder: (_) => const SettingsSheet(),
            ),
          ),
          IconButton(
            tooltip: 'سجلات السيرفر',
            icon: const Icon(Icons.terminal_rounded),
            onPressed: () => showModalBottomSheet<void>(
              context: context,
              isScrollControlled: true,
              backgroundColor: Colors.transparent,
              builder: (_) => const ServerLogsSheet(),
            ),
          ),
        ],
      ),
      body: Column(
        children: [
          const Padding(
            padding: EdgeInsets.fromLTRB(20, 12, 20, 4),
            child: WorkflowStepper(currentStage: NestingJobStage.upload),
          ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
              children: [
                _HeaderBlock(
                  total: job.uploadedParts.length,
                  valid: valid,
                  rejected: rejected,
                  pending: pending,
                ),
                if (provider.hasResumableJob && !provider.initializing) ...[
                  const SizedBox(height: 12),
                  _ResumeBanner(
                    provider: provider,
                    total: job.uploadedParts.length,
                    pending: pending,
                  ),
                ],
                const SizedBox(height: 14),
                _DropCard(
                  onPick: () => _pickFiles(context),
                  onPickFolder: () => _pickFolder(context),
                ),
                if (provider.uploading) ...[
                  const SizedBox(height: 12),
                  _UploadProgressCard(progress: provider.uploadProgress),
                ],
                if (job.errorMessage != null) ...[
                  const SizedBox(height: 12),
                  _ErrorBanner(message: job.errorMessage!),
                ],
                if (rejected > 0) ...[
                  const SizedBox(height: 12),
                  _RejectedBanner(
                    count: rejected,
                    onRecheck: provider.refreshCurrentJob,
                  ),
                ],
                if (job.uploadedParts.isNotEmpty) ...[
                  const SizedBox(height: 14),
                  Row(
                    children: [
                      const Expanded(
                        child: Text(
                          'الصور المحددة',
                          style: TextStyle(
                            fontWeight: FontWeight.w800,
                            fontSize: 14,
                          ),
                        ),
                      ),
                      if (job.uploadedParts.isNotEmpty)
                        TextButton.icon(
                          onPressed: provider.uploading
                              ? null
                              : provider.clearAllParts,
                          icon: const Icon(
                            Icons.delete_sweep_outlined,
                            size: 18,
                          ),
                          label: const Text('مسح الكل'),
                        ),
                    ],
                  ),
                  const SizedBox(height: 6),
                  ...job.uploadedParts.map(
                    (part) => Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: _PartCard(
                        part: part,
                        onRemove: () => provider.removeUploadedPart(part.id),
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ),
          _BottomBar(
            total: job.uploadedParts.length,
            canProceed: canProceed,
            busy: provider.uploading || pending > 0,
            onAddMore: () => _pickFiles(context),
            onProceed: () => Navigator.of(context).push(
              MaterialPageRoute<void>(builder: (_) => const PreviewScreen()),
            ),
          ),
        ],
      ),
    );
  }
}

class _ResumeBanner extends StatelessWidget {
  const _ResumeBanner({
    required this.provider,
    required this.total,
    required this.pending,
  });

  final NestingJobProvider provider;
  final int total;
  final int pending;

  @override
  Widget build(BuildContext context) {
    final done = total - pending;
    return Card(
      margin: EdgeInsets.zero,
      color: AppColors.info.withValues(alpha: .06),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Row(
          children: [
            Container(
              width: 42,
              height: 42,
              decoration: BoxDecoration(
                color: AppColors.info.withValues(alpha: .12),
                borderRadius: BorderRadius.circular(12),
              ),
              child: const Icon(Icons.restore_rounded, color: AppColors.info),
            ),
            const SizedBox(width: 11),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    'مهمة محفوظة — يمكن الاستكمال بأمان',
                    style: TextStyle(fontWeight: FontWeight.w800, fontSize: 13),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    pending > 0
                        ? 'تم حفظ وتحليل $done من $total صورة. المتبقي $pending صورة، والاستكمال يتم من آخر نقطة محفوظة.'
                        : 'كل الصور محفوظة على السيرفر ويمكن متابعة الخطوة التالية بدون إعادة الرفع.',
                    style: const TextStyle(
                      color: AppColors.slate600,
                      fontSize: 11.5,
                      height: 1.45,
                    ),
                  ),
                ],
              ),
            ),
            if (pending > 0) ...[
              const SizedBox(width: 8),
              FilledButton.tonal(
                onPressed: provider.uploading
                    ? null
                    : provider.resumePendingUploads,
                child: Text(provider.uploading ? 'جاري...' : 'استكمال'),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _ServerBadge extends StatelessWidget {
  const _ServerBadge({required this.provider});
  final NestingJobProvider provider;

  @override
  Widget build(BuildContext context) {
    final color = provider.serverReachable
        ? AppColors.success
        : AppColors.warning;
    return Tooltip(
      message: provider.serverReachable ? 'الخادم متصل' : 'الخادم غير متصل',
      child: Container(
        margin: const EdgeInsetsDirectional.only(end: 8),
        padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
        decoration: BoxDecoration(
          color: color.withValues(alpha: .08),
          borderRadius: BorderRadius.circular(9),
          border: Border.all(color: color.withValues(alpha: .25)),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 7,
              height: 7,
              decoration: BoxDecoration(color: color, shape: BoxShape.circle),
            ),
            const SizedBox(width: 6),
            Text(
              provider.serverReachable ? 'متصل' : 'انتظار',
              style: TextStyle(
                color: color,
                fontSize: 11,
                fontWeight: FontWeight.w700,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _HeaderBlock extends StatelessWidget {
  const _HeaderBlock({
    required this.total,
    required this.valid,
    required this.rejected,
    required this.pending,
  });
  final int total;
  final int valid;
  final int rejected;
  final int pending;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'جاهز لترتيب الشيت؟',
              style: TextStyle(fontSize: 19, fontWeight: FontWeight.w800),
            ),
            const SizedBox(height: 5),
            const Text(
              'ارفع صورك بأي صيغة: PNG أو JPG أو JPEG أو WebP أو TIFF وغيرها. السيرفر يجهزها تلقائياً ويحلّل الـcontour قبل الترتيب.',
              style: TextStyle(
                color: AppColors.slate500,
                fontSize: 12.5,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 14),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                _MetricChip(
                  icon: Icons.photo_library_outlined,
                  label: '$total صورة',
                  color: AppColors.slate700,
                ),
                _MetricChip(
                  icon: Icons.check_circle_outline,
                  label: '$valid صالحة',
                  color: AppColors.success,
                ),
                if (pending > 0)
                  _MetricChip(
                    icon: Icons.sync_rounded,
                    label: '$pending قيد الفحص',
                    color: AppColors.info,
                  ),
                if (rejected > 0)
                  _MetricChip(
                    icon: Icons.error_outline_rounded,
                    label: '$rejected مرفوضة',
                    color: AppColors.danger,
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _MetricChip extends StatelessWidget {
  const _MetricChip({
    required this.icon,
    required this.label,
    required this.color,
  });
  final IconData icon;
  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
    decoration: BoxDecoration(
      color: color.withValues(alpha: .07),
      borderRadius: BorderRadius.circular(9),
      border: Border.all(color: color.withValues(alpha: .16)),
    ),
    child: Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 15, color: color),
        const SizedBox(width: 5),
        Text(
          label,
          style: TextStyle(
            color: color,
            fontSize: 11.5,
            fontWeight: FontWeight.w700,
          ),
        ),
      ],
    ),
  );
}

class _DropCard extends StatelessWidget {
  const _DropCard({required this.onPick, required this.onPickFolder});
  final VoidCallback onPick;
  final VoidCallback onPickFolder;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onPick,
      borderRadius: BorderRadius.circular(18),
      child: Ink(
        decoration: BoxDecoration(
          color: AppColors.primary.withValues(alpha: .035),
          borderRadius: BorderRadius.circular(18),
          border: Border.all(
            color: AppColors.primary.withValues(alpha: .22),
            width: 1.2,
          ),
        ),
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 26),
        child: Column(
          children: [
            Container(
              width: 58,
              height: 58,
              decoration: BoxDecoration(
                color: AppColors.primary.withValues(alpha: .10),
                shape: BoxShape.circle,
              ),
              child: const Icon(
                Icons.add_photo_alternate_outlined,
                color: AppColors.primary,
                size: 28,
              ),
            ),
            const SizedBox(height: 12),
            const Text(
              'اختيار الصور',
              style: TextStyle(fontWeight: FontWeight.w800, fontSize: 15),
            ),
            const SizedBox(height: 5),
            const Text(
              'يمكنك اختيار عشرات الصور في دفعة واحدة',
              style: TextStyle(color: AppColors.slate500, fontSize: 12),
            ),
            const SizedBox(height: 14),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              alignment: WrapAlignment.center,
              children: [
                OutlinedButton.icon(
                  onPressed: onPick,
                  icon: const Icon(
                    Icons.add_photo_alternate_outlined,
                    size: 18,
                  ),
                  label: const Text('اختيار صور'),
                ),
                OutlinedButton.icon(
                  onPressed: onPickFolder,
                  icon: const Icon(Icons.folder_open_rounded, size: 18),
                  label: const Text('اختيار مجلد'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _PartCard extends StatelessWidget {
  const _PartCard({required this.part, required this.onRemove});
  final UploadedPart part;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) {
    final statusColor = part.isValid
        ? AppColors.success
        : part.isRejected
        ? AppColors.danger
        : AppColors.info;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(10),
        child: Row(
          children: [
            _Thumbnail(part: part),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    part.fileName,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      fontWeight: FontWeight.w700,
                      fontSize: 13,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Row(
                    children: [
                      Icon(
                        part.isValid
                            ? Icons.verified_rounded
                            : part.isRejected
                            ? Icons.error_outline_rounded
                            : Icons.sync_rounded,
                        size: 14,
                        color: statusColor,
                      ),
                      const SizedBox(width: 4),
                      Expanded(
                        child: Text(
                          part.isValid
                              ? 'تم التحقق — جاهزة للـnesting'
                              : part.isRejected
                              ? (part.rejectionReason ?? 'تم رفض الصورة')
                              : 'جاري إرسالها وتحليلها...',
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: 11.2,
                            color: statusColor,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
            IconButton(
              onPressed: onRemove,
              icon: const Icon(Icons.close_rounded),
              tooltip: 'حذف الصورة',
            ),
          ],
        ),
      ),
    );
  }
}

class _Thumbnail extends StatelessWidget {
  const _Thumbnail({required this.part});
  final UploadedPart part;

  @override
  Widget build(BuildContext context) {
    Widget child;
    if (part.bytes != null) {
      child = Image.memory(part.bytes!, fit: BoxFit.cover);
    } else {
      child = const Icon(Icons.image_outlined, color: AppColors.slate400);
    }
    return Container(
      width: 58,
      height: 58,
      decoration: BoxDecoration(
        color: AppColors.slate100,
        borderRadius: BorderRadius.circular(11),
        border: Border.all(color: AppColors.slate200),
      ),
      clipBehavior: Clip.antiAlias,
      child: child,
    );
  }
}

class _UploadProgressCard extends StatelessWidget {
  const _UploadProgressCard({required this.progress});
  final double progress;

  @override
  Widget build(BuildContext context) {
    return Card(
      color: AppColors.info.withValues(alpha: .05),
      child: Padding(
        padding: const EdgeInsets.all(13),
        child: Row(
          children: [
            const SizedBox(
              width: 18,
              height: 18,
              child: CircularProgressIndicator(strokeWidth: 2.2),
            ),
            const SizedBox(width: 10),
            const Expanded(
              child: Text(
                'جاري رفع الصور وتجهيزها وتحليل الـcontour...',
                style: TextStyle(fontWeight: FontWeight.w700, fontSize: 12.5),
              ),
            ),
            if (progress > 0 && progress < 1)
              Text(
                '${(progress * 100).round()}%',
                style: const TextStyle(
                  fontWeight: FontWeight.w800,
                  fontSize: 12,
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _RejectedBanner extends StatelessWidget {
  const _RejectedBanner({required this.count, required this.onRecheck});
  final int count;
  final Future<void> Function() onRecheck;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(13),
    decoration: BoxDecoration(
      color: AppColors.danger.withValues(alpha: .06),
      borderRadius: BorderRadius.circular(12),
      border: Border.all(color: AppColors.danger.withValues(alpha: .22)),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const Icon(
              Icons.warning_amber_rounded,
              color: AppColors.danger,
              size: 19,
            ),
            const SizedBox(width: 9),
            Expanded(
              child: Text(
                '$count صورة تحتاج إعادة فحص بعد التحديث.',
                style: const TextStyle(
                  color: AppColors.danger,
                  fontWeight: FontWeight.w700,
                  fontSize: 12,
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 5),
        TextButton.icon(
          onPressed: () => onRecheck(),
          icon: const Icon(Icons.refresh_rounded, size: 17),
          label: const Text('إعادة الفحص الآن'),
        ),
      ],
    ),
  );
}

class _ErrorBanner extends StatelessWidget {
  const _ErrorBanner({required this.message});
  final String message;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(13),
    decoration: BoxDecoration(
      color: AppColors.warning.withValues(alpha: .07),
      borderRadius: BorderRadius.circular(12),
      border: Border.all(color: AppColors.warning.withValues(alpha: .24)),
    ),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Icon(
          Icons.info_outline_rounded,
          color: AppColors.warning,
          size: 19,
        ),
        const SizedBox(width: 9),
        Expanded(
          child: Text(
            message,
            style: const TextStyle(
              color: AppColors.slate700,
              fontSize: 12.2,
              height: 1.45,
            ),
          ),
        ),
      ],
    ),
  );
}

class _BottomBar extends StatelessWidget {
  const _BottomBar({
    required this.total,
    required this.canProceed,
    required this.busy,
    required this.onAddMore,
    required this.onProceed,
  });
  final int total;
  final bool canProceed;
  final bool busy;
  final VoidCallback onAddMore;
  final VoidCallback onProceed;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 10, 16, 14),
      decoration: BoxDecoration(
        color: Theme.of(context).scaffoldBackgroundColor,
        border: const Border(top: BorderSide(color: AppColors.slate200)),
        boxShadow: [
          BoxShadow(
            color: AppColors.slate900.withValues(alpha: .05),
            blurRadius: 14,
            offset: const Offset(0, -4),
          ),
        ],
      ),
      child: SafeArea(
        top: false,
        child: Row(
          children: [
            OutlinedButton.icon(
              onPressed: busy ? null : onAddMore,
              icon: const Icon(Icons.add_rounded, size: 18),
              label: const Text('إضافة'),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: ElevatedButton.icon(
                onPressed: canProceed && !busy ? onProceed : null,
                icon: const Icon(Icons.auto_awesome_rounded, size: 18),
                label: Text(
                  total > 0 ? 'بدء ترتيب $total صورة' : 'ابدأ بإضافة الصور',
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
