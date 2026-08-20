"""FastAPI application for image analysis, exact nesting and TIFF export."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from PIL import ImageColor
from app.api.job_storage import (
    DEFAULT_JOBS_ROOT,
    JobNotFoundError,
    StoredPart,
    append_pending_part,
    create_job,
    load_job_state,
    output_tiff_path,
    part_inputs_from_state,
    placed_parts_to_state,
    save_job_state,
    sheets_from_state,
    sheets_to_state,
    uploads_dir,
)
from app.api.processed_images import ProcessedImagesError, move_processed_originals
from app.api.schemas import (
    ComputeRequest,
    ContourPointPreview,
    CreateJobResponse,
    JobStatusResponse,
    JobPartStatus,
    ComputeResponse,
    ConfirmRequest,
    ConfirmResponse,
    PlacedPartPreview,
    SheetPreview,
    ProgressResponse,
    QaViolationResponse,
    UploadResponse,
    UploadedPartResult,
    ViolationPreview,
)
from app.core_logging import get_logger
from app.geometry.contour import ContourExtractionError, extract_contour_from_image
from app.geometry.units import Resolution
from app.image_safety import MAX_INPUT_IMAGE_PIXELS, open_image_with_limit
from app.nesting.collision import CollisionReport, ValidationViolation, validate_layout
from app.nesting.compaction import compact_layout
from app.nesting.engine import NestingCancelledError, run_best_single_sheet_nesting
from app.nesting.lns import run_lns_optimization, run_local_reoptimization
from app.rasterization.tiff_export import export_multi_sheet_tiff
from app.validation.alpha_check import normalize_open_image_to_rgba, validate_open_rgba_image
from app.validation.qa_check import run_qa_check

logger = get_logger(__name__)

# A real production source may be hundreds of megabytes.  The default is one
# GiB, and an installation can still set NESTING_MAX_UPLOAD_BYTES explicitly
# when its disk/network policy requires a different hard ceiling.
MAX_UPLOAD_BYTES = int(os.getenv("NESTING_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
ALLOWED_DPI = float(os.getenv("NESTING_DEFAULT_DPI", "300"))
DEFAULT_JOBS_ROOT.mkdir(parents=True, exist_ok=True)


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        logger.warning("Invalid %s value; using %d.", name, default)
        return default


# Image decoding/contour extraction and GEOS nesting are CPU-bound.  Letting a
# large upload create dozens of workers or two layouts consume every core makes
# the desktop unresponsive even though each individual operation is valid.
# These deliberately conservative limits keep one machine responsive; they
# remain configurable for a stronger dedicated server.
MAX_PARALLEL_IMAGE_ANALYSES = _positive_env_int(
    "NESTING_MAX_PARALLEL_IMAGE_ANALYSES",
    min(4, max(1, (os.cpu_count() or 2) // 2)),
)
MAX_CONCURRENT_NESTING_JOBS = _positive_env_int("NESTING_MAX_CONCURRENT_NESTING_JOBS", 1)

_cancelled_jobs: set[str] = set()
_progress_jobs: dict[str, tuple[int, int, int, str | None]] = {}
_progress_versions: dict[str, int] = {}
_progress_waiters: dict[str, set[asyncio.Event]] = {}
_finished_progress_jobs: set[str] = set()
_job_locks: dict[str, asyncio.Lock] = {}
_job_lock_guard = asyncio.Lock()
_nesting_capacity = asyncio.Semaphore(MAX_CONCURRENT_NESTING_JOBS)
_app_event_loop: asyncio.AbstractEventLoop | None = None
_PROGRESS_HEARTBEAT_SECONDS = 20
_PROGRESS_CLEANUP_DELAY_SECONDS = 60
_PROGRESS_EMIT_MIN_INTERVAL_SECONDS = 0.15


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Keep the one server loop used to deliver worker-thread progress."""
    global _app_event_loop
    _app_event_loop = asyncio.get_running_loop()
    yield


app = FastAPI(
    title="Sheet Nesting Backend",
    description="High-precision irregular image nesting for print sheets.",
    version="1.0.0",
    lifespan=_lifespan,
)
# Security review finding: this backend is designed to run locally with no
# authentication on any endpoint (upload, compute, confirm/export, delete --
# see README.md and run_server.py's own "Images stay on this device" banner).
# allow_origins=["*"] previously meant ANY website open in ANY browser tab on
# this machine could script a cross-origin fetch() to this server -- list job
# data, upload attacker-chosen images into an existing job, or trigger an
# export that writes files via processed_images_path -- entirely without the
# user's knowledge, since a wildcard CORS policy lets the browser both send
# the request AND read the response regardless of which page initiated it.
# The frontend only ever talks to a fixed local origin (localhost/127.0.0.1,
# any port -- see web/src/lib/nestingApi.ts's own "browser -> loopback ->
# Python" comment and its LOCAL_BACKEND_URL default), so restricting to that
# origin family costs the app nothing while closing this cross-origin path.
# The regex intentionally allows any port (the desktop frontend's dev/prod
# port can vary) but only the loopback hostnames themselves -- not "*" and
# not a broader private-network range, since this app has no legitimate
# reason to be called from a different device's browser.
ALLOWED_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _notify_progress_waiters(job_id: str) -> None:
    for waiter in tuple(_progress_waiters.get(job_id, ())):
        waiter.set()


def _set_progress(
    job_id: str,
    progress: tuple[int, int, int, str | None],
    *,
    finished: bool = False,
) -> None:
    """Publish one in-memory progress change to any SSE listeners.

    This runs only on FastAPI's event loop.  The nesting solver runs in a
    worker thread and uses `_set_progress_from_worker` below to enter this
    loop safely.
    """
    _progress_jobs[job_id] = progress
    _progress_versions[job_id] = _progress_versions.get(job_id, 0) + 1
    if finished:
        _finished_progress_jobs.add(job_id)
        if _app_event_loop is not None:
            _app_event_loop.call_later(
                _PROGRESS_CLEANUP_DELAY_SECONDS,
                _clear_finished_progress,
                job_id,
                _progress_versions[job_id],
            )
    else:
        _finished_progress_jobs.discard(job_id)
    _notify_progress_waiters(job_id)


def _clear_finished_progress(job_id: str, finished_version: int) -> None:
    """Release a completed stream snapshot after clients had time to see it."""
    if (
        job_id in _finished_progress_jobs
        and _progress_versions.get(job_id) == finished_version
    ):
        _progress_jobs.pop(job_id, None)
        _progress_versions.pop(job_id, None)
        _finished_progress_jobs.discard(job_id)


def _set_progress_from_worker(
    job_id: str,
    progress: tuple[int, int, int, str | None],
) -> None:
    """Schedule a progress event from the CPU-bound nesting worker thread."""
    loop = _app_event_loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(_set_progress, job_id, progress)


def _finish_progress(job_id: str, message: str | None = None) -> None:
    done, total, placed, current_message = _progress_jobs.get(job_id, (0, 0, 0, None))
    _set_progress(
        job_id,
        (done, total, placed, message if message is not None else current_message),
        finished=True,
    )


async def _get_job_lock(job_id: str) -> asyncio.Lock:
    async with _job_lock_guard:
        return _job_locks.setdefault(job_id, asyncio.Lock())


def _upload_message(total: int, placed: int, unplaced: int) -> str:
    if unplaced:
        return (
            f"تم ملء ورقة TIFF واحدة بأقصى ترتيب صالح: {placed} من أصل {total}. "
            f"الصور الباقية ({unplaced}) لم تدخل هذه الورقة وستبقى في المصدر."
        )
    return f"اكتمل ترتيب كل الصور: {placed} من {total} على ورقة TIFF واحدة."


def _background_rgba(value: str) -> tuple[int, int, int, int]:
    """Parse a user-selected solid background without hardcoded colour rules."""
    try:
        rgba = ImageColor.getcolor(value.strip(), "RGBA")
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="لون الخلفية غير صالح. استخدم اسمًا مثل white أو قيمة مثل #1A2B3C.",
        ) from exc
    if rgba[3] != 255:
        raise HTTPException(
            status_code=422,
            detail="خلفية صفحة TIFF يجب أن تكون بلون معتم بالكامل (alpha = 255).",
        )
    return rgba


async def _save_upload(uploaded_file: UploadFile, destination: Path) -> int:
    total = 0
    with destination.open("wb") as output:
        while True:
            chunk = await uploaded_file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                output.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"حجم الصورة أكبر من الحد المسموح ({MAX_UPLOAD_BYTES} bytes).",
                )
            output.write(chunk)
    return total


def _analyze_saved_image(path: Path, resolution: Resolution):
    with open_image_with_limit(path, max_pixels=MAX_INPUT_IMAGE_PIXELS) as image:
        image.load()
        normalized = normalize_open_image_to_rgba(image)
        try:
            validation = validate_open_rgba_image(normalized, path)
            if not validation.is_valid:
                return validation, None
            try:
                contour = extract_contour_from_image(normalized, resolution)
            except ContourExtractionError as exc:
                validation = type(validation)(str(path), False, f"فشل استخراج الـcontour: {exc}")
                return validation, None
            return validation, contour
        finally:
            if normalized is not image:
                normalized.close()


def _store_analysis_result(part: StoredPart, validation, contour) -> None:
    """Apply a decoded-image analysis result to durable part metadata."""
    part.is_valid = bool(validation.is_valid)
    part.rejection_reason = validation.rejection_reason
    if contour is None:
        part.contour_wkt = None
        part.source_width_px = None
        part.source_height_px = None
        part.source_centroid_x_px = None
        part.source_centroid_y_px = None
        part.alpha_bbox_x0_px = None
        part.alpha_bbox_y0_px = None
        part.alpha_bbox_x1_px = None
        part.alpha_bbox_y1_px = None
        return

    from app.api.job_storage import geometry_to_wkt

    part.contour_wkt = geometry_to_wkt(contour.polygon_mm)
    part.source_width_px = contour.source_width_px
    part.source_height_px = contour.source_height_px
    part.source_centroid_x_px = contour.source_centroid_px[0]
    part.source_centroid_y_px = contour.source_centroid_px[1]
    x0, y0, x1, y1 = contour.alpha_bbox_px
    part.alpha_bbox_x0_px = x0
    part.alpha_bbox_y0_px = y0
    part.alpha_bbox_x1_px = x1
    part.alpha_bbox_y1_px = y1


def _is_legacy_alpha_rejection(part: StoredPart) -> bool:
    """Whether a former strict-RGBA rule is the only rejection reason."""
    reason = part.rejection_reason or ""
    return (
        reason.startswith("الصورة بصيغة ") and "ليست RGBA" in reason
    ) or reason.startswith("الصورة معتمة بالكامل")


def _upgrade_legacy_alpha_rejections(state) -> bool:
    """Reanalyze old RGB/opaque rejections once after the format-policy upgrade."""
    candidates = [
        part
        for part in state.parts
        if not part.is_valid and _is_legacy_alpha_rejection(part)
    ]
    if not candidates:
        return False

    resolution = Resolution(dpi=float(state.source_dpi or ALLOWED_DPI))
    changed = False
    for part in candidates:
        try:
            validation, contour = _analyze_saved_image(
                Path(part.stored_image_path), resolution
            )
        except Exception:
            logger.warning(
                "cannot upgrade legacy rejected image part=%s job=%s",
                part.part_id,
                state.job_id,
                exc_info=True,
            )
            continue
        _store_analysis_result(part, validation, contour)
        changed = True

    if not changed:
        return False

    # A previously rejected part may now join the layout, making a cached
    # partial computation/export stale.
    state.stage = "uploaded"
    state.placed_parts = []
    state.sheets = []
    state.unplaced_part_ids = []
    state.layout_message = None
    state.sheet_full = False
    state.output_tiff_path = None
    state.output_export_accepted = None
    state.output_width_px = None
    state.output_height_px = None
    state.output_dpi = None
    state.output_layer_count = 0
    state.background_color = None
    state.processed_images_path = None
    state.processed_images_directory = None
    state.moved_processed_images_count = 0
    state.qa_violations = []
    state.cached_collision_signature = None
    state.cached_collision_is_valid = None
    state.cached_collision_violations = []
    state.cached_collision_checked_pairs = 0
    save_job_state(state)
    return True


def _job_status_payload(state) -> CreateJobResponse:
    valid = sum(1 for part in state.parts if part.is_valid)
    rejected = len(state.parts) - valid
    return CreateJobResponse(
        job_id=state.job_id,
        stage=state.stage,
        created_at=state.created_at,
        updated_at=state.updated_at,
        source_dpi=state.source_dpi,
        received_count=len(state.parts),
        valid_count=valid,
        rejected_count=rejected,
        total_count=len(state.parts),
        upload_complete=bool(state.parts) and all(part.is_valid for part in state.parts),
        output_available=bool(state.output_tiff_path and Path(state.output_tiff_path).exists()),
    )


def _exterior_ring_mm(placed_shape_mm) -> list[ContourPointPreview]:
    """Exact exterior ring of a placed part's real (possibly irregular) shape.

    placed_shape_mm is normally a single Polygon, but PartInput.shape_mm (and
    therefore placed_shape_mm after rotation/translation) can in principle be
    a MultiPolygon if contour extraction's unary_union ever merges disjoint
    alpha regions (see geometry/contour.py). There is no single exterior ring
    for disjoint pieces, and the frontend canvas only ever draws one closed
    path per part id, so the largest-area piece is used by convention -- the
    same defensive MultiPolygon handling already used elsewhere in this
    codebase (see nesting/benchmark.py's _draw_layout). This never raises for
    a normal single-Polygon part, which is the overwhelming common case.
    """
    geom = placed_shape_mm
    if geom.geom_type == "MultiPolygon":
        polygons = [g for g in geom.geoms if g.geom_type == "Polygon" and g.area > 0]
        if not polygons:
            return []
        geom = max(polygons, key=lambda g: g.area)
    if geom.geom_type != "Polygon":
        return []
    # [:-1] drops the exterior ring's closing point (Shapely repeats the
    # first coordinate at the end), matching how the frontend already treats
    # contourMm as an implicitly-closed path (SheetLayoutCanvas.tsx calls
    # path.closePath() itself after the last point).
    return [ContourPointPreview(x_mm=x, y_mm=y) for x, y in list(geom.exterior.coords)[:-1]]


def _placed_previews(placed_parts):
    previews = []
    for part in placed_parts:
        bounds = part.placed_shape_mm.bounds
        centroid = part.placed_shape_mm.centroid
        previews.append(
            PlacedPartPreview(
                part_id=part.part_id,
                rotation_deg=int(part.rotation.value),
                bounds_min_x_mm=bounds[0],
                bounds_min_y_mm=bounds[1],
                bounds_max_x_mm=bounds[2],
                bounds_max_y_mm=bounds[3],
                centroid_x_mm=centroid.x,
                centroid_y_mm=centroid.y,
                contour_mm=_exterior_ring_mm(part.placed_shape_mm),
            )
        )
    return previews


def _collision_signature(state) -> str:
    """Cheap fingerprint of everything validate_layout()'s result depends on:
    the placed-parts layout (shape + rotation, already stored as WKT) and the
    sheet settings. Recomputing this is O(n) string hashing on data already
    in memory — negligible next to validate_layout()'s O(n log n) spatial
    query plus exact GEOS predicates per candidate pair. If this signature
    matches state.cached_collision_signature, the cached report is still
    exactly valid for the current layout and settings, so validate_layout()
    does not need to run again.
    """
    stored_sheets = state.sheets or []
    if not stored_sheets and state.placed_parts:
        # Backward compatibility for layouts saved before multi-sheet support.
        parts_fingerprint = "|".join(
            f"1:{p.part_id}:{p.rotation_deg}:{p.placed_shape_wkt}" for p in state.placed_parts
        )
    else:
        parts_fingerprint = "|".join(
            f"{sheet.page_number}:{part.part_id}:{part.rotation_deg}:{part.placed_shape_wkt}"
            for sheet in stored_sheets
            for part in sheet.placed_parts
        )
    settings_fingerprint = f"{state.sheet_width_mm}:{state.sheet_height_mm}:{state.sheet_margin_mm}:{state.clearance_mm}"
    return hashlib.sha256(f"{settings_fingerprint}#{parts_fingerprint}".encode("utf-8")).hexdigest()


def _sheet_preview(page_number: int, placed, collision_report) -> SheetPreview:
    return SheetPreview(
        page_number=page_number,
        placed_parts=_placed_previews(placed),
        collision_report_valid=collision_report.is_valid,
        violations=[
            ViolationPreview(
                severity=violation.severity,
                part_id_a=violation.part_id_a,
                part_id_b=violation.part_id_b,
                detail=violation.detail,
                measured_distance_mm=violation.measured_distance_mm,
            )
            for violation in collision_report.violations
        ],
    )


def _reports_from_cached_violations(state, placed_sheets):
    """Split the flat, cross-sheet cached_collision_violations back into one
    CollisionReport per sheet, keyed by which sheet each violation's part(s)
    actually belong to.

    state.cached_collision_violations is stored as a single flattened list
    across every sheet (see the write site in compute_layout, which builds it
    from ``for report in collision_reports for v in report.violations``), but
    _stored_compute_response needs one CollisionReport per sheet -- the same
    per-sheet shape validate_layout() itself returns. A violation's part_id_a
    is always present and sufficient to identify its sheet (validate_layout is
    called once per sheet, so both parts in a pairwise violation are
    necessarily on the same sheet as part_id_a); part_id_b is None for
    single-part violations (OUT_OF_BOUNDS).

    checked_pairs_count is not reconstructed per-sheet from the cache (only
    the aggregate total is stored, in cached_collision_checked_pairs) -- it is
    purely a diagnostic/logging figure, never used to gate acceptance
    (is_valid depends only on ``violations``), so leaving it at 0 for a cache
    hit changes no accepted/rejected outcome for any layout.
    """
    sheet_owner_by_part_id: dict[str, int] = {}
    for sheet_index, placed in enumerate(placed_sheets):
        for part in placed:
            sheet_owner_by_part_id[part.part_id] = sheet_index

    violations_by_sheet: list[list[ValidationViolation]] = [[] for _ in placed_sheets]
    for cached in state.cached_collision_violations:
        owner_index = sheet_owner_by_part_id.get(cached["part_id_a"])
        if owner_index is None:
            # A cached violation referencing a part_id no longer present on any
            # current sheet means the layout changed since the cache was
            # written -- exactly what _collision_signature exists to detect.
            # Structurally unreachable when the signature check above already
            # matched, but this is the safe, explicit fallback rather than a
            # silent KeyError or a dropped violation.
            return None
        violations_by_sheet[owner_index].append(
            ValidationViolation(
                severity=cached["severity"],
                part_id_a=cached["part_id_a"],
                part_id_b=cached["part_id_b"],
                detail=cached["detail"],
                measured_distance_mm=cached["measured_distance_mm"],
            )
        )
    return [
        CollisionReport(violations=violations_by_sheet[index], checked_pairs_count=0)
        for index in range(len(placed_sheets))
    ]


def _validate_stored_sheets(state):
    placed_sheets = sheets_from_state(state)

    # cached_collision_signature/is_valid/violations/checked_pairs are written
    # in compute_layout after every successful compute (see that write site),
    # documented there and in job_storage.py's own JobState field comment as
    # existing specifically so GET /jobs/{id} -- which calls this function on
    # every poll while stage is "computed"/"confirmed" -- does not need to
    # re-run the full O(n log n) STRtree query plus exact GEOS predicates for
    # every candidate pair on every single poll. That comparison was
    # previously never actually performed here: the cache was written but
    # never read, so validate_layout() ran unconditionally regardless of
    # whether the layout had changed since the last compute.
    if state.cached_collision_signature is not None and state.cached_collision_signature == _collision_signature(state):
        cached_reports = _reports_from_cached_violations(state, placed_sheets)
        if cached_reports is not None:
            return placed_sheets, cached_reports

    reports = [
        validate_layout(
            placed,
            float(state.sheet_width_mm),
            float(state.sheet_height_mm),
            float(state.sheet_margin_mm),
            clearance_mm=float(state.clearance_mm),
        )
        for placed in placed_sheets
    ]
    return placed_sheets, reports


def _stored_compute_response(state):
    if any(v is None for v in (state.sheet_width_mm, state.sheet_height_mm, state.sheet_margin_mm, state.clearance_mm)):
        return None

    placed_sheets, reports = _validate_stored_sheets(state)
    sheet_previews = [
        _sheet_preview(page_number, placed, report)
        for page_number, (placed, report) in enumerate(zip(placed_sheets, reports, strict=True), start=1)
    ]
    first_sheet = placed_sheets[0] if placed_sheets else []
    collision_is_valid = bool(reports) and all(report.is_valid for report in reports)
    violations = [violation for sheet in sheet_previews for violation in sheet.violations]

    unplaced = list(state.unplaced_part_ids)
    return ComputeResponse(
        job_id=state.job_id,
        placed_parts=_placed_previews(first_sheet),
        sheets=sheet_previews,
        sheet_count=len(sheet_previews),
        unplaced_part_ids=unplaced,
        all_placed=len(unplaced) == 0,
        collision_report_valid=collision_is_valid,
        violations=violations,
        ready_to_confirm=bool(first_sheet) and collision_is_valid,
        sheet_full=state.sheet_full,
        processed_count=len(state.parts),
        total_count=len(state.parts),
        layout_message=state.layout_message or "النتيجة محفوظة ويمكن استكمال العمل بدون إعادة الحساب.",
    )


@app.post("/jobs", response_model=CreateJobResponse)
async def create_nesting_job() -> CreateJobResponse:
    state = create_job()
    return _job_status_payload(state)


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_nesting_job(job_id: str) -> JobStatusResponse:
    try:
        state = load_job_state(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Jobs saved by the former PNG/RGBA-only policy are upgraded when the
    # client reconnects, so users do not have to delete and upload RGB images
    # again after installing this release.
    if any(not part.is_valid and _is_legacy_alpha_rejection(part) for part in state.parts):
        lock = await _get_job_lock(job_id)
        async with lock:
            state = load_job_state(job_id)
            await run_in_threadpool(_upgrade_legacy_alpha_rejections, state)

    base = _job_status_payload(state)
    compute = _stored_compute_response(state) if state.stage in ("computed", "confirmed") else None
    return JobStatusResponse(
        **base.model_dump(),
        parts=[
            JobPartStatus(
                client_part_id=part.client_part_id,
                part_id=part.part_id,
                original_filename=part.original_filename,
                is_valid=part.is_valid,
                rejection_reason=part.rejection_reason,
                stored=Path(part.stored_image_path).exists(),
            )
            for part in state.parts
        ],
        sheet_width_mm=state.sheet_width_mm,
        sheet_height_mm=state.sheet_height_mm,
        sheet_margin_mm=state.sheet_margin_mm,
        clearance_mm=state.clearance_mm,
        dpi=state.dpi,
        packing_attempts=state.packing_attempts,
        placed_parts=compute.placed_parts if compute else [],
        sheets=compute.sheets if compute else [],
        sheet_count=compute.sheet_count if compute else 0,
        unplaced_part_ids=compute.unplaced_part_ids if compute else list(state.unplaced_part_ids),
        sheet_full=state.sheet_full,
        layout_message=state.layout_message,
        collision_report_valid=compute.collision_report_valid if compute else None,
        ready_to_confirm=compute.ready_to_confirm if compute else False,
    )


async def _read_and_hash_upload(uploaded_file: UploadFile, destination: Path) -> tuple[int, str]:
    total = 0
    digest = hashlib.sha256()
    with destination.open("wb") as output:
        while True:
            chunk = await uploaded_file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                output.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"حجم الصورة أكبر من الحد المسموح ({MAX_UPLOAD_BYTES} bytes).",
                )
            digest.update(chunk)
            output.write(chunk)
    return total, digest.hexdigest()


@app.post("/upload", response_model=UploadResponse)
async def upload_images(
    files: list[UploadFile] = File(...),
    job_id: str | None = Form(default=None),
    dpi: float = Form(default=ALLOWED_DPI, gt=0),
    client_part_ids_json: str | None = Form(default=None),
    original_source_paths_json: str | None = Form(default=None),
) -> UploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="لا توجد صور مرفوعة.")
    client_part_ids: list[str] = []
    if client_part_ids_json:
        try:
            decoded_ids = json.loads(client_part_ids_json)
            if not isinstance(decoded_ids, list) or not all(isinstance(item, str) for item in decoded_ids):
                raise ValueError("invalid list")
            client_part_ids = decoded_ids
        except Exception as exc:
            raise HTTPException(status_code=400, detail="client_part_ids_json غير صالح.") from exc
    else:
        client_part_ids = [uuid.uuid4().hex for _ in files]
    if len(client_part_ids) != len(files):
        raise HTTPException(status_code=400, detail="عدد client_part_ids يجب أن يطابق عدد الملفات.")
    source_paths: list[str | None] = [None] * len(files)
    if original_source_paths_json:
        try:
            decoded_paths = json.loads(original_source_paths_json)
            if (
                not isinstance(decoded_paths, list)
                or len(decoded_paths) != len(files)
                or not all(item is None or isinstance(item, str) for item in decoded_paths)
            ):
                raise ValueError("invalid source path list")
            source_paths = [
                item.strip() if isinstance(item, str) and item.strip() else None
                for item in decoded_paths
            ]
        except Exception as exc:
            raise HTTPException(status_code=400, detail="original_source_paths_json غير صالح.") from exc

    if job_id is None:
        state = create_job()
        job_id = state.job_id
    else:
        try:
            state = load_job_state(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    existing_dpi = state.source_dpi
    if existing_dpi is not None and abs(float(existing_dpi) - dpi) > 1e-9:
        raise HTTPException(
            status_code=400,
            detail=f"الـDPI لهذا الـjob هو {existing_dpi}. لا يمكن رفع صور جديدة بـDPI مختلف.",
        )
    if existing_dpi is None:
        state.source_dpi = dpi

    resolution = Resolution(dpi=dpi)
    results: list[UploadedPartResult | None] = [None] * len(files)
    existing_count = 0
    new_count = 0
    job_uploads = uploads_dir(state.job_id)
    job_uploads.mkdir(parents=True, exist_ok=True)

    lock = await _get_job_lock(state.job_id)
    async with lock:
        by_client_id = {part.client_part_id: part for part in state.parts}

        # Stage 1: sequential I/O (read + hash + write each upload to disk).
        # This is already fast streaming I/O and each file needs a distinct
        # temp/final path, so there is nothing to gain from parallelizing it.
        # For each file we resolve immediately whether it's an idempotent
        # retry (existing) or genuinely new work that still needs analysis.
        new_work: list[tuple[int, str, str, Path, str]] = []  # (index, client_part_id, part_id, saved_path, incoming_hash)
        for index, uploaded_file in enumerate(files):
            client_part_id = client_part_ids[index] or uuid.uuid4().hex
            # Stable client IDs make upload retries idempotent: a lost response can be retried safely.
            existing = by_client_id.get(client_part_id)

            suffix = Path(uploaded_file.filename or "image.png").suffix.lower() or ".png"
            temp_path = job_uploads / f".uploading_{uuid.uuid4().hex}{suffix}"
            _, incoming_hash = await _read_and_hash_upload(uploaded_file, temp_path)

            if existing is not None:
                temp_path.unlink(missing_ok=True)
                if existing.content_sha256 and existing.content_sha256 != incoming_hash:
                    raise HTTPException(
                        status_code=409,
                        detail=f"client_part_id مكرر بمحتوى مختلف: {client_part_id}.",
                    )
                existing_count += 1
                results[index] = UploadedPartResult(
                    part_id=existing.part_id,
                    client_part_id=existing.client_part_id,
                    original_filename=existing.original_filename,
                    is_valid=existing.is_valid,
                    rejection_reason=existing.rejection_reason,
                )
                continue

            part_id = uuid.uuid4().hex[:12]
            saved_path = job_uploads / f"{part_id}{suffix}"
            temp_path.replace(saved_path)
            new_work.append((index, client_part_id, part_id, saved_path, incoming_hash))

        # Stage 2: analyze independent images concurrently, with a strict
        # capacity cap.  PIL/OpenCV/GEOS work can all be CPU-heavy; starting a
        # worker for every file in a large batch is what previously starved
        # the desktop and made unrelated operations appear frozen.
        analysis_capacity = asyncio.Semaphore(MAX_PARALLEL_IMAGE_ANALYSES)

        async def _analyze_one(saved_path: Path):
            async with analysis_capacity:
                try:
                    return await run_in_threadpool(_analyze_saved_image, saved_path, resolution)
                except Exception as exc:
                    validation = type("V", (), {
                        "is_valid": False,
                        "rejection_reason": f"فشل تحليل الصورة: {exc}",
                    })()
                    return validation, None

        analyzed = await asyncio.gather(*(_analyze_one(item[3]) for item in new_work))

        # Stage 3: commit results in the ORIGINAL file order (not analysis
        # completion order), identical to the previous sequential behaviour.
        for (index, client_part_id, part_id, saved_path, incoming_hash), (validation, contour) in zip(
            new_work, analyzed, strict=True
        ):
            original_filename = files[index].filename or "unknown"
            stored_part = StoredPart(
                part_id=part_id,
                client_part_id=client_part_id,
                content_sha256=incoming_hash,
                original_filename=original_filename,
                stored_image_path=str(saved_path),
                is_valid=bool(validation.is_valid),
                original_source_path=source_paths[index],
                rejection_reason=validation.rejection_reason,
            )
            _store_analysis_result(stored_part, validation, contour)

            state.parts.append(stored_part)
            by_client_id[client_part_id] = stored_part
            # Durability boundary kept exactly as before: every analyzed image
            # is committed to disk before the response is returned. The write
            # is now O(1) (append_pending_part) instead of O(current total
            # parts) (save_job_state), which is what made a 180-image batch
            # get slower with every additional image. If the process dies
            # mid-batch, load_job_state() replays this log on the next read,
            # so items committed before the crash are still recovered exactly
            # like before.
            append_pending_part(state.job_id, stored_part)
            new_count += 1
            results[index] = UploadedPartResult(
                part_id=part_id,
                client_part_id=client_part_id,
                original_filename=stored_part.original_filename,
                is_valid=stored_part.is_valid,
                rejection_reason=stored_part.rejection_reason,
            )
            logger.debug(
                "upload commit=%d batch=%d job=%s valid=%s client_part_id=%s",
                len(state.parts), len(files), state.job_id, stored_part.is_valid, client_part_id,
            )

        # One full-state write at the end of the batch (was previously N
        # writes, one per image). This also folds the pending log into
        # job_state.json and clears it (see save_job_state).
        save_job_state(state)

    final_results: list[UploadedPartResult] = [r for r in results if r is not None]

    total_count = len(state.parts)
    upload_complete = total_count > 0 and all(part.is_valid for part in state.parts)
    if upload_complete:
        resume_message = f"تم استلام وتحليل كل الصور: {total_count} صورة."
    else:
        resume_message = f"تم حفظ {total_count} صورة. يمكنك الاستكمال بأمان من الصورة التالية عند إعادة التشغيل."

    logger.info(
        "upload batch complete job=%s batch=%d new=%d existing=%d total=%d",
        state.job_id,
        len(files),
        new_count,
        existing_count,
        total_count,
    )

    return UploadResponse(
        job_id=state.job_id,
        parts=final_results,
        all_valid=all(result.is_valid for result in final_results),
        dpi=dpi,
        existing_count=existing_count,
        new_count=new_count,
        received_count=total_count,
        total_count=total_count,
        upload_complete=upload_complete,
        resume_message=resume_message,
    )


@app.delete("/jobs/{job_id}/parts/{client_part_id}")
async def delete_job_part(job_id: str, client_part_id: str) -> dict[str, object]:
    try:
        state = load_job_state(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    lock = await _get_job_lock(job_id)
    async with lock:
        target = next((part for part in state.parts if part.client_part_id == client_part_id), None)
        if target is None:
            return {"deleted": False, "job_id": job_id, "remaining_count": len(state.parts)}

        Path(target.stored_image_path).unlink(missing_ok=True)
        state.parts = [part for part in state.parts if part.client_part_id != client_part_id]
        # Any structural change invalidates a computed/exported layout.
        state.stage = "uploaded"
        state.placed_parts = []
        state.sheets = []
        state.unplaced_part_ids = []
        state.layout_message = None
        state.sheet_full = False
        state.output_tiff_path = None
        state.output_export_accepted = None
        state.output_width_px = None
        state.output_height_px = None
        state.output_dpi = None
        state.output_layer_count = 0
        state.background_color = None
        state.processed_images_path = None
        state.processed_images_directory = None
        state.moved_processed_images_count = 0
        state.qa_violations = []
        # Found during full-project review: this reset previously missed the
        # four cached_collision_* fields that _upgrade_legacy_alpha_rejections
        # (a few dozen lines above) already correctly clears on the same kind
        # of structural change. Not exploitable today -- _validate_stored_sheets
        # (the only reader of these fields) is only ever invoked by
        # _stored_compute_response, which is only ever called when
        # state.stage is "computed"/"confirmed" (see get_nesting_job's own
        # guard), and this deletion always resets stage to "uploaded" first --
        # so the stale cache is unreachable dead data until the next
        # compute_layout() call unconditionally overwrites it anyway. Clearing
        # it here regardless keeps every structural-change reset site
        # consistent and removes the latent trap for any future code path
        # that might call _validate_stored_sheets without first checking
        # state.stage, matching this project's existing defense-in-depth style
        # (e.g. _reports_from_cached_violations' own "should be structurally
        # unreachable... but checked rather than assumed" fallback).
        state.cached_collision_signature = None
        state.cached_collision_is_valid = None
        state.cached_collision_violations = []
        state.cached_collision_checked_pairs = 0
        save_job_state(state)

    return {"deleted": True, "job_id": job_id, "remaining_count": len(state.parts)}


@app.delete("/jobs/{job_id}")
async def delete_nesting_job(job_id: str) -> dict[str, object]:
    try:
        state = load_job_state(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    lock = await _get_job_lock(job_id)
    async with lock:
        _cancelled_jobs.add(job_id)
        job_dir = DEFAULT_JOBS_ROOT / job_id
        # validate through load_job_state above; this path is therefore safe.
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)
        _finish_progress(job_id, "تم حذف عملية الترتيب.")
    # _job_lock_guard (not the per-job `lock` just released above) is the
    # correct mutex here: it is the SAME guard _get_job_lock() itself uses to
    # create/look up entries in _job_locks. Popping the dict entry without it
    # was a bare, unguarded mutation racing directly against any concurrent
    # _get_job_lock(job_id) call for a job recreated under the same id right
    # after deletion — one coroutine could observe a half-updated dict while
    # another was mid-`setdefault`. Holding _job_lock_guard around the pop
    # makes this dict mutation atomic with every other dict mutation
    # _get_job_lock() performs, closing that window.
    async with _job_lock_guard:
        _job_locks.pop(job_id, None)
    return {"deleted": True, "job_id": job_id, "previous_stage": state.stage}


@app.post("/layout/compute/{job_id}", response_model=ComputeResponse)
async def compute_layout(job_id: str, req: ComputeRequest) -> ComputeResponse:
    try:
        state = load_job_state(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    source_dpi = getattr(state, "source_dpi", None)
    if source_dpi is None:
        source_dpi = req.dpi
    if abs(float(source_dpi) - req.dpi) > 1e-9:
        raise HTTPException(
            status_code=400,
            detail=(
                f"DPI mismatch: الصور تم تحليلها عند {source_dpi} DPI، بينما طلب الحساب {req.dpi} DPI. "
                "ارفع الصور بنفس DPI المطلوب."
            ),
        )
    # part_inputs was previously computed here, BEFORE the per-job lock below
    # is acquired, from this same pre-lock `state` snapshot -- and never
    # refreshed afterward. /upload and /delete each take that same lock to
    # mutate state.parts, but only for the duration of their own write; they
    # do not block a compute_layout call that already read `state` before
    # they ran. A part uploaded or deleted in the window between this read
    # and the lock acquisition below was silently invisible to the entire
    # nesting run that followed, while the job's `state` on disk (which
    # /upload or /delete had already committed) reflected the newer part
    # set -- the saved result at the end of this function would then not
    # match what was actually uploaded. The DPI check and cached-response
    # fast path immediately above are read-only and safe to keep unlocked
    # (a cache hit here never mutates anything and returning early is
    # correct even against a slightly stale snapshot -- worst case is one
    # extra full recompute), but the actual computation below needs the
    # freshest possible part set once it is committed to running.

    lock = await _get_job_lock(job_id)
    async with lock:
        # Re-read state now, from inside the lock, so any /upload or /delete
        # that completed (and released the same lock) between the pre-lock
        # checks above and this line is fully reflected before part_inputs is
        # derived from it. This mirrors the same defensive re-read pattern
        # every other locked mutation in this file already performs by
        # working from `state` captured at, or after, lock acquisition.
        state = load_job_state(job_id)
        part_inputs = part_inputs_from_state(state)
        if not part_inputs:
            raise HTTPException(status_code=400, detail="لا يوجد أي صور مقبولة لحساب الترتيب.")

        _cancelled_jobs.discard(job_id)
        _set_progress(job_id, (0, len(part_inputs), 0, "في انتظار موارد الحساب المتاحة..."))

        def cancelled() -> bool:
            return job_id in _cancelled_jobs

        last_progress_emit = 0.0

        def attempt_progress(
            done: int,
            part_total: int,
            placed: int,
            attempt_index: int,
            attempt_total: int,
        ) -> None:
            nonlocal last_progress_emit
            now = time.monotonic()
            # When small images finish nearly at once, publish only the latest
            # state at a modest rate.  SSE still carries every meaningful
            # change, without queuing hundreds of UI rebuilds or events.  This
            # now fires from *inside* every packing attempt (not just once per
            # completed attempt), so the reported part number keeps moving
            # throughout the whole run instead of appearing to stall after the
            # first attempt finishes.
            overall_done = (attempt_index - 1) * part_total + done
            overall_total = attempt_total * part_total
            if (
                done == part_total
                or done == 1
                or now - last_progress_emit >= _PROGRESS_EMIT_MIN_INTERVAL_SECONDS
            ):
                last_progress_emit = now
                _set_progress_from_worker(
                    job_id,
                    (
                        overall_done,
                        overall_total,
                        placed,
                        (
                            f"جاري ترتيب {done} من {part_total} صورة "
                            f"(محاولة توزيع {attempt_index} من {attempt_total})..."
                        ),
                    ),
                )

        started = time.perf_counter()
        try:
            # A layout can be computationally intense even after the NFP
            # optimisations.  Queue competing layouts instead of letting
            # their GEOS workers fight for every CPU core and freeze the host.
            async with _nesting_capacity:
                _set_progress(job_id, (
                    0,
                    len(part_inputs),
                    0,
                    "جاري بدء ترتيب الصور...",
                ))
                result = await run_in_threadpool(
                    run_best_single_sheet_nesting,
                    parts_mm=part_inputs,
                    sheet_width_mm=req.sheet_width_mm,
                    sheet_height_mm=req.sheet_height_mm,
                    sheet_margin_mm=req.sheet_margin_mm,
                    clearance_mm=req.clearance_mm,
                    packing_attempts=req.packing_attempts,
                    check_cancelled=cancelled,
                    on_attempt_progress=attempt_progress,
                    # الـ backfill sweep دايماً بيستخدم مسار الـ exact NFP بغض النظر
                    # عن مسار الـ main pass، فأول قطعة فيه ممكن تدفع ثمن unary_union
                    # كامل على مئات الـ zones المشغولة، لـ 24 زاوية دوران محتملة —
                    # بدون سقف زمني، ده كان بيظهر كأن العملية توقفت تماماً. 45 ثانية
                    # كافية عملياً لمحاولة حقيقية، وبنفس رتبة lns_time_budget بالأسفل.
                    backfill_time_budget_seconds=60.0,
                )

                # ---- LNS Optimization Stage ----
                # عدد الـ iterations والـ time budget يتناسبوا مع عدد الصور.
                # مع 150+ صورة، iteration واحدة بتاخد دقائق بسبب NFP.
                # لذلك نستخدم عدد أقل من الـ iterations وtime budget أقصر.
                placed_count = len(result.placed)
                if placed_count >= 100:
                    lns_max_iterations = 10
                    lns_time_budget = 60.0
                    lns_destroy_fraction = 0.10
                elif placed_count >= 50:
                    lns_max_iterations = 10
                    lns_time_budget = 60.0
                    lns_destroy_fraction = 0.15
                else:
                    lns_max_iterations = 10
                    lns_time_budget = 60.0
                    lns_destroy_fraction = 0.20

                if result.placed:
                    def _lns_iteration_progress(entry) -> None:
                        """Report each LNS iteration to the UI."""
                        status = "✓ تحسّن" if entry.is_new_best else ("↔ مقبول" if entry.accepted else "✗ مرفوض")
                        msg = (
                            f"جاري تحسين الترتيب — تكرار {entry.iteration} من {lns_max_iterations} "
                            f"({status}، {entry.placed_count} صورة)"
                        )
                        logger.info("LNS iteration=%d/%d %s placed=%d", entry.iteration, lns_max_iterations, status, entry.placed_count)
                        _set_progress_from_worker(
                            job_id,
                            (entry.iteration, lns_max_iterations, entry.placed_count, msg),
                        )

                    logger.info(
                        "LNS starting job=%s placed=%d iterations=%d budget=%.0fs destroy=%.0f%%",
                        job_id, placed_count, lns_max_iterations, lns_time_budget, lns_destroy_fraction * 100,
                    )
                    _set_progress(
                        job_id,
                        (0, lns_max_iterations, placed_count,
                         f"جاري بدء تحسين الترتيب ({placed_count} صورة، حد أقصى {int(lns_time_budget)} ثانية)..."),
                    )
                    lns_result = await run_in_threadpool(
                        run_lns_optimization,
                        starting_result=result,
                        parts_mm=part_inputs,
                        sheet_width_mm=req.sheet_width_mm,
                        sheet_height_mm=req.sheet_height_mm,
                        sheet_margin_mm=req.sheet_margin_mm,
                        clearance_mm=req.clearance_mm,
                        placement_policy="bottom_left",
                        max_iterations=lns_max_iterations,
                        destroy_fraction=lns_destroy_fraction,
                        initial_temperature=50.0,
                        cooling_rate=0.92,
                        seed=42,
                        time_budget_seconds=lns_time_budget,
                        check_cancelled=cancelled,
                        on_iteration=_lns_iteration_progress,
                    )
                    if lns_result.improved:
                        result = lns_result.best
                        logger.info(
                            "LNS improved layout job=%s score=%.2f->%.2f placed=%d iterations=%d",
                            job_id, lns_result.starting_score.total, lns_result.best_score.total,
                            len(result.placed), lns_result.iterations_run,
                        )
                    else:
                        logger.info(
                            "LNS did not improve layout job=%s iterations=%d",
                            job_id, lns_result.iterations_run,
                        )

                # ---- Compaction Stage ----
                if result.placed:
                    logger.info("Compaction starting job=%s placed=%d", job_id, len(result.placed))
                    _set_progress(
                        job_id,
                        (len(result.placed), result.total_count, len(result.placed),
                         f"جاري ضغط الترتيب وتقليل الفراغات ({len(result.placed)} صورة)..."),
                    )
                    compaction_result = await run_in_threadpool(
                        compact_layout,
                        starting_result=result,
                        sheet_width_mm=req.sheet_width_mm,
                        sheet_height_mm=req.sheet_height_mm,
                        sheet_margin_mm=req.sheet_margin_mm,
                        clearance_mm=req.clearance_mm,
                        check_cancelled=cancelled,
                    )
                    if compaction_result.improved:
                        result = compaction_result.result
                        logger.info("Compaction improved job=%s moved=%d", job_id, compaction_result.moved_count)
                    else:
                        logger.info("Compaction did not improve job=%s", job_id)

                # ---- Local Re-optimization Stage ----
                # A final targeted pass over whatever single free-space pocket LNS
                # and compaction's whole-sheet search still leave as worst, after
                # both have already run. Scaled the same way as the LNS stage above
                # (larger jobs -> shorter budget, fewer rounds), since this stage's
                # own repair cost is the same per-part NFP cost LNS pays.
                if result.placed:
                    if placed_count >= 100:
                        local_reopt_max_rounds = 3
                        local_reopt_time_budget = 30.0
                    elif placed_count >= 50:
                        local_reopt_max_rounds = 4
                        local_reopt_time_budget = 30.0
                    else:
                        local_reopt_max_rounds = 5
                        local_reopt_time_budget = 30.0

                    def _local_reopt_round_progress(entry) -> None:
                        """Report each local re-optimization round to the UI."""
                        status = "✓ تحسّن" if entry.accepted else "✗ لم يتحسّن"
                        msg = (
                            f"جاري إعادة تحسين أكبر فراغ متبقي — جولة {entry.round_index} "
                            f"({status}، {entry.isolated_part_count} قطعة معزولة)"
                        )
                        logger.info(
                            "local reopt round=%d %s isolated=%d",
                            entry.round_index, status, entry.isolated_part_count,
                        )
                        _set_progress_from_worker(
                            job_id,
                            (len(result.placed), result.total_count, len(result.placed), msg),
                        )

                    logger.info(
                        "Local reoptimization starting job=%s placed=%d max_rounds=%d budget=%.0fs",
                        job_id, len(result.placed), local_reopt_max_rounds, local_reopt_time_budget,
                    )
                    _set_progress(
                        job_id,
                        (len(result.placed), result.total_count, len(result.placed),
                         f"جاري إعادة تحسين أكبر فراغ متبقي ({len(result.placed)} صورة)..."),
                    )
                    local_reopt_result = await run_in_threadpool(
                        run_local_reoptimization,
                        starting_result=result,
                        parts_mm=part_inputs,
                        sheet_width_mm=req.sheet_width_mm,
                        sheet_height_mm=req.sheet_height_mm,
                        sheet_margin_mm=req.sheet_margin_mm,
                        clearance_mm=req.clearance_mm,
                        placement_policy="bottom_left",
                        max_rounds=local_reopt_max_rounds,
                        seed=42,
                        time_budget_seconds=local_reopt_time_budget,
                        check_cancelled=cancelled,
                        on_round=_local_reopt_round_progress,
                    )
                    if local_reopt_result.improved:
                        result = local_reopt_result.best
                        logger.info(
                            "Local reoptimization improved layout job=%s score=%.2f->%.2f placed=%d rounds=%d",
                            job_id, local_reopt_result.starting_score.total, local_reopt_result.best_score.total,
                            len(result.placed), local_reopt_result.rounds_run,
                        )
                    else:
                        logger.info(
                            "Local reoptimization did not improve job=%s rounds=%d",
                            job_id, local_reopt_result.rounds_run,
                        )

        except NestingCancelledError as exc:
            _cancelled_jobs.discard(job_id)
            _finish_progress(job_id, "تم إلغاء الحساب.")
            raise HTTPException(status_code=499, detail=str(exc)) from exc
        except Exception as exc:
            _cancelled_jobs.discard(job_id)
            _finish_progress(job_id, "توقف الحساب بسبب خطأ.")
            logger.exception("nesting failed job=%s", job_id)
            raise HTTPException(status_code=500, detail=f"فشل محرك الـnesting: {exc}") from exc

        _cancelled_jobs.discard(job_id)
        _set_progress(
            job_id,
            (
                len(result.placed),
                result.total_count,
                len(result.placed),
                "جاري التحقق الهندسي وحفظ النتيجة...",
            ),
        )
        logger.info(
            "compute done job=%s sheets=%d placed=%d unplaced=%d elapsed_ms=%.1f",
            job_id,
            1 if result.placed else 0,
            len(result.placed),
            len(result.unplaced_part_ids),
            (time.perf_counter() - started) * 1000,
        )

        placed_sheets = [result.placed] if result.placed else []
        collision_reports = [
            validate_layout(
                placed,
                req.sheet_width_mm,
                req.sheet_height_mm,
                req.sheet_margin_mm,
                clearance_mm=req.clearance_mm,
            )
            for placed in placed_sheets
        ]
        collision_is_valid = bool(collision_reports) and all(
            report.is_valid for report in collision_reports
        )
        message = _upload_message(
            len(part_inputs),
            len(result.placed),
            len(result.unplaced_part_ids),
        )

        state.stage = "computed"
        state.sheet_width_mm = req.sheet_width_mm
        state.sheet_height_mm = req.sheet_height_mm
        state.sheet_margin_mm = req.sheet_margin_mm
        state.clearance_mm = req.clearance_mm
        state.dpi = req.dpi
        state.packing_attempts = req.packing_attempts
        state.sheets = sheets_to_state(placed_sheets)
        state.multi_sheet_layout = True
        # Keep the sole page in the legacy field so older desktop clients can
        # still preview/export it after upgrading the backend.
        state.placed_parts = placed_parts_to_state(placed_sheets[0]) if placed_sheets else []
        state.unplaced_part_ids = result.unplaced_part_ids
        state.sheet_full = bool(result.unplaced_part_ids)
        state.layout_message = message
        # A recomputed geometry invalidates every export setting/result. The
        # prior TIFF remains on disk until job cleanup, but is no longer
        # reachable as this job's current output.
        state.output_tiff_path = None
        state.output_export_accepted = None
        state.output_width_px = None
        state.output_height_px = None
        state.output_dpi = None
        state.output_layer_count = 0
        state.background_color = None
        state.processed_images_path = None
        state.processed_images_directory = None
        state.moved_processed_images_count = 0
        state.qa_violations = []
        state.cached_collision_signature = _collision_signature(state)
        state.cached_collision_is_valid = collision_is_valid
        state.cached_collision_checked_pairs = sum(
            report.checked_pairs_count for report in collision_reports
        )
        state.cached_collision_violations = [
            {
                "severity": v.severity,
                "part_id_a": v.part_id_a,
                "part_id_b": v.part_id_b,
                "detail": v.detail,
                "measured_distance_mm": v.measured_distance_mm,
            }
            for report in collision_reports
            for v in report.violations
        ]
        save_job_state(state)
        _finish_progress(job_id, message)

    sheet_previews = [
        _sheet_preview(page_number, placed, report)
        for page_number, (placed, report) in enumerate(
            zip(placed_sheets, collision_reports, strict=True), start=1
        )
    ]
    first_sheet = sheet_previews[0] if sheet_previews else None
    violations = [violation for sheet in sheet_previews for violation in sheet.violations]

    return ComputeResponse(
        job_id=job_id,
        placed_parts=first_sheet.placed_parts if first_sheet else [],
        sheets=sheet_previews,
        sheet_count=len(sheet_previews),
        unplaced_part_ids=result.unplaced_part_ids,
        all_placed=result.all_placed,
        collision_report_valid=collision_is_valid,
        violations=violations,
        ready_to_confirm=bool(placed_sheets) and collision_is_valid,
        sheet_full=bool(result.unplaced_part_ids),
        processed_count=len(result.placed),
        total_count=result.total_count,
        layout_message=message,
    )


@app.post("/layout/cancel/{job_id}")
async def cancel_layout(job_id: str) -> dict[str, str]:
    # Every other job-scoped endpoint validates the job exists via
    # load_job_state before acting; this one silently accepted a cancel for a
    # completely unknown job_id. Deliberately NOT taking the per-job lock
    # here: compute_layout holds that lock for its entire (potentially
    # multi-minute) run, so cancel_layout must stay lock-free to actually be
    # able to interrupt a compute in progress — acquiring the same lock would
    # make every cancel request queue behind the very computation it is
    # trying to stop, defeating cancellation entirely. set.add() on
    # _cancelled_jobs is a single atomic operation under the GIL and
    # compute_layout's own cancelled() check already treats this set as a
    # best-effort, eventually-consistent signal (polled, not locked), so no
    # lock is needed for the mutation itself — only the existence check was
    # actually missing.
    try:
        load_job_state(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _cancelled_jobs.add(job_id)
    done, total, placed, _message = _progress_jobs.get(job_id, (0, 0, 0, None))
    _set_progress(job_id, (done, total, placed, "جاري إيقاف الحساب..."))
    return {"status": "cancelled"}


@app.get("/layout/progress/{job_id}", response_model=ProgressResponse)
async def get_layout_progress(job_id: str) -> ProgressResponse:
    progress = _progress_jobs.get(job_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="لا توجد عملية ترتيب جارية لهذا الـjob.")
    done, total, placed, message = progress
    return ProgressResponse(job_id=job_id, done=done, total=total, placed=placed, message=message)


@app.get("/layout/progress/stream/{job_id}")
async def stream_layout_progress(job_id: str) -> StreamingResponse:
    """Push layout progress on one SSE connection instead of client polling."""
    try:
        load_job_state(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def events() -> AsyncIterator[str]:
        seen_version = -1
        while True:
            # Register before reading the snapshot so no update can be missed
            # between the read and the wait.
            wakeup = asyncio.Event()
            _progress_waiters.setdefault(job_id, set()).add(wakeup)
            try:
                version = _progress_versions.get(job_id, 0)
                progress = _progress_jobs.get(job_id)
                finished = job_id in _finished_progress_jobs

                if progress is not None and version != seen_version:
                    done, total, placed, message = progress
                    payload = json.dumps(
                        {
                            "job_id": job_id,
                            "done": done,
                            "total": total,
                            "placed": placed,
                            "message": message,
                            "complete": finished,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    seen_version = version
                    yield f"event: progress\ndata: {payload}\n\n"
                    if finished:
                        return
                    continue

                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=_PROGRESS_HEARTBEAT_SECONDS)
                except TimeoutError:
                    # A comment keeps proxies from timing out the connection;
                    # it is not a state update and Flutter ignores it.
                    yield ": keep-alive\n\n"
            finally:
                waiters = _progress_waiters.get(job_id)
                if waiters is not None:
                    waiters.discard(wakeup)
                    if not waiters:
                        _progress_waiters.pop(job_id, None)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/layout/confirm/{job_id}", response_model=ConfirmResponse)
async def confirm_and_export(job_id: str, req: ConfirmRequest) -> ConfirmResponse:
    try:
        state = load_job_state(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if state.stage == "confirmed" and state.output_tiff_path and Path(state.output_tiff_path).exists():
        return ConfirmResponse(
            job_id=job_id,
            output_tiff_path=state.output_tiff_path,
            export_accepted=bool(state.output_export_accepted),
            qa_violations=[QaViolationResponse(**item) for item in state.qa_violations],
            width_px=int(state.output_width_px or 0),
            height_px=int(state.output_height_px or 0),
            dpi=float(state.output_dpi or state.dpi or 0),
            page_count=len(sheets_from_state(state)) or 1,
            layer_count=int(state.output_layer_count or 0),
            processed_images_directory=state.processed_images_directory,
            moved_processed_images_count=int(state.moved_processed_images_count or 0),
        )

    # Every mutating step below -- collision re-check, TIFF export, QA check,
    # moving processed originals, and the final save_job_state -- previously
    # ran with NO lock at all, unlike every other endpoint that mutates this
    # same job's state file. confirmAndExport's own frontend retry path
    # (nestingJobStore.ts) resends this exact request when a response is lost
    # on a long-running call, which is precisely the case a lost response on
    # a real multi-minute export+QA call produces: two concurrent
    # confirm_and_export calls for the SAME job_id, both re-validating the
    # same placed_sheets, both writing to the same output_tiff_path(job_id)
    # file via export_multi_sheet_tiff, and both eventually calling
    # save_job_state with independently-computed results -- whichever finishes
    # last silently wins, and the TIFF on disk can end up not matching either
    # call's own QA report. Wrapping the whole body in the same per-job lock
    # compute_layout already holds for its own full duration serializes any
    # overlapping confirm attempts exactly the way compute attempts already
    # are, without changing behavior for the single-caller case at all.
    lock = await _get_job_lock(job_id)
    async with lock:
        # Re-read state now that the lock is held, mirroring compute_layout's
        # identical re-read: a second confirm call queued behind the first
        # must see whatever the first call already committed (e.g. it may now
        # already be "confirmed"), not the snapshot read before either call
        # took the lock.
        state = load_job_state(job_id)
        if state.stage == "confirmed" and state.output_tiff_path and Path(state.output_tiff_path).exists():
            return ConfirmResponse(
                job_id=job_id,
                output_tiff_path=state.output_tiff_path,
                export_accepted=bool(state.output_export_accepted),
                qa_violations=[QaViolationResponse(**item) for item in state.qa_violations],
                width_px=int(state.output_width_px or 0),
                height_px=int(state.output_height_px or 0),
                dpi=float(state.output_dpi or state.dpi or 0),
                page_count=len(sheets_from_state(state)) or 1,
                layer_count=int(state.output_layer_count or 0),
                processed_images_directory=state.processed_images_directory,
                moved_processed_images_count=int(state.moved_processed_images_count or 0),
            )
        if state.stage != "computed":
            raise HTTPException(status_code=400, detail="لازم تعمل compute قبل confirm.")
        placed_sheets = sheets_from_state(state)
        if not placed_sheets or not any(placed_sheets):
            raise HTTPException(status_code=400, detail="لا توجد قطع مرتبة للتصدير.")

        values = (state.sheet_width_mm, state.sheet_height_mm, state.sheet_margin_mm, state.clearance_mm, state.dpi)
        if any(value is None for value in values):
            raise HTTPException(status_code=500, detail="حالة الـjob ناقصة.")
        sheet_w, sheet_h, sheet_m, clearance, dpi = values
        resolution = Resolution(dpi=float(dpi))
        background_rgba = _background_rgba(req.background_color)

        # Each page is geometrically independent and must pass the same exact
        # collision/clearance validation before a TIFF frame is ever generated.
        collision_reports = [
            validate_layout(
                placed_parts,
                float(sheet_w),
                float(sheet_h),
                float(sheet_m),
                clearance_mm=float(clearance),
            )
            for placed_parts in placed_sheets
        ]
        if not all(report.is_valid for report in collision_reports):
            raise HTTPException(status_code=409, detail="الـlayout تغيّر أو غير صالح: توجد مخالفات هندسية قبل التصدير.")

        # Progress scale for the whole confirm/export call: one unit per sheet
        # written, then 4 more units for the sequential QA checks that follow.
        # export_multi_sheet_tiff and run_qa_check each report on their own local
        # scale through the callbacks below; this offsets both onto one
        # consistent done/total the SSE stream (already used by /layout/compute)
        # can display as a single coherent percentage.
        sheet_total = len(placed_sheets)
        qa_stage_total = 4
        overall_total = sheet_total + qa_stage_total
        _set_progress(
            job_id,
            (0, overall_total, len(placed_sheets[0]) if placed_sheets else 0, "جاري بدء التصدير..."),
        )

        def _export_sheet_progress(sheet_done: int, _sheet_total: int, message: str) -> None:
            _set_progress_from_worker(
                job_id,
                (sheet_done, overall_total, len(placed_sheets[0]) if placed_sheets else 0, message),
            )

        def _export_qa_progress(check_done: int, _check_total: int, message: str) -> None:
            _set_progress_from_worker(
                job_id,
                (sheet_total + check_done, overall_total, len(placed_sheets[0]) if placed_sheets else 0, message),
            )

        try:
            tiff_result = await run_in_threadpool(
                export_multi_sheet_tiff,
                placed_sheets,
                float(sheet_w),
                float(sheet_h),
                resolution,
                output_tiff_path(state.job_id),
                mode=req.mode,
                background_rgba=background_rgba,
                on_sheet_progress=_export_sheet_progress,
            )
            qa_report = await run_in_threadpool(
                run_qa_check,
                tiff_result.file_path,
                placed_sheets,
                float(sheet_w),
                float(sheet_h),
                float(sheet_m),
                resolution,
                clearance_mm=float(clearance),
                on_check_progress=_export_qa_progress,
            )
        except Exception as exc:
            logger.exception("confirm/export failed job=%s", job_id)
            _finish_progress(job_id, "توقف التصدير بسبب خطأ.")
            raise HTTPException(status_code=500, detail=f"فشل التصدير أو الـQA: {exc}") from exc

        processed_result = None
        if qa_report.is_valid:
            placed_part_ids = {
                part.part_id for placed_parts in placed_sheets for part in placed_parts
            }
            try:
                processed_result = await run_in_threadpool(
                    move_processed_originals,
                    state.parts,
                    placed_part_ids,
                    req.processed_images_path,
                    folder_name=req.folder_name,
                )
            except ProcessedImagesError as exc:
                logger.exception("processed originals move failed job=%s", job_id)
                _finish_progress(job_id, "تم إنشاء ملف TIFF لكن توقف نقل الصور الأصلية.")
                raise HTTPException(
                    status_code=409,
                    detail=f"تم إنشاء TIFF لكن لم يتم نقل الصور الأصلية: {exc}",
                ) from exc

        _finish_progress(job_id, "اكتمل التصدير والتحقق النهائي.")
        state.stage = "confirmed"
        state.output_tiff_path = tiff_result.file_path
        state.output_export_accepted = qa_report.is_valid
        state.output_width_px = tiff_result.width_px
        state.output_height_px = tiff_result.height_px
        state.output_dpi = tiff_result.dpi_x
        state.output_layer_count = tiff_result.layer_count
        state.background_color = req.background_color
        state.processed_images_path = req.processed_images_path
        state.processed_images_directory = (
            processed_result.directory if processed_result is not None else None
        )
        state.moved_processed_images_count = (
            processed_result.moved_count if processed_result is not None else 0
        )
        state.qa_violations = [
            {"severity": v.severity, "detail": v.detail, "expected": v.expected, "actual": v.actual}
            for v in qa_report.violations
        ]
        save_job_state(state)
        return ConfirmResponse(
            job_id=job_id,
            output_tiff_path=tiff_result.file_path,
            export_accepted=qa_report.is_valid,
            qa_violations=[
                QaViolationResponse(
                    severity=v.severity,
                    detail=v.detail,
                    expected=v.expected,
                    actual=v.actual,
                )
                for v in qa_report.violations
            ],
            width_px=tiff_result.width_px,
            height_px=tiff_result.height_px,
            dpi=tiff_result.dpi_x,
            page_count=tiff_result.page_count,
            layer_count=tiff_result.layer_count,
            processed_images_directory=state.processed_images_directory,
            moved_processed_images_count=state.moved_processed_images_count,
        )

@app.get("/download/{job_id}")
async def download_tiff(job_id: str) -> FileResponse:
    try:
        state = load_job_state(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not state.output_tiff_path:
        raise HTTPException(status_code=400, detail="لا يوجد TIFF مُصدَّر لهذا الـjob.")
    path = Path(state.output_tiff_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="ملف TIFF غير موجود على القرص.")
    return FileResponse(path=path, media_type="image/tiff", filename=f"sheet_layout_{job_id[:8]}.tiff")


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
