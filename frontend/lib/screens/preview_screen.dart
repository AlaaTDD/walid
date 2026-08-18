import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/theme/app_theme.dart';
import '../models/nesting_job.dart';
import '../models/sheet_part.dart';
import '../providers/nesting_job_provider.dart';
import '../widgets/sheet_layout_painter.dart';
import '../widgets/violation_list_tile.dart';
import '../widgets/workflow_stepper.dart';
import 'export_screen.dart';
import 'server_logs_sheet.dart';

class PreviewScreen extends StatefulWidget {
  const PreviewScreen({super.key});

  @override
  State<PreviewScreen> createState() => _PreviewScreenState();
}

class _PreviewScreenState extends State<PreviewScreen> with SingleTickerProviderStateMixin {
  String? _selectedPartId;
  int _selectedSheetIndex = 0;
  bool _showClearanceZones = false;
  late final AnimationController _highlightController;

  @override
  void initState() {
    super.initState();
    _highlightController = AnimationController(vsync: this, duration: AppMotion.slow);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final provider = context.read<NestingJobProvider>();
      if (provider.job.computeResult == null) provider.computeLayout();
    });
  }

  @override
  void dispose() {
    _highlightController.dispose();
    super.dispose();
  }

  void _selectPart(String? id) {
    setState(() => _selectedPartId = id);
    if (id != null) _highlightController.forward(from: 0);
  }

  void _selectSheet(int index) {
    setState(() {
      _selectedSheetIndex = index;
      _selectedPartId = null;
    });
  }

  @override
  Widget build(BuildContext context) {
    final provider = context.watch<NestingJobProvider>();
    final job = provider.job;
    return PopScope(
      canPop: true,
      onPopInvokedWithResult: (didPop, result) {
        if (didPop && job.stage == NestingJobStage.computing) provider.cancelCompute();
      },
      child: Scaffold(
        appBar: AppBar(
          title: const Text('معاينة الترتيب'),
          actions: [
            IconButton(
              tooltip: 'إعادة الحساب',
              onPressed: job.stage == NestingJobStage.proofPreview ? provider.computeLayout : null,
              icon: const Icon(Icons.refresh_rounded),
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
              child: WorkflowStepper(currentStage: NestingJobStage.proofPreview),
            ),
            Expanded(child: _Body(job: job, provider: provider, selectedPartId: _selectedPartId, selectedSheetIndex: _selectedSheetIndex, showClearance: _showClearanceZones, onSelect: _selectPart, onSheetSelect: _selectSheet, onClearance: (value) => setState(() => _showClearanceZones = value), highlight: _highlightController)),
          ],
        ),
        bottomNavigationBar: job.stage == NestingJobStage.proofPreview ? _ConfirmBar(job: job) : null,
      ),
    );
  }
}

class _Body extends StatelessWidget {
  const _Body({required this.job, required this.provider, required this.selectedPartId, required this.selectedSheetIndex, required this.showClearance, required this.onSelect, required this.onSheetSelect, required this.onClearance, required this.highlight});
  final NestingJob job;
  final NestingJobProvider provider;
  final String? selectedPartId;
  final int selectedSheetIndex;
  final bool showClearance;
  final ValueChanged<String?> onSelect;
  final ValueChanged<int> onSheetSelect;
  final ValueChanged<bool> onClearance;
  final AnimationController highlight;

  @override
  Widget build(BuildContext context) {
    switch (job.stage) {
      case NestingJobStage.computing:
        final done = provider.computeProgressDone;
        final total = provider.computeProgressTotal;
        final ratio = done != null && total != null && total > 0 ? (done / total).clamp(0, 1).toDouble() : null;
        return Center(
          child: Padding(
            padding: const EdgeInsets.all(28),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 460),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.auto_awesome_rounded, size: 42, color: AppColors.primary),
                  const SizedBox(height: 18),
                  const Text('جاري بناء أفضل ترتيب هندسي', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w800)),
                  const SizedBox(height: 8),
                  Text(provider.computeProgressMessage ?? 'يتم تحليل المساحات واختبار الزوايا المتاحة...', textAlign: TextAlign.center, style: const TextStyle(color: AppColors.slate500, fontSize: 12.5, height: 1.5)),
                  const SizedBox(height: 18),
                  ClipRRect(borderRadius: BorderRadius.circular(6), child: LinearProgressIndicator(value: ratio, minHeight: 7)),
                  if (done != null && total != null) ...[
                    const SizedBox(height: 8),
                    Text('$done / $total صورة', style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: AppColors.slate600)),
                  ],
                  const SizedBox(height: 20),
                  OutlinedButton.icon(onPressed: provider.cancelCompute, icon: const Icon(Icons.stop_circle_outlined, size: 18), label: const Text('إلغاء الحساب')),
                ],
              ),
            ),
          ),
        );
      case NestingJobStage.failed:
        return Center(child: Padding(padding: const EdgeInsets.all(28), child: Column(mainAxisSize: MainAxisSize.min, children: [
          const Icon(Icons.error_outline_rounded, color: AppColors.danger, size: 46),
          const SizedBox(height: 14),
          Text(job.errorMessage ?? 'حدث خطأ غير متوقع', textAlign: TextAlign.center, style: const TextStyle(fontSize: 13, color: AppColors.slate700, height: 1.5)),
          const SizedBox(height: 20),
          ElevatedButton.icon(onPressed: provider.computeLayout, icon: const Icon(Icons.refresh_rounded, size: 18), label: const Text('إعادة المحاولة')),
        ])));
      case NestingJobStage.proofPreview:
        final result = job.computeResult;
        if (result == null) return const SizedBox.shrink();
        return _ProofLayout(job: job, result: result, selectedPartId: selectedPartId, selectedSheetIndex: selectedSheetIndex.clamp(0, result.sheets.length - 1).toInt(), showClearance: showClearance, onSelect: onSelect, onSheetSelect: onSheetSelect, onClearance: onClearance, highlight: highlight);
      default:
        return const SizedBox.shrink();
    }
  }
}

class _ProofLayout extends StatelessWidget {
  const _ProofLayout({required this.job, required this.result, required this.selectedPartId, required this.selectedSheetIndex, required this.showClearance, required this.onSelect, required this.onSheetSelect, required this.onClearance, required this.highlight});
  final NestingJob job;
  final NestingComputeResult result;
  final String? selectedPartId;
  final int selectedSheetIndex;
  final bool showClearance;
  final ValueChanged<String?> onSelect;
  final ValueChanged<int> onSheetSelect;
  final ValueChanged<bool> onClearance;
  final AnimationController highlight;

  @override
  Widget build(BuildContext context) {
    final wide = MediaQuery.of(context).size.width >= 960;
    final sheet = result.sheets[selectedSheetIndex];
    final canvas = _CanvasPanel(job: job, result: result, sheet: sheet, selectedPartId: selectedPartId, showClearance: showClearance, onSelect: onSelect, onSheetSelect: onSheetSelect, onClearance: onClearance, highlight: highlight);
    final summary = _SummaryPanel(job: job, result: result, sheet: sheet, selectedPartId: selectedPartId, onSelect: onSelect);
    return wide ? Row(children: [Expanded(flex: 3, child: canvas), const VerticalDivider(width: 1), SizedBox(width: 360, child: summary)]) : Column(children: [Expanded(flex: 3, child: canvas), const Divider(height: 1), Expanded(flex: 2, child: summary)]);
  }
}

class _CanvasPanel extends StatelessWidget {
  const _CanvasPanel({required this.job, required this.result, required this.sheet, required this.selectedPartId, required this.showClearance, required this.onSelect, required this.onSheetSelect, required this.onClearance, required this.highlight});
  final NestingJob job;
  final NestingComputeResult result;
  final NestingSheetLayout sheet;
  final String? selectedPartId;
  final bool showClearance;
  final ValueChanged<String?> onSelect;
  final ValueChanged<int> onSheetSelect;
  final ValueChanged<bool> onClearance;
  final AnimationController highlight;

  @override
  Widget build(BuildContext context) {
    return Column(children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(16, 13, 16, 8),
        child: Row(children: [
          const Icon(Icons.straighten_rounded, size: 17, color: AppColors.slate500),
          const SizedBox(width: 6),
          Text('${job.settings.sheetWidthMm.toStringAsFixed(0)} × ${job.settings.sheetHeightMm.toStringAsFixed(0)} mm', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 13)),
          const SizedBox(width: 8),
          Text('${job.settings.dpi.toStringAsFixed(0)} DPI', style: const TextStyle(color: AppColors.slate500, fontSize: 11.5)),
          if (result.sheetCount > 1) ...[
            const SizedBox(width: 10),
            DropdownButton<int>(
              value: sheet.pageNumber - 1,
              underline: const SizedBox.shrink(),
              isDense: true,
              items: List.generate(result.sheetCount, (index) => DropdownMenuItem(value: index, child: Text('ورقة ${index + 1} / ${result.sheetCount}'))),
              onChanged: (index) {
                if (index != null) onSheetSelect(index);
              },
            ),
          ],
          const Spacer(),
          FilterChip(label: const Text('مسافات الأمان'), selected: showClearance, onSelected: onClearance, visualDensity: VisualDensity.compact),
        ]),
      ),
      if (result.unplacedPartIds.isNotEmpty) Padding(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
        child: _CapacityBanner(result: result),
      ),
      Expanded(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: LayoutBuilder(builder: (context, constraints) {
            final ratio = job.settings.sheetWidthMm / job.settings.sheetHeightMm;
            final maxW = constraints.maxWidth;
            final maxH = constraints.maxHeight;
            var w = maxW;
            var h = w / ratio;
            if (h > maxH) { h = maxH; w = h * ratio; }
            return Center(
              child: SizedBox(
                width: w,
                height: h,
                child: GestureDetector(
                  behavior: HitTestBehavior.opaque,
                  onTapUp: (details) {
                    final hit = _hitTest(details.localPosition, Size(w, h), sheet.placedParts, job.settings.sheetWidthMm, job.settings.sheetHeightMm);
                    onSelect(hit == selectedPartId ? null : hit);
                  },
                  child: AnimatedBuilder(
                    animation: highlight,
                    builder: (_, _) => CustomPaint(
                      painter: SheetLayoutPainter(
                        sheetWidthMm: job.settings.sheetWidthMm,
                        sheetHeightMm: job.settings.sheetHeightMm,
                        sheetMarginMm: job.settings.sheetMarginMm,
                        clearanceMm: job.settings.clearanceMm,
                        placedParts: sheet.placedParts,
                        selectedPartId: selectedPartId,
                        showClearanceZones: showClearance,
                        highlightProgress: highlight.value,
                      ),
                    ),
                  ),
                ),
              ),
            );
          }),
        ),
      ),
    ]);
  }

  String? _hitTest(Offset point, Size size, List<PlacedPart> parts, double sheetW, double sheetH) {
    final scale = (size.width / sheetW) < (size.height / sheetH) ? size.width / sheetW : size.height / sheetH;
    final renderedW = sheetW * scale;
    final renderedH = sheetH * scale;
    final dx = (size.width - renderedW) / 2;
    final dy = (size.height - renderedH) / 2;
    final x = (point.dx - dx) / scale;
    final y = (point.dy - dy) / scale;
    for (final part in parts.reversed) {
      final (minX, minY, maxX, maxY) = part.boundsMm;
      if (x >= minX && x <= maxX && y >= minY && y <= maxY) return part.partId;
    }
    return null;
  }
}

class _CapacityBanner extends StatelessWidget {
  const _CapacityBanner({required this.result});
  final NestingComputeResult result;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppColors.warning.withValues(alpha: .07),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppColors.warning.withValues(alpha: .24)),
      ),
      child: Row(children: [
        const Icon(Icons.layers_clear_rounded, color: AppColors.warning, size: 19),
        const SizedBox(width: 8),
        Expanded(child: Text(result.layoutMessage, style: const TextStyle(color: AppColors.slate700, fontSize: 12, fontWeight: FontWeight.w700, height: 1.4))),
      ]),
    );
  }
}

class _SummaryPanel extends StatelessWidget {
  const _SummaryPanel({required this.job, required this.result, required this.sheet, required this.selectedPartId, required this.onSelect});
  final NestingJob job;
  final NestingComputeResult result;
  final NestingSheetLayout sheet;
  final String? selectedPartId;
  final ValueChanged<String?> onSelect;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 18),
      children: [
        _StatusCard(result: result),
        const SizedBox(height: 10),
        Row(children: [
          Expanded(child: _StatCard(value: '${result.placedCount}', label: 'مرتبة', icon: Icons.check_circle_outline, color: AppColors.success)),
          const SizedBox(width: 8),
          Expanded(child: _StatCard(value: '${result.unplacedPartIds.length}', label: 'متبقية', icon: Icons.hourglass_empty_rounded, color: result.unplacedPartIds.isEmpty ? AppColors.success : AppColors.warning)),
          const SizedBox(width: 8),
          Expanded(child: _StatCard(value: '${result.processedCount}', label: 'تمت معالجتها', icon: Icons.memory_rounded, color: AppColors.info)),
        ]),
        const SizedBox(height: 12),
        Text(result.sheetCount > 1 ? 'القطع في الورقة ${sheet.pageNumber}' : 'القطع المرتبة', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 13.5)),
        const SizedBox(height: 7),
        if (sheet.placedParts.isEmpty) const Card(child: Padding(padding: EdgeInsets.all(16), child: Text('لم يتم وضع أي قطعة. راجع أبعاد الشيت والقيود.', style: TextStyle(color: AppColors.slate500, fontSize: 12.5)))),
        ...sheet.placedParts.asMap().entries.map((entry) {
          final part = entry.value;
          final selected = part.partId == selectedPartId;
          return Padding(padding: const EdgeInsets.only(bottom: 6), child: _PlacedPartTile(part: part, selected: selected, onTap: () => onSelect(selected ? null : part.partId)));
        }),
        if (result.collisionViolations.isNotEmpty) ...[
          const SizedBox(height: 8),
          const Text('مخالفات التحقق الهندسي', style: TextStyle(color: AppColors.danger, fontWeight: FontWeight.w800, fontSize: 13.5)),
          const SizedBox(height: 7),
          ...result.collisionViolations.map((v) => ViolationListTile(violation: v)),
        ],
      ],
    );
  }
}

class _StatusCard extends StatelessWidget {
  const _StatusCard({required this.result});
  final NestingComputeResult result;

  @override
  Widget build(BuildContext context) {
    final valid = result.canExport;
    final partial = result.unplacedPartIds.isNotEmpty;
    final color = valid ? (partial ? AppColors.warning : AppColors.success) : AppColors.danger;
    final title = !result.isCollisionValid ? 'يوجد خلل هندسي' : partial ? 'بعض الصور لا تلائم حتى ورقة فارغة' : result.sheetCount > 1 ? 'تم الترتيب على ${result.sheetCount} ورقة' : 'كل الصور اتوضعت بنجاح';
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Row(children: [
          Container(width: 42, height: 42, alignment: Alignment.center, decoration: BoxDecoration(color: color.withValues(alpha: .10), shape: BoxShape.circle), child: Icon(valid ? (partial ? Icons.layers_rounded : Icons.verified_rounded) : Icons.error_outline_rounded, color: color, size: 22)),
          const SizedBox(width: 10),
          Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [Text(title, style: TextStyle(fontWeight: FontWeight.w800, fontSize: 13, color: color)), const SizedBox(height: 3), Text(result.layoutMessage, maxLines: 3, overflow: TextOverflow.ellipsis, style: const TextStyle(color: AppColors.slate500, fontSize: 11.4, height: 1.4))])),
        ]),
      ),
    );
  }
}

class _StatCard extends StatelessWidget {
  const _StatCard({required this.value, required this.label, required this.icon, required this.color});
  final String value;
  final String label;
  final IconData icon;
  final Color color;

  @override
  Widget build(BuildContext context) => Card(child: Padding(padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 11), child: Column(children: [Icon(icon, size: 17, color: color), const SizedBox(height: 5), Text(value, style: const TextStyle(fontWeight: FontWeight.w900, fontSize: 15)), const SizedBox(height: 1), Text(label, style: const TextStyle(color: AppColors.slate500, fontSize: 10.5))])));
}

class _PlacedPartTile extends StatelessWidget {
  const _PlacedPartTile({required this.part, required this.selected, required this.onTap});
  final PlacedPart part;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final (minX, minY, maxX, maxY) = part.boundsMm;
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(10),
      child: AnimatedContainer(
        duration: AppMotion.fast,
        padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 9),
        decoration: BoxDecoration(
          color: selected ? AppColors.primary.withValues(alpha: .055) : Colors.white,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: selected ? AppColors.primary : AppColors.slate200, width: selected ? 1.4 : 1),
        ),
        child: Row(children: [
          Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [Text(part.partId, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 11.8)), const SizedBox(height: 2), Text('${(maxX - minX).toStringAsFixed(1)} × ${(maxY - minY).toStringAsFixed(1)} mm', style: const TextStyle(color: AppColors.slate500, fontSize: 10.7))])),
          Container(padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 4), decoration: BoxDecoration(color: AppColors.slate100, borderRadius: BorderRadius.circular(7)), child: Text('${part.rotation.degrees}°', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 10.5))),
        ]),
      ),
    );
  }
}

class _ConfirmBar extends StatelessWidget {
  const _ConfirmBar({required this.job});
  final NestingJob job;

  @override
  Widget build(BuildContext context) {
    final canExport = job.canConfirmExport;
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 10, 16, 14),
      decoration: const BoxDecoration(color: Colors.white, border: Border(top: BorderSide(color: AppColors.slate200))),
      child: SafeArea(top: false, child: Row(children: [
        Expanded(child: OutlinedButton.icon(onPressed: () { context.read<NestingJobProvider>().backToUpload(); Navigator.of(context).pop(); }, icon: const Icon(Icons.arrow_back_rounded, size: 18), label: const Text('تعديل الصور'))),
        const SizedBox(width: 10),
        Expanded(flex: 2, child: ElevatedButton.icon(onPressed: canExport ? () => Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => const ExportScreen())) : null, icon: const Icon(Icons.file_download_outlined, size: 18), label: Text((job.computeResult?.sheetCount ?? 1) > 1 ? 'تأكيد وتصدير كل الأوراق' : 'تأكيد وتصدير'))),
      ])),
    );
  }
}
