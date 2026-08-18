import 'dart:io';

const _imageExtensions = {
  '.avif', '.bmp', '.gif', '.heic', '.heif', '.ico', '.jpeg', '.jpg',
  '.png', '.tif', '.tiff', '.webp',
};

/// Enumerate only direct raster-image files in a chosen source folder.
///
/// The original files are never copied or moved here. The backend receives
/// their paths solely to move *placed* files after TIFF + QA success.
Future<List<String>> imagePathsInFolder(String folderPath) async {
  final directory = Directory(folderPath);
  if (!await directory.exists()) return const [];

  final paths = <String>[];
  await for (final entity in directory.list(followLinks: false)) {
    if (entity is! File) continue;
    final name = entity.path.toLowerCase();
    if (_imageExtensions.any(name.endsWith)) {
      paths.add(entity.path);
    }
  }
  paths.sort((left, right) => left.toLowerCase().compareTo(right.toLowerCase()));
  return paths;
}
