import 'dart:typed_data';

enum PartValidationStatus { pending, valid, rejected }

class UploadedPart {
  const UploadedPart({
    required this.id,
    required this.fileName,
    required this.filePath,
    this.originalSourcePath,
    this.bytes,
    this.backendPartId,
    this.validationStatus = PartValidationStatus.pending,
    this.rejectionReason,
    this.widthPx,
    this.heightPx,
  });

  final String id;
  final String fileName;
  final String filePath;
  /// The user-selected local original. filePath may later be replaced by the
  /// resumable staging copy, while this path is sent to the local backend for
  /// the post-success move operation.
  final String? originalSourcePath;
  final Uint8List? bytes;
  final String? backendPartId;
  final PartValidationStatus validationStatus;
  final String? rejectionReason;
  final int? widthPx;
  final int? heightPx;

  bool get isValid => validationStatus == PartValidationStatus.valid;
  bool get isRejected => validationStatus == PartValidationStatus.rejected;
  bool get isPending => validationStatus == PartValidationStatus.pending;

  UploadedPart copyWith({
    String? id,
    String? fileName,
    String? filePath,
    String? originalSourcePath,
    bool clearOriginalSourcePath = false,
    Uint8List? bytes,
    String? backendPartId,
    bool clearBackendPartId = false,
    PartValidationStatus? validationStatus,
    String? rejectionReason,
    int? widthPx,
    int? heightPx,
  }) => UploadedPart(
        id: id ?? this.id,
        fileName: fileName ?? this.fileName,
        filePath: filePath ?? this.filePath,
        originalSourcePath: clearOriginalSourcePath
            ? null
            : (originalSourcePath ?? this.originalSourcePath),
        bytes: bytes ?? this.bytes,
        backendPartId: clearBackendPartId ? null : (backendPartId ?? this.backendPartId),
        validationStatus: validationStatus ?? this.validationStatus,
        rejectionReason: rejectionReason ?? this.rejectionReason,
        widthPx: widthPx ?? this.widthPx,
        heightPx: heightPx ?? this.heightPx,
      );
}

/// The rotation angle applied to a placed part, in whole degrees.
///
/// Historically this mirrored a fixed 24-value enum on the backend
/// (LockedRotation in app/nesting/rotation.py, every 15 degrees). The
/// backend has since added Fine Rotation Refinement, which can pick a
/// winning angle from ~96 additional integer-degree values (±3°/±6°
/// around each coarse multiple of 15°) -- see rotation.py's FINE_* members
/// and _refine_rotation_around_winner in engine.py. Modeling this as a
/// closed Dart enum meant any of those fine angles reaching the frontend
/// (a real, observed outcome once refinement wins) threw an ArgumentError
/// and crashed decoding an otherwise-successful compute result. The
/// frontend never uses this value for any geometry or placement math --
/// placedShape bounds/contour/centroid all arrive from the backend already
/// final -- it is purely a display label (see preview_screen.dart). A
/// lightweight wrapper that accepts any integer degree removes the crash
/// without losing anything: the person still sees the exact angle the
/// backend chose, whether it is a coarse or a fine one.
class LockedRotation {
  const LockedRotation._(this.degrees);

  final int degrees;

  static LockedRotation fromDegrees(int value) => LockedRotation._(value);

  @override
  bool operator ==(Object other) =>
      other is LockedRotation && other.degrees == degrees;

  @override
  int get hashCode => degrees.hashCode;

  @override
  String toString() => 'LockedRotation($degrees°)';
}

class ContourPointMm {
  const ContourPointMm(this.x, this.y);
  final double x;
  final double y;
}

class PlacedPart {
  const PlacedPart({
    required this.partId,
    required this.rotation,
    required this.contourMm,
    required this.boundsMm,
    required this.centroidMm,
    this.sourceThumbnail,
  });

  final String partId;
  final LockedRotation rotation;
  final List<ContourPointMm> contourMm;
  final (double, double, double, double) boundsMm;
  final ContourPointMm centroidMm;
  final Uint8List? sourceThumbnail;
}
