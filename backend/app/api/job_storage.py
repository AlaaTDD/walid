"""Filesystem job storage with atomic state writes and safe job ids."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from shapely import wkt as shapely_wkt
from shapely.geometry.base import BaseGeometry

from app.nesting.engine import PartInput, PlacedPart
from app.nesting.rotation import LockedRotation


def _default_jobs_root() -> Path:
    """Determine the default jobs storage directory.

    When running as a PyInstaller-frozen bundle (e.g. inside a macOS .app or a
    Windows installed executable), the directory containing the source files is
    read-only.  In that case we fall back to the platform's standard writable
    application-data directory:

      - macOS:   ~/Library/Application Support/SheetNestingApp/jobs
      - Windows: %LOCALAPPDATA%/SheetNestingApp/jobs
      - Linux:   ~/.local/share/SheetNestingApp/jobs

    The environment variable ``NESTING_JOBS_ROOT`` still overrides everything,
    allowing server/headless deployments to point at a custom directory.
    """
    env = os.getenv("NESTING_JOBS_ROOT")
    if env:
        return Path(env)

    # Frozen bundle (PyInstaller): use a writable user data directory.
    if getattr(sys, "frozen", False):
        import platform
        system = platform.system()
        if system == "Darwin":
            base = Path.home() / "Library" / "Application Support"
        elif system == "Windows":
            base = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        else:
            base = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        return base / "SheetNestingApp" / "jobs"

    # Development: relative to the source tree (original behavior).
    return Path(__file__).resolve().parent.parent.parent / "jobs"


DEFAULT_JOBS_ROOT = _default_jobs_root()


def _app_binary_dir() -> Path:
    """Directory containing the running executable, on any OS.

    - Frozen (PyInstaller) build: the folder holding the .exe/binary itself
      (e.g. .../ForClient/ on macOS, or the install folder on Windows),
      taken from ``sys.executable`` at run time -- never a path written in
      source, so it is correct no matter which machine/OS the .app or the
      installed .exe was moved to or run from.
    - Not frozen (plain ``python main.py`` during development): the ForClient/
      package folder relative to this source file, i.e. three levels up from
      backend/app/api/ -> backend/, then its ForClient/ sibling. Falls back to
      the current working directory if that layout is not present, so this
      never raises during development.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    dev_for_client = Path(__file__).resolve().parent.parent.parent.parent / "ForClient"
    if dev_for_client.is_dir():
        return dev_for_client
    return Path.cwd()


IMAGES_DIRNAME = "images"


def _default_images_root() -> Path:
    """Default folder, next to the running app, that holds client images.

    Layout created under it:
        images/
            uploaded/   -- every image added to a job (copied in as it is
                           uploaded, kept regardless of placed/unplaced)
            remaining/  -- images still unplaced/pending at last export
            final/      -- the last exported output.tiff for each job

    The environment variable ``NESTING_IMAGES_ROOT`` overrides this, for the
    same reason ``NESTING_JOBS_ROOT`` exists: custom/headless deployments.
    """
    env = os.getenv("NESTING_IMAGES_ROOT")
    if env:
        return Path(env)
    return _app_binary_dir() / IMAGES_DIRNAME


DEFAULT_IMAGES_ROOT = _default_images_root()
UPLOADED_IMAGES_DIRNAME = "uploaded"
REMAINING_IMAGES_DIRNAME = "remaining"
FINAL_TIFF_DIRNAME = "final"


def _images_job_dir(job_id: str, images_root: Path | None = None) -> Path:
    """Per-session (per-job) folder under images/: images/<job_id>/.

    Each job/session gets its own folder here, named with the same job_id
    UUID the job already uses everywhere else (job_state.json's own folder
    under DEFAULT_JOBS_ROOT, output_tiff_path, etc.) -- not a separately
    invented session id. validate_job_id() rejects anything that is not that
    exact UUID shape, so this can never be pointed at an arbitrary path.
    """
    root = DEFAULT_IMAGES_ROOT if images_root is None else images_root
    validate_job_id(job_id)
    return root / job_id


def images_job_dir(job_id: str, images_root: Path | None = None) -> Path:
    """Public accessor for images/<job_id>/ -- the whole per-job image folder
    (uploaded/, remaining/, final/ together), without creating it.

    Exists specifically so a caller that needs to REMOVE this tree (job
    deletion) can get the exact same path every images_*_dir() helper above
    already writes into, instead of reconstructing DEFAULT_IMAGES_ROOT / job_id
    by hand -- which would silently drift out of sync if this module's layout
    ever changes. Unlike ensure_images_job_dir(), this does not create the
    directory or its subfolders; a job that never had any images uploaded may
    have no images/<job_id>/ folder at all, and the caller (shutil.rmtree with
    ignore_errors=True) already handles a nonexistent path safely.
    """
    return _images_job_dir(job_id, images_root)


def images_uploaded_dir(job_id: str, images_root: Path | None = None) -> Path:
    """images/<job_id>/uploaded/ -- every image added to this one job."""
    path = _images_job_dir(job_id, images_root) / UPLOADED_IMAGES_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def images_remaining_dir(job_id: str, images_root: Path | None = None) -> Path:
    """images/<job_id>/remaining/ -- images still unplaced/pending for this job."""
    path = _images_job_dir(job_id, images_root) / REMAINING_IMAGES_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def images_final_dir(job_id: str, images_root: Path | None = None) -> Path:
    """images/<job_id>/final/ -- this job's exported output.tiff."""
    path = _images_job_dir(job_id, images_root) / FINAL_TIFF_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_images_job_dir(job_id: str, images_root: Path | None = None) -> Path:
    """Create images/<job_id>/ with its uploaded/remaining/final subfolders.

    Call this once per job/session (e.g. alongside create_job()) so every
    session gets its own three subfolders instead of one shared set.
    """
    job_dir = _images_job_dir(job_id, images_root)
    job_dir.mkdir(parents=True, exist_ok=True)
    images_uploaded_dir(job_id, images_root)
    images_remaining_dir(job_id, images_root)
    images_final_dir(job_id, images_root)
    return job_dir


JOB_STATE_FILENAME = "job_state.json"
UPLOADS_DIRNAME = "uploads"
OUTPUT_TIFF_FILENAME = "output.tiff"
# Append-only durability log. Each uploaded image writes ONE line here instead
# of rewriting the entire job_state.json (which would be O(n^2) over a batch
# of N images, since job_state.json's payload grows with every part already
# recorded). If the process dies mid-batch, load_job_state() replays any lines
# here that are not yet reflected in job_state.json, so the same durability
# guarantee as before is kept: everything committed before the crash survives.
PENDING_PARTS_FILENAME = "pending_parts.ndjson"
_JOB_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class JobStorageError(Exception):
    pass


class JobNotFoundError(JobStorageError):
    pass


def _resolve_jobs_root(jobs_root: Path | None) -> Path:
    """Resolve the jobs root at call time so tests can safely isolate storage."""
    return DEFAULT_JOBS_ROOT if jobs_root is None else jobs_root


@dataclass
class StoredPart:
    part_id: str
    client_part_id: str
    content_sha256: str
    original_filename: str
    stored_image_path: str
    is_valid: bool
    # The desktop client sends this only for a locally reachable source file.
    # It is deliberately separate from stored_image_path, which is the server
    # upload copy used for rasterisation and remains safe to read during work.
    original_source_path: str | None = None
    rejection_reason: str | None = None
    contour_wkt: str | None = None
    source_width_px: int | None = None
    source_height_px: int | None = None
    source_centroid_x_px: float | None = None
    source_centroid_y_px: float | None = None
    alpha_bbox_x0_px: int | None = None
    alpha_bbox_y0_px: int | None = None
    alpha_bbox_x1_px: int | None = None
    alpha_bbox_y1_px: int | None = None


def _unique_client_facing_name(
    directory: Path,
    original_filename: str,
    fallback: str,
) -> Path:
    """Pick a filesystem-safe destination inside ``directory``.

    The client only ever sees images/<job_id>/{uploaded,remaining,final}/, so
    names there use the client's own original_filename (not the internal
    part_id) -- the same file the client dragged in should be recognisable
    by name in this folder on either macOS or Windows. Path separators/null
    bytes are stripped so a crafted filename can never escape this directory,
    and a name collision (two different uploads sharing one filename) gets a
    numeric suffix instead of silently overwriting the earlier file.
    """
    candidate = Path(original_filename).name.strip()
    candidate = re.sub(r"[\x00/\\]", "_", candidate) or fallback
    destination = directory / candidate
    if not destination.exists():
        return destination
    stem, suffix = destination.stem, destination.suffix
    index = 1
    while True:
        destination = directory / f"{stem}_{index}{suffix}"
        if not destination.exists():
            return destination
        index += 1


def copy_uploaded_image_into_images_dir(
    job_id: str,
    part: StoredPart,
    images_root: Path | None = None,
) -> None:
    """Copy one just-saved upload into images/<job_id>/uploaded/.

    This is a COPY, not a move: stored_image_path (under DEFAULT_JOBS_ROOT)
    remains the path every downstream stage (contour extraction, nesting,
    compositing, TIFF export) actually reads from, and must keep existing
    unchanged regardless of whether this client-visible copy succeeds. A
    copy failure here (e.g. a read-only images/ folder) is therefore
    swallowed rather than raised -- it must never block or roll back an
    otherwise-successful upload, since the durable upload record
    (append_pending_part/save_job_state) already committed before this runs.
    """
    source = Path(part.stored_image_path)
    if not source.is_file():
        return
    try:
        destination_dir = images_uploaded_dir(job_id, images_root)
        destination = _unique_client_facing_name(
            destination_dir, part.original_filename, f"image_{part.part_id}{source.suffix}"
        )
        shutil.copy2(str(source), str(destination))
    except OSError:
        pass


def sync_images_final_and_remaining(
    job_id: str,
    parts: list[StoredPart],
    unplaced_part_ids: set[str],
    exported_tiff_path: str | Path,
    images_root: Path | None = None,
) -> None:
    """After a successful confirm/export, refresh images/<job_id>/final/ and
    images/<job_id>/remaining/ so the client-visible folder always reflects
    the LATEST export -- not an accumulation across every /layout/confirm
    call for the same job (a recompute+re-export must not leave stale
    remaining/ images from a superseded layout sitting next to the current
    ones).

    final/      -- cleared, then the just-exported TIFF is copied in.
    remaining/  -- cleared, then every part that is either explicitly
                   unplaced (in unplaced_part_ids) or was never placed at all
                   (rejected, is_valid=False) is copied in. A rejected image
                   was never part of the nesting run and therefore can never
                   appear in unplaced_part_ids, but from the client's own
                   point of view it is just as much "still remaining" as an
                   unplaced-but-valid one.

    Best-effort by design (see copy_uploaded_image_into_images_dir): a
    filesystem problem here must not fail an otherwise-successful export,
    since output_tiff_path under DEFAULT_JOBS_ROOT is already the durable,
    verified result this function only mirrors for client visibility.
    """
    try:
        final_dir = images_final_dir(job_id, images_root)
        remaining_dir = images_remaining_dir(job_id, images_root)
    except OSError:
        return

    def _clear_dir(directory: Path) -> None:
        for existing in directory.iterdir():
            try:
                if existing.is_file() or existing.is_symlink():
                    existing.unlink(missing_ok=True)
                elif existing.is_dir():
                    shutil.rmtree(existing, ignore_errors=True)
            except OSError:
                pass

    try:
        _clear_dir(final_dir)
        tiff_source = Path(exported_tiff_path)
        if tiff_source.is_file():
            shutil.copy2(str(tiff_source), str(final_dir / tiff_source.name))
    except OSError:
        pass

    try:
        _clear_dir(remaining_dir)
        for part in parts:
            still_remaining = (not part.is_valid) or (part.part_id in unplaced_part_ids)
            if not still_remaining:
                continue
            source = Path(part.stored_image_path)
            if not source.is_file():
                continue
            destination = _unique_client_facing_name(
                remaining_dir, part.original_filename, f"image_{part.part_id}{source.suffix}"
            )
            try:
                shutil.copy2(str(source), str(destination))
            except OSError:
                pass
    except OSError:
        pass


@dataclass
class StoredPlacedPart:
    part_id: str
    source_image_path: str
    placed_shape_wkt: str
    rotation_deg: int


@dataclass
class StoredSheet:
    page_number: int
    placed_parts: list[StoredPlacedPart] = field(default_factory=list)


@dataclass
class JobState:
    job_id: str
    stage: str = "uploaded"
    parts: list[StoredPart] = field(default_factory=list)
    sheet_width_mm: float | None = None
    sheet_height_mm: float | None = None
    sheet_margin_mm: float | None = None
    clearance_mm: float | None = None
    dpi: float | None = None
    packing_attempts: int | None = None
    source_dpi: float | None = None
    placed_parts: list[StoredPlacedPart] = field(default_factory=list)
    sheets: list[StoredSheet] = field(default_factory=list)
    # False marks a layout written before the page collection was introduced.
    # It lets the API recompute old saved results with the current one-sheet
    # capacity behavior instead of returning obsolete multi-page output.
    multi_sheet_layout: bool = False
    unplaced_part_ids: list[str] = field(default_factory=list)
    output_tiff_path: str | None = None
    layout_message: str | None = None
    sheet_full: bool = False
    output_export_accepted: bool | None = None
    output_width_px: int | None = None
    output_height_px: int | None = None
    output_dpi: float | None = None
    output_layer_count: int = 0
    background_color: str | None = None
    qa_violations: list[dict] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    # Cache of the last collision report computed for this exact placed-parts
    # layout, so GET /jobs/{id} does not re-run the full O(n log n) collision
    # check (STRtree query + exact GEOS predicates per candidate pair) on every
    # poll. Invalidated (fields reset to None) whenever placed_parts changes.
    cached_collision_signature: str | None = None
    cached_collision_is_valid: bool | None = None
    cached_collision_violations: list[dict] = field(default_factory=list)
    cached_collision_checked_pairs: int = 0


def validate_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise JobNotFoundError(f"معرّف job غير صالح: {job_id!r}")
    return job_id


def _job_dir(job_id: str, jobs_root: Path | None = None) -> Path:
    jobs_root = _resolve_jobs_root(jobs_root)
    validate_job_id(job_id)
    return jobs_root / job_id


def _job_state_path(job_id: str, jobs_root: Path | None = None) -> Path:
    return _job_dir(job_id, jobs_root) / JOB_STATE_FILENAME


def uploads_dir(job_id: str, jobs_root: Path | None = None) -> Path:
    return _job_dir(job_id, jobs_root) / UPLOADS_DIRNAME


def output_tiff_path(job_id: str, jobs_root: Path | None = None) -> Path:
    return _job_dir(job_id, jobs_root) / OUTPUT_TIFF_FILENAME


def create_job(jobs_root: Path | None = None) -> JobState:
    jobs_root = _resolve_jobs_root(jobs_root)
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    job_dir = _job_dir(job_id, jobs_root)
    job_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir(job_id, jobs_root).mkdir(parents=True, exist_ok=True)
    state = JobState(job_id=job_id, created_at=now, updated_at=now)
    save_job_state(state, jobs_root)
    return state


def _pending_parts_path(job_id: str, jobs_root: Path | None = None) -> Path:
    return _job_dir(job_id, jobs_root) / PENDING_PARTS_FILENAME


def save_job_state(state: JobState, jobs_root: Path | None = None) -> None:
    """Write the full job state atomically.

    This remains the single source of truth on disk. It is intentionally kept
    O(n) per call (payload size = number of parts) and is meant to be called
    at durability checkpoints (job creation, end of an upload batch, after
    compute, after confirm) rather than once per individual image — use
    append_pending_part() for per-image durability during a batch instead.
    """
    jobs_root = _resolve_jobs_root(jobs_root)
    state.updated_at = datetime.now(timezone.utc).isoformat()
    state_path = _job_state_path(state.job_id, jobs_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(state), ensure_ascii=False, separators=(",", ":"))
    tmp_path = state_path.with_suffix(".json.tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(state_path)
    # Everything in the pending log is now durably reflected in job_state.json
    # itself, so the log can be cleared. A crash between the two lines above
    # and this one just leaves a harmless already-applied log for the next
    # load_job_state() call to skip over (client_part_id dedup below).
    _pending_parts_path(state.job_id, jobs_root).unlink(missing_ok=True)


def append_pending_part(job_id: str, part: StoredPart, jobs_root: Path | None = None) -> None:
    """Durably record ONE newly analyzed part without rewriting job_state.json.

    Appends a single JSON line to a per-job ndjson log. This is O(1) per call
    (the write size is one part's data, not the whole job's), which keeps a
    batch of N uploaded images at O(N) total I/O instead of O(N^2). The next
    save_job_state() call (end of the upload batch) folds this log into
    job_state.json and clears it. If the process crashes mid-batch,
    load_job_state() replays this log so every part durably appended before
    the crash is still recovered — the same guarantee the previous
    'save full state after every image' approach provided.
    """
    jobs_root = _resolve_jobs_root(jobs_root)
    path = _pending_parts_path(job_id, jobs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(part), ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


def _parts_from_payload(raw_parts: list[dict]) -> list[StoredPart]:
    return [StoredPart(
        part_id=p["part_id"],
        client_part_id=p.get("client_part_id", p["part_id"]),
        content_sha256=p.get("content_sha256", ""),
        original_filename=p.get("original_filename", "unknown"),
        stored_image_path=p.get("stored_image_path", ""),
        original_source_path=p.get("original_source_path"),
        is_valid=bool(p.get("is_valid", False)),
        rejection_reason=p.get("rejection_reason"),
        contour_wkt=p.get("contour_wkt"),
        source_width_px=p.get("source_width_px"),
        source_height_px=p.get("source_height_px"),
        source_centroid_x_px=p.get("source_centroid_x_px"),
        source_centroid_y_px=p.get("source_centroid_y_px"),
        alpha_bbox_x0_px=p.get("alpha_bbox_x0_px"),
        alpha_bbox_y0_px=p.get("alpha_bbox_y0_px"),
        alpha_bbox_x1_px=p.get("alpha_bbox_x1_px"),
        alpha_bbox_y1_px=p.get("alpha_bbox_y1_px"),
    ) for p in raw_parts]


def _replay_pending_parts(job_id: str, parts: list[StoredPart], jobs_root: Path | None = None) -> list[StoredPart]:
    """Merge any parts recorded via append_pending_part() but not yet folded
    into job_state.json (i.e. the process crashed mid-batch before the next
    save_job_state()). Returns parts unchanged if there is nothing to replay.
    """
    jobs_root = _resolve_jobs_root(jobs_root)
    pending_path = _pending_parts_path(job_id, jobs_root)
    if not pending_path.exists():
        return parts
    try:
        raw_lines = [ln for ln in pending_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return parts
    if not raw_lines:
        return parts
    known_client_ids = {p.client_part_id for p in parts}
    replayed: list[StoredPart] = []
    for line in raw_lines:
        try:
            raw_part = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = _parts_from_payload([raw_part])[0]
        if part.client_part_id in known_client_ids:
            continue  # already folded into job_state.json by a prior save_job_state
        known_client_ids.add(part.client_part_id)
        replayed.append(part)
    if not replayed:
        return parts
    return [*parts, *replayed]


def load_job_state(job_id: str, jobs_root: Path | None = None) -> JobState:
    jobs_root = _resolve_jobs_root(jobs_root)
    state_path = _job_state_path(job_id, jobs_root)
    if not state_path.exists():
        raise JobNotFoundError(f"الـ job المطلوب غير موجود: {job_id!r}")
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        parts = _parts_from_payload(payload.get("parts", []))
        parts = _replay_pending_parts(job_id, parts, jobs_root)
        placed_parts = [StoredPlacedPart(**p) for p in payload.get("placed_parts", [])]
        raw_sheets = payload.get("sheets", [])
        sheets = [
            StoredSheet(
                page_number=int(raw_sheet.get("page_number", index + 1)),
                placed_parts=[StoredPlacedPart(**part) for part in raw_sheet.get("placed_parts", [])],
            )
            for index, raw_sheet in enumerate(raw_sheets)
            if isinstance(raw_sheet, dict)
        ]
        # Older jobs predate multi-sheet storage. Their existing layout is
        # exactly one page and remains fully usable after an app upgrade.
        if not sheets and placed_parts:
            sheets = [StoredSheet(page_number=1, placed_parts=placed_parts)]
        return JobState(
            job_id=payload["job_id"],
            stage=payload.get("stage", "uploaded"),
            parts=parts,
            sheet_width_mm=payload.get("sheet_width_mm"),
            sheet_height_mm=payload.get("sheet_height_mm"),
            sheet_margin_mm=payload.get("sheet_margin_mm"),
            clearance_mm=payload.get("clearance_mm"),
            dpi=payload.get("dpi"),
            packing_attempts=payload.get("packing_attempts"),
            source_dpi=payload.get("source_dpi"),
            placed_parts=placed_parts,
            sheets=sheets,
            multi_sheet_layout=bool(payload.get("multi_sheet_layout", False)),
            unplaced_part_ids=payload.get("unplaced_part_ids", []),
            output_tiff_path=payload.get("output_tiff_path"),
            layout_message=payload.get("layout_message"),
            sheet_full=bool(payload.get("sheet_full", False)),
            output_export_accepted=payload.get("output_export_accepted"),
            output_width_px=payload.get("output_width_px"),
            output_height_px=payload.get("output_height_px"),
            output_dpi=payload.get("output_dpi"),
            output_layer_count=int(payload.get("output_layer_count", 0)),
            background_color=payload.get("background_color"),
            qa_violations=payload.get("qa_violations", []),
            created_at=payload.get("created_at", ""),
            updated_at=payload.get("updated_at", ""),
            cached_collision_signature=payload.get("cached_collision_signature"),
            cached_collision_is_valid=payload.get("cached_collision_is_valid"),
            cached_collision_violations=payload.get("cached_collision_violations", []),
            cached_collision_checked_pairs=payload.get("cached_collision_checked_pairs", 0),
        )
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as exc:
        raise JobStorageError(f"فشل قراءة حالة الـ job {job_id!r}: {exc}") from exc


def geometry_to_wkt(shape: BaseGeometry) -> str:
    return shapely_wkt.dumps(shape, trim=False)


def wkt_to_geometry(wkt_string: str) -> BaseGeometry:
    return shapely_wkt.loads(wkt_string)


def part_inputs_from_state(state: JobState) -> dict[str, PartInput]:
    result: dict[str, PartInput] = {}
    for part in state.parts:
        if not part.is_valid or part.contour_wkt is None:
            continue
        if part.source_centroid_x_px is None or part.source_centroid_y_px is None:
            # Backward-compatible fallback for old state files.  The current
            # uploader always stores this metadata and avoids a second image parse.
            centroid = wkt_to_geometry(part.contour_wkt).centroid
            from app.geometry.units import Resolution, mm_to_px
            dpi = state.dpi or 300.0
            resolution = Resolution(dpi=dpi)
            centroid_px = (mm_to_px(float(centroid.x), resolution), mm_to_px(float(centroid.y), resolution))
        else:
            centroid_px = (part.source_centroid_x_px, part.source_centroid_y_px)

        # contour_wkt already carries the vertex-reduced, simplified contour produced
        # once at upload time (geometry/contour.py's extract_contour_from_rgba, which
        # applies simplify(tolerance) to the raw traced contour before it is ever
        # stored). Re-applying a second, independent buffer(2.0)->buffer(-4.0)->
        # buffer(2.0)->simplify(1.0) smoothing pass here on every single compute() call
        # drifted the geometry a second time away from the already-simplified upload
        # contour, contradicting this project's documented 100%-geometric-precision
        # principle (see geometry/units.py, nesting/nfp.py) for no measurable benefit --
        # the vertex-count reduction this comment described (200 -> 20) is already fully
        # achieved once at upload, not something this second pass still needs to do.
        shape_mm = wkt_to_geometry(part.contour_wkt)
        if not shape_mm.is_valid:
            shape_mm = shape_mm.buffer(0)
        
        result[part.part_id] = PartInput(
            shape_mm=shape_mm,
            source_image_path=part.stored_image_path,
            source_centroid_px=centroid_px,
            alpha_bbox_px=(
                part.alpha_bbox_x0_px or 0,
                part.alpha_bbox_y0_px or 0,
                part.alpha_bbox_x1_px or (part.source_width_px or 0),
                part.alpha_bbox_y1_px or (part.source_height_px or 0),
            ),
        )
    return result


def placed_parts_to_state(placed: list[PlacedPart]) -> list[StoredPlacedPart]:
    return [
        StoredPlacedPart(
            part_id=p.part_id,
            source_image_path=p.source_image_path,
            placed_shape_wkt=geometry_to_wkt(p.placed_shape_mm),
            rotation_deg=int(p.rotation.value),
        )
        for p in placed
    ]


def sheets_to_state(sheets: list[list[PlacedPart]]) -> list[StoredSheet]:
    return [
        StoredSheet(page_number=index + 1, placed_parts=placed_parts_to_state(placed))
        for index, placed in enumerate(sheets)
    ]


def sheets_from_state(state: JobState) -> list[list[PlacedPart]]:
    stored_sheets = state.sheets
    if not stored_sheets and state.placed_parts:
        stored_sheets = [StoredSheet(page_number=1, placed_parts=state.placed_parts)]
    return [placed_parts_from_state(sheet.placed_parts) for sheet in stored_sheets]


def placed_parts_from_state(stored: list[StoredPlacedPart]) -> list[PlacedPart]:
    return [
        PlacedPart(
            part_id=p.part_id,
            source_image_path=p.source_image_path,
            placed_shape_mm=wkt_to_geometry(p.placed_shape_wkt),
            rotation=LockedRotation(p.rotation_deg),
        )
        for p in stored
    ]
