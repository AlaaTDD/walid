import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';

import 'package:sheet_nesting_app/providers/nesting_job_provider.dart';
import 'package:sheet_nesting_app/screens/upload_screen.dart';
import 'package:sheet_nesting_app/core/theme/app_theme.dart';

void main() {
  testWidgets('upload screen renders the production workflow', (tester) async {
    await tester.pumpWidget(
      MultiProvider(
        providers: [
          ChangeNotifierProvider(
            create: (_) => NestingJobProvider(loadPersistedSettings: false),
          ),
        ],
        child: MaterialApp(theme: AppTheme.light, home: const UploadScreen()),
      ),
    );

    await tester.pump();
    expect(find.text('جاهز لترتيب الشيت؟'), findsOneWidget);
    expect(find.text('اختيار الصور'), findsOneWidget);
    expect(find.text('ابدأ بإضافة الصور'), findsOneWidget);
  });
}
