import 'package:flutter/material.dart';

import '../core/theme/app_theme.dart';
import '../models/nesting_job.dart';

class WorkflowStepper extends StatelessWidget {
  const WorkflowStepper({super.key, required this.currentStage});

  final NestingJobStage currentStage;

  int get _activeIndex {
    switch (currentStage) {
      case NestingJobStage.upload:
        return 0;
      case NestingJobStage.computing:
      case NestingJobStage.proofPreview:
        return 1;
      case NestingJobStage.exporting:
      case NestingJobStage.completed:
      case NestingJobStage.failed:
        return 2;
    }
  }

  static const _labels = ['رفع القطع', 'مراجعة الترتيب', 'التصدير'];

  @override
  Widget build(BuildContext context) {
    return Row(
      children: List.generate(_labels.length * 2 - 1, (i) {
        if (i.isOdd) {
          final leftDone = (i - 1) ~/ 2 < _activeIndex;
          return Expanded(
            child: AnimatedContainer(
              duration: AppMotion.base,
              curve: AppMotion.standard,
              height: 2,
              color: leftDone ? AppColors.primary : AppColors.slate200,
            ),
          );
        }
        final stepIndex = i ~/ 2;
        final isDone = stepIndex < _activeIndex;
        final isActive = stepIndex == _activeIndex;
        return _StepDot(
          label: _labels[stepIndex],
          isDone: isDone,
          isActive: isActive,
        );
      }),
    );
  }
}

class _StepDot extends StatelessWidget {
  const _StepDot({
    required this.label,
    required this.isDone,
    required this.isActive,
  });

  final String label;
  final bool isDone;
  final bool isActive;

  @override
  Widget build(BuildContext context) {
    final color = isDone || isActive ? AppColors.primary : AppColors.slate300;
    return Column(
      children: [
        AnimatedContainer(
          duration: AppMotion.base,
          curve: AppMotion.standard,
          width: 30,
          height: 30,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: isDone ? AppColors.primary : Colors.white,
            border: Border.all(color: color, width: isActive ? 2.5 : 1.6),
            boxShadow: isActive
                ? [
                    BoxShadow(
                      color: AppColors.primary.withValues(alpha: 0.28),
                      blurRadius: 10,
                      spreadRadius: 1.5,
                    ),
                  ]
                : isDone
                    ? [
                        BoxShadow(
                          color: AppColors.primary.withValues(alpha: 0.16),
                          blurRadius: 5,
                          spreadRadius: 0,
                        ),
                      ]
                    : null,
          ),
          alignment: Alignment.center,
          child: AnimatedSwitcher(
            duration: AppMotion.fast,
            transitionBuilder: (child, animation) => ScaleTransition(
              scale: animation,
              child: child,
            ),
            child: isDone
                ? const Icon(
                    Icons.check_rounded,
                    key: ValueKey('done'),
                    color: Colors.white,
                    size: 17,
                  )
                : SizedBox(
                    key: const ValueKey('dot'),
                    width: 6,
                    height: 6,
                    child: isActive
                        ? DecoratedBox(
                            decoration: BoxDecoration(
                              shape: BoxShape.circle,
                              color: AppColors.primary,
                            ),
                          )
                        : null,
                  ),
          ),
        ),
        const SizedBox(height: 7),
        AnimatedDefaultTextStyle(
          duration: AppMotion.base,
          curve: AppMotion.standard,
          style: TextStyle(
            fontSize: 11,
            fontWeight: isActive ? FontWeight.w700 : FontWeight.w500,
            color: isActive ? AppColors.slate900 : AppColors.slate500,
            letterSpacing: isActive ? 0.1 : 0,
          ),
          child: Text(label),
        ),
      ],
    );
  }
}
