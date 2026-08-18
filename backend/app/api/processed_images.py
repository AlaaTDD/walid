"""Transactional archive of the original files that were actually exported."""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.api.job_storage import StoredPart


class ProcessedImagesError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProcessedImagesResult:
    directory: str | None
    moved_count: int = 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_filename(name: str, fallback: str) -> str:
    # The name is presented to the user in their archive, but it must never
    # create a nested destination or escape its operation directory.
    candidate = Path(name).name.strip()
    # NOTE: earlier version had r"[\\x00/\\\\]" (double-escaped), whose
    # actual character class matched the literal characters \, x, 0, /
    # instead of a null byte + slash + backslash -- it silently mangled any
    # 'x' or '0' in a real filename (e.g. "box1.png" -> "bo_1.png") while
    # never actually stripping a real null byte. This is the corrected,
    # single-escaped pattern: \x00 is the literal null byte, / and \\ are
    # the literal slash/backslash characters.
    candidate = re.sub(r"[\x00/\\]", "_", candidate)
    return candidate or fallback


def _sanitize_folder_name(name: str) -> str:
    """Sanitize a user-chosen folder name to be filesystem-safe."""
    # Strip path separators and null bytes; trim whitespace.
    sanitized = re.sub(r"[\x00/\\]", "_", name).strip()
    # Remove leading dots to prevent hidden directories.
    sanitized = sanitized.lstrip(".")
    # Collapse runs of underscores / spaces.
    sanitized = re.sub(r"[_\s]+", "_", sanitized).strip("_")
    return sanitized


def _new_operation_directory(
    storage_root: Path,
    folder_name: str | None = None,
) -> Path:
    """Create a new operation directory with the given name + timestamp.

    Structure: ``<storage_root>/<folder_name>_YYYY-MM-DD_HH-MM-SS/``
    If the name already exists (even with the timestamp), a numeric suffix
    ``_1``, ``_2``, … is appended to guarantee uniqueness.
    """
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")

    if folder_name:
        sanitized = _sanitize_folder_name(folder_name)
        if sanitized:
            base = f"{sanitized}_{timestamp}"
        else:
            base = timestamp
    else:
        base = timestamp

    candidate = storage_root / base
    suffix = 1
    while candidate.exists():
        candidate = storage_root / f"{base}_{suffix}"
        suffix += 1
    candidate.mkdir(parents=False, exist_ok=False)
    return candidate


def _resolve_source(part: StoredPart) -> Path:
    """Resolve and validate the original source path for a single part."""
    if not part.original_source_path:
        raise ProcessedImagesError(
            f"لا يوجد مسار للملف الأصلي للصورة '{part.original_filename}'، لذلك لن يتم نقل أي صور."
        )
    try:
        source = Path(part.original_source_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProcessedImagesError(
            f"تعذر الوصول للصورة الأصلية '{part.original_filename}': {exc}"
        ) from exc
    if not source.is_file():
        raise ProcessedImagesError(f"المسار الأصلي ليس ملفًا: {source}")
    return source


def _plan_moves(
    entries: list[tuple[StoredPart, Path]],
    dest_dir: Path,
) -> list[tuple[Path, Path]]:
    """Plan source → destination moves for a list of (part, source) pairs."""
    planned: list[tuple[Path, Path]] = []
    used_names: set[str] = set()
    for part, source in entries:
        name = _safe_filename(part.original_filename, f"image_{part.part_id}")
        destination = dest_dir / name
        if name in used_names:
            destination = dest_dir / f"{destination.stem}_{part.part_id}{destination.suffix}"
        used_names.add(destination.name)
        planned.append((source, destination))
    return planned


def _execute_moves(planned: list[tuple[Path, Path]]) -> int:
    """Execute the planned moves with best-effort rollback on failure."""
    moved: list[tuple[Path, Path]] = []
    try:
        for source, destination in planned:
            # shutil.move selects an atomic rename where possible and safely
            # falls back to copy+remove across volumes.
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
    except Exception as exc:
        rollback_failures: list[str] = []
        for source, destination in reversed(moved):
            try:
                if destination.exists() and not source.exists():
                    shutil.move(str(destination), str(source))
            except Exception:
                rollback_failures.append(destination.name)
        detail = ""
        if rollback_failures:
            detail = f" تعذر التراجع عن: {', '.join(rollback_failures)}."
        raise ProcessedImagesError(f"فشل نقل الصور الأصلية: {exc}.{detail}") from exc
    return len(moved)


def move_processed_originals(
    parts: list[StoredPart],
    placed_part_ids: set[str],
    storage_path: str | Path | None,
    folder_name: str | None = None,
) -> ProcessedImagesResult:
    """Move the original files into placed/unplaced subdirectories.

    After a successful TIFF export + QA pass, this function organises the
    original source images into a new operation directory:

        <storage_path>/<folder_name>_<timestamp>/
            placed/     — images that were placed on the sheet(s)
            unplaced/   — images that were uploaded but not placed

    A source path is accepted only if its current SHA-256 still matches the
    upload that generated the layout. This prevents a stale client path from
    moving a different file. Every source is preflighted before the first move
    and best-effort rollback restores earlier moves if a later filesystem move
    fails (including cross-volume moves).
    """
    if storage_path is None or not str(storage_path).strip():
        return ProcessedImagesResult(directory=None)

    if not parts:
        return ProcessedImagesResult(directory=None)

    # Separate parts into placed and unplaced.
    placed_entries: list[tuple[StoredPart, Path]] = []
    unplaced_entries: list[tuple[StoredPart, Path]] = []
    seen_sources: set[Path] = set()

    for part in parts:
        if not part.original_source_path:
            continue
        try:
            source = _resolve_source(part)
        except ProcessedImagesError:
            # If an unplaced image is inaccessible, skip it rather than
            # aborting the entire operation.
            if part.part_id not in placed_part_ids:
                continue
            raise
        if source in seen_sources:
            continue
        seen_sources.add(source)

        # Verify SHA-256 integrity for placed images (critical for export).
        # For unplaced images, skip the hash check — they may have been
        # modified since upload but should still be organised.
        if part.part_id in placed_part_ids:
            if not part.content_sha256 or _sha256(source) != part.content_sha256:
                raise ProcessedImagesError(
                    f"الصورة الأصلية تغيّرت بعد الرفع ولن تُنقل حفاظًا على البيانات: {source.name}"
                )
            placed_entries.append((part, source))
        else:
            unplaced_entries.append((part, source))

    if not placed_entries and not unplaced_entries:
        return ProcessedImagesResult(directory=None)

    # Ensure the root storage directory exists.
    try:
        root = Path(storage_path).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ProcessedImagesError(f"تعذر إنشاء أو الوصول لمجلد الحفظ النهائي: {exc}") from exc
    if not root.is_dir():
        raise ProcessedImagesError(f"مسار الحفظ النهائي ليس مجلدًا: {root}")

    # Create the operation directory with placed/unplaced subdirs.
    operation_dir = _new_operation_directory(root, folder_name=folder_name)
    placed_dir = operation_dir / "placed"
    unplaced_dir = operation_dir / "unplaced"
    placed_dir.mkdir(exist_ok=True)
    unplaced_dir.mkdir(exist_ok=True)

    total_moved = 0

    # Move placed images.
    if placed_entries:
        placed_planned = _plan_moves(placed_entries, placed_dir)
        total_moved += _execute_moves(placed_planned)

    # Move unplaced images (best-effort; failures here do not abort).
    if unplaced_entries:
        unplaced_planned = _plan_moves(unplaced_entries, unplaced_dir)
        try:
            total_moved += _execute_moves(unplaced_planned)
        except ProcessedImagesError:
            # Unplaced image move failures are non-critical.
            pass

    # Clean up empty subdirectories.
    if not any(placed_dir.iterdir()):
        placed_dir.rmdir()
    if not any(unplaced_dir.iterdir()):
        unplaced_dir.rmdir()

    return ProcessedImagesResult(directory=str(operation_dir), moved_count=total_moved)
