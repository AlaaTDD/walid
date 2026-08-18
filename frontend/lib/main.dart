import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'core/theme/app_theme.dart';
import 'screens/upload_screen.dart';
import 'providers/server_provider.dart';
import 'providers/nesting_job_provider.dart';

void main() {
  runApp(const SheetNestingApp());
}

/// التطبيق الرئيسي: يلف كل شيء بـ MultiProvider عشان
/// حالة الـ job والـ server تكون متاحة لكل الشاشات.
class SheetNestingApp extends StatefulWidget {
  const SheetNestingApp({super.key});

  @override
  State<SheetNestingApp> createState() => _SheetNestingAppState();
}

class _SheetNestingAppState extends State<SheetNestingApp> {
  final _serverProvider = ServerProvider();

  @override
  void dispose() {
    _serverProvider.stopServer();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => NestingJobProvider()),
        ChangeNotifierProvider.value(value: _serverProvider),
      ],
      child: MaterialApp(
        title: 'Sheet Nesting',
        debugShowCheckedModeBanner: false,
        theme: AppTheme.light,
        darkTheme: AppTheme.dark,
        themeMode: ThemeMode.light,
        // RTL support للعربية
        builder: (context, child) {
          return Directionality(
            textDirection: TextDirection.rtl,
            child: child!,
          );
        },
        home: const UploadScreen(),
      ),
    );
  }
}
