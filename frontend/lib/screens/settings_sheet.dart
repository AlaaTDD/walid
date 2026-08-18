import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../core/constants/nesting_constants.dart';
import '../core/theme/app_theme.dart';
import '../models/nesting_job.dart';
import '../providers/nesting_job_provider.dart';

class SettingsSheet extends StatefulWidget {
  const SettingsSheet({super.key});

  @override
  State<SettingsSheet> createState() => _SettingsSheetState();
}

class _SettingsSheetState extends State<SettingsSheet> {
  late TextEditingController _widthCtrl;
  late TextEditingController _heightCtrl;
  late TextEditingController _marginCtrl;
  late TextEditingController _clearanceCtrl;
  late TextEditingController _dpiCtrl;
  late TextEditingController _processedImagesPathCtrl;
  late Color _backgroundColor;
  late String _exportMode;

  // رسائل خطأ لكل حقل رقمي — null يعني القيمة سليمة. بدل ما
  // double.tryParse ترجع null بصمت والقيمة القديمة تتستخدم، دلوقتي المستخدم
  // بيشوف إن القيمة اللي كتبها مش مقبولة فورياً، مش بعد ما يدوس "حفظ".
  String? _widthError;
  String? _heightError;
  String? _marginError;
  String? _clearanceError;
  String? _dpiError;

  @override
  void initState() {
    super.initState();
    final settings = context.read<NestingJobProvider>().job.settings;
    _widthCtrl = TextEditingController(
      text: settings.sheetWidthMm.toStringAsFixed(1),
    );
    _heightCtrl = TextEditingController(
      text: settings.sheetHeightMm.toStringAsFixed(1),
    );
    _marginCtrl = TextEditingController(
      text: settings.sheetMarginMm.toStringAsFixed(1),
    );
    _clearanceCtrl = TextEditingController(
      text: settings.clearanceMm.toStringAsFixed(2),
    );
    _dpiCtrl = TextEditingController(text: settings.dpi.toStringAsFixed(0));
    _backgroundColor = _colorFromSetting(settings.backgroundColor);
    _processedImagesPathCtrl = TextEditingController(
      text: settings.processedImagesPath,
    );
    _exportMode = settings.exportMode;
  }

  @override
  void dispose() {
    _widthCtrl.dispose();
    _heightCtrl.dispose();
    _marginCtrl.dispose();
    _clearanceCtrl.dispose();
    _dpiCtrl.dispose();
    _processedImagesPathCtrl.dispose();
    super.dispose();
  }

  /// يتحقق من قيمة رقمية موجبة ويرجعها لو صالحة، أو يرجع null ويسجّل
  /// رسالة خطأ واضحة للمستخدم بدل ما يرجع للقيمة القديمة بصمت.
  double? _validatePositive(String text, void Function(String?) setError) {
    final trimmed = text.trim();
    if (trimmed.isEmpty) {
      setError('مطلوب');
      return null;
    }
    final value = double.tryParse(trimmed);
    if (value == null) {
      setError('رقم غير صالح');
      return null;
    }
    if (value <= 0) {
      setError('لازم يكون أكبر من صفر');
      return null;
    }
    setError(null);
    return value;
  }

  Color _colorFromSetting(String value) {
    final normalized = value.trim().replaceFirst('#', '');
    final rgb = int.tryParse(normalized, radix: 16);
    if (RegExp(r'^[0-9A-Fa-f]{6}$').hasMatch(normalized) && rgb != null) {
      return Color(0xFF000000 | rgb);
    }
    switch (value.trim().toLowerCase()) {
      case 'black':
        return Colors.black;
      case 'gray':
      case 'grey':
        return const Color(0xFF808080);
      case 'white':
      default:
        return Colors.white;
    }
  }

  String _colorToHex(Color color) {
    final rgb = color.toARGB32() & 0x00FFFFFF;
    return '#${rgb.toRadixString(16).padLeft(6, '0').toUpperCase()}';
  }

  Future<void> _chooseBackgroundColor() async {
    final selected = await showDialog<Color>(
      context: context,
      builder: (context) =>
          _BackgroundColorPickerDialog(initialColor: _backgroundColor),
    );
    if (selected != null && mounted) {
      setState(() => _backgroundColor = selected);
    }
  }

  Future<void> _save() async {
    // كل التحققات جوه setState واحد صريح — رسائل الخطأ بتتحدث
    // والـ rebuild يحصل مرة واحدة واضحة النية، مش معتمدة على
    // ترتيب استدعاءات ضمني. القيم المتحقق منها بتتجمع محلياً
    // في قائمة واحدة بدل متغيرات منفصلة — عشان يبقى الـ null-safety
    // واضح بدون الحاجة لـ null assertion (!) بعد الخروج من closure.
    final values = <double?>[];

    setState(() {
      values
        ..add(_validatePositive(_widthCtrl.text, (e) => _widthError = e))
        ..add(_validatePositive(_heightCtrl.text, (e) => _heightError = e))
        ..add(_validatePositive(_marginCtrl.text, (e) => _marginError = e))
        ..add(
          _validatePositive(_clearanceCtrl.text, (e) => _clearanceError = e),
        )
        ..add(_validatePositive(_dpiCtrl.text, (e) => _dpiError = e));
    });

    final hasError = values.any((v) => v == null);

    if (hasError) {
      // رسائل الخطأ ظهرت فعلياً تحت الحقول من الـ setState فوق ذاته —
      // مفيش حاجة لـ setState تانية. منمنعين الإغلاق الصامت
      // للشيت بقيم غير مكتملة وبننبّه المستخدم باللمس.
      HapticFeedback.mediumImpact();
      return;
    }
    final newSettings = NestingJobSettings(
      sheetWidthMm: values[0]!,
      sheetHeightMm: values[1]!,
      sheetMarginMm: values[2]!,
      clearanceMm: values[3]!,
      dpi: values[4]!,
      exportMode: _exportMode,
      backgroundColor: _colorToHex(_backgroundColor),
      processedImagesPath: _processedImagesPathCtrl.text.trim(),
      // الوضع الأقصى ثابت دائماً وحيد الآن — لا يوجد اختيار أقل في الواجهة.
      // راجع NestingConstants.maxPackingAttempts في nesting_constants.dart.
      packingAttempts: NestingConstants.maxPackingAttempts,
    );
    final provider = context.read<NestingJobProvider>();
    await provider.updateSettings(newSettings);
    if (mounted) Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    return DraggableScrollableSheet(
      initialChildSize: 0.75,
      minChildSize: 0.4,
      maxChildSize: 0.92,
      expand: false,
      builder: (context, scrollController) {
        return Container(
          decoration: BoxDecoration(
            color: Colors.white,
            borderRadius: const BorderRadius.vertical(top: Radius.circular(22)),
            boxShadow: [
              BoxShadow(
                color: AppColors.slate900.withValues(alpha: 0.14),
                blurRadius: 28,
                offset: const Offset(0, -6),
              ),
            ],
          ),
          child: Column(
            children: [
              const SizedBox(height: 11),
              Container(
                width: 40,
                height: 4,
                decoration: BoxDecoration(
                  color: AppColors.slate300,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 16, 20, 10),
                child: Row(
                  children: [
                    const Expanded(
                      child: Text(
                        'إعدادات الشيت',
                        style: TextStyle(
                          fontSize: 17,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    IconButton(
                      icon: const Icon(Icons.close_rounded),
                      style: IconButton.styleFrom(
                        backgroundColor: AppColors.slate100,
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(9),
                        ),
                      ),
                      onPressed: () => Navigator.of(context).pop(),
                    ),
                  ],
                ),
              ),
              const Divider(height: 1),
              Expanded(
                child: ListView(
                  controller: scrollController,
                  padding: const EdgeInsets.fromLTRB(20, 16, 20, 20),
                  children: [
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Expanded(
                          child: _NumberField(
                            label: 'عرض الشيت (mm)',
                            controller: _widthCtrl,
                            errorText: _widthError,
                          ),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: _NumberField(
                            label: 'ارتفاع الشيت (mm)',
                            controller: _heightCtrl,
                            errorText: _heightError,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 14),
                    _NumberField(
                      label: 'هامش الأمان من حرف الشيت (mm)',
                      controller: _marginCtrl,
                      errorText: _marginError,
                    ),
                    const SizedBox(height: 14),
                    _NumberField(
                      label: 'المسافة بين القطع (clearance, mm)',
                      controller: _clearanceCtrl,
                      errorText: _clearanceError,
                      helperText: _clearanceError == null
                          ? 'القيمة الافتراضية الموثقة في الـ backend: 4.10mm'
                          : null,
                    ),
                    const SizedBox(height: 14),
                    _NumberField(
                      label: 'دقة التصدير (DPI)',
                      controller: _dpiCtrl,
                      errorText: _dpiError,
                    ),
                    const SizedBox(height: 14),
                    _ColorPickerField(
                      color: _backgroundColor,
                      hex: _colorToHex(_backgroundColor),
                      onTap: _chooseBackgroundColor,
                    ),
                    const SizedBox(height: 14),
                    _NumberField(
                      label: 'مسار حفظ الصور المعالَجة',
                      controller: _processedImagesPathCtrl,
                      helperText:
                          'على نفس جهاز الـbackend. بعد نجاح TIFF فقط تُنقل الصور المرتبة إلى مجلد بتاريخ العملية.',
                      keyboardType: TextInputType.text,
                    ),
                    const SizedBox(height: 20),
                    const Text(
                      'وضع الألوان للتصدير',
                      style: TextStyle(
                        fontWeight: FontWeight.w600,
                        fontSize: 13.5,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Row(
                      children: [
                        Expanded(
                          child: _ModeOption(
                            label: 'RGB',
                            subtitle: 'آمن لمعظم خطوط الطباعة',
                            selected: _exportMode == 'RGB',
                            onTap: () => setState(() => _exportMode = 'RGB'),
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: _ModeOption(
                            label: 'RGBA',
                            subtitle: 'يحفظ الشفافية',
                            selected: _exportMode == 'RGBA',
                            onTap: () => setState(() => _exportMode = 'RGBA'),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 8, 20, 20),
                child: SafeArea(
                  top: false,
                  child: SizedBox(
                    width: double.infinity,
                    child: ElevatedButton(
                      onPressed: _save,
                      child: const Text('حفظ'),
                    ),
                  ),
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _NumberField extends StatelessWidget {
  const _NumberField({
    required this.label,
    required this.controller,
    this.helperText,
    this.errorText,
    this.keyboardType,
  });

  final String label;
  final TextEditingController controller;
  final String? helperText;
  final String? errorText;
  final TextInputType? keyboardType;

  @override
  Widget build(BuildContext context) {
    return AnimatedContainer(
      duration: AppMotion.fast,
      curve: AppMotion.standard,
      child: TextField(
        controller: controller,
        keyboardType:
            keyboardType ??
            const TextInputType.numberWithOptions(decimal: true),
        decoration: InputDecoration(
          labelText: label,
          helperText: helperText,
          helperMaxLines: 2,
          errorText: errorText,
          errorMaxLines: 2,
        ),
      ),
    );
  }
}

class _ColorPickerField extends StatelessWidget {
  const _ColorPickerField({
    required this.color,
    required this.hex,
    required this.onTap,
  });

  final Color color;
  final String hex;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: 'اختيار لون خلفية TIFF، اللون الحالي $hex',
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Container(
          padding: const EdgeInsets.all(13),
          decoration: BoxDecoration(
            color: AppColors.slate50,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: AppColors.slate200),
          ),
          child: Row(
            children: [
              Container(
                width: 46,
                height: 46,
                decoration: BoxDecoration(
                  color: color,
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: AppColors.slate300),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      'لون خلفية TIFF',
                      style: TextStyle(fontWeight: FontWeight.w700),
                    ),
                    const SizedBox(height: 3),
                    Text(
                      '$hex — اضغط لاختيار اللون',
                      style: const TextStyle(
                        color: AppColors.slate500,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),
              const Icon(Icons.colorize_rounded, color: AppColors.primary),
            ],
          ),
        ),
      ),
    );
  }
}

class _BackgroundColorPickerDialog extends StatefulWidget {
  const _BackgroundColorPickerDialog({required this.initialColor});

  final Color initialColor;

  @override
  State<_BackgroundColorPickerDialog> createState() =>
      _BackgroundColorPickerDialogState();
}

class _BackgroundColorPickerDialogState
    extends State<_BackgroundColorPickerDialog> {
  static const _palette = <Color>[
    Colors.white,
    Color(0xFFF2F2F2),
    Color(0xFF808080),
    Colors.black,
    Color(0xFFFBE9E7),
    Color(0xFFFFCDD2),
    Color(0xFFD32F2F),
    Color(0xFFFFF3E0),
    Color(0xFFFFB300),
    Color(0xFFFFFDE7),
    Color(0xFFFFEB3B),
    Color(0xFFE8F5E9),
    Color(0xFF43A047),
    Color(0xFFE3F2FD),
    Color(0xFF1E88E5),
    Color(0xFFEDE7F6),
    Color(0xFF5E35B1),
    Color(0xFFFCE4EC),
    Color(0xFFD81B60),
  ];

  late Color _color;

  @override
  void initState() {
    super.initState();
    _color = widget.initialColor;
  }

  int get _rgb => _color.toARGB32() & 0x00FFFFFF;
  int get _red => (_rgb >> 16) & 0xFF;
  int get _green => (_rgb >> 8) & 0xFF;
  int get _blue => _rgb & 0xFF;

  String get _hex => '#${_rgb.toRadixString(16).padLeft(6, '0').toUpperCase()}';

  void _setChannels({int? red, int? green, int? blue}) {
    setState(() {
      _color = Color.fromARGB(255, red ?? _red, green ?? _green, blue ?? _blue);
    });
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('اختيار لون الخلفية'),
      content: SingleChildScrollView(
        child: SizedBox(
          width: 340,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                height: 72,
                width: double.infinity,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: _color,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: AppColors.slate300),
                ),
                child: Text(
                  _hex,
                  style: TextStyle(
                    color: _color.computeLuminance() > 0.45
                        ? Colors.black
                        : Colors.white,
                    fontWeight: FontWeight.w800,
                    fontSize: 16,
                  ),
                ),
              ),
              const SizedBox(height: 16),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final color in _palette)
                    Semantics(
                      button: true,
                      label: 'لون جاهز',
                      child: InkWell(
                        onTap: () => setState(() => _color = color),
                        borderRadius: BorderRadius.circular(20),
                        child: Container(
                          width: 32,
                          height: 32,
                          decoration: BoxDecoration(
                            color: color,
                            shape: BoxShape.circle,
                            border: Border.all(
                              color: color.toARGB32() == _color.toARGB32()
                                  ? AppColors.primary
                                  : AppColors.slate300,
                              width: color.toARGB32() == _color.toARGB32()
                                  ? 3
                                  : 1,
                            ),
                          ),
                        ),
                      ),
                    ),
                ],
              ),
              const SizedBox(height: 16),
              _ColorChannelSlider(
                label: 'أحمر',
                value: _red,
                activeColor: Colors.red,
                onChanged: (value) => _setChannels(red: value),
              ),
              _ColorChannelSlider(
                label: 'أخضر',
                value: _green,
                activeColor: Colors.green,
                onChanged: (value) => _setChannels(green: value),
              ),
              _ColorChannelSlider(
                label: 'أزرق',
                value: _blue,
                activeColor: Colors.blue,
                onChanged: (value) => _setChannels(blue: value),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('إلغاء'),
        ),
        ElevatedButton(
          onPressed: () => Navigator.of(context).pop(_color),
          child: const Text('تطبيق اللون'),
        ),
      ],
    );
  }
}

class _ColorChannelSlider extends StatelessWidget {
  const _ColorChannelSlider({
    required this.label,
    required this.value,
    required this.activeColor,
    required this.onChanged,
  });

  final String label;
  final int value;
  final Color activeColor;
  final ValueChanged<int> onChanged;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        SizedBox(
          width: 42,
          child: Text(label, style: const TextStyle(fontSize: 12)),
        ),
        Expanded(
          child: Slider(
            value: value.toDouble(),
            min: 0,
            max: 255,
            divisions: 255,
            activeColor: activeColor,
            onChanged: (next) => onChanged(next.round()),
          ),
        ),
        SizedBox(width: 30, child: Text('$value', textAlign: TextAlign.end)),
      ],
    );
  }
}

class _ModeOption extends StatelessWidget {
  const _ModeOption({
    required this.label,
    required this.subtitle,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final String subtitle;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(11),
      child: AnimatedContainer(
        duration: AppMotion.fast,
        curve: AppMotion.standard,
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: selected
              ? AppColors.primary.withValues(alpha: 0.07)
              : AppColors.slate50,
          borderRadius: BorderRadius.circular(11),
          border: Border.all(
            color: selected ? AppColors.primary : AppColors.slate200,
            width: selected ? 1.6 : 1,
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Text(
                  label,
                  style: TextStyle(
                    fontWeight: FontWeight.w700,
                    fontSize: 13.5,
                    color: selected ? AppColors.primary : AppColors.slate900,
                  ),
                ),
                const Spacer(),
                AnimatedSwitcher(
                  duration: AppMotion.fast,
                  transitionBuilder: (child, animation) =>
                      ScaleTransition(scale: animation, child: child),
                  child: selected
                      ? const Icon(
                          Icons.check_circle_rounded,
                          key: ValueKey('selected'),
                          size: 16,
                          color: AppColors.primary,
                        )
                      : const SizedBox(
                          key: ValueKey('unselected'),
                          width: 16,
                          height: 16,
                        ),
                ),
              ],
            ),
            const SizedBox(height: 3),
            Text(
              subtitle,
              style: const TextStyle(fontSize: 11, color: AppColors.slate500),
            ),
          ],
        ),
      ),
    );
  }
}
