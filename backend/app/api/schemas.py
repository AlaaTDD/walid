"""Public API models."""
from __future__ import annotations

from pydantic import BaseModel, Field


class UploadedPartResult(BaseModel):
    part_id: str
    client_part_id: str
    original_filename: str
    is_valid: bool
    rejection_reason: str | None = None


class UploadResponse(BaseModel):
    job_id: str
    parts: list[UploadedPartResult]
    all_valid: bool
    dpi: float
    existing_count: int = 0
    new_count: int = 0
    received_count: int = 0
    total_count: int = 0
    upload_complete: bool = False
    resume_message: str | None = None


class CreateJobResponse(BaseModel):
    job_id: str
    stage: str
    created_at: str
    updated_at: str
    source_dpi: float | None = None
    received_count: int = 0
    valid_count: int = 0
    rejected_count: int = 0
    total_count: int = 0
    upload_complete: bool = False
    output_available: bool = False


class JobPartStatus(BaseModel):
    client_part_id: str
    part_id: str
    original_filename: str
    is_valid: bool
    rejection_reason: str | None = None
    stored: bool = True


class ComputeRequest(BaseModel):
    sheet_width_mm: float = Field(default=790.0, gt=0)
    sheet_height_mm: float = Field(default=1190.0, gt=0)
    sheet_margin_mm: float = Field(default=5.0, ge=0)
    clearance_mm: float = Field(default=4.10, gt=0)
    dpi: float = Field(default=300.0, gt=0)
    # محاولة واحدة فقط: الـ LNS optimizer يتولى التحسين العالمي بدلاً من
    # تكرار محاولات greedy متعددة. راجع _PACKING_STRATEGIES في engine.py.
    packing_attempts: int = Field(default=1, ge=1, le=1)
    # اختياريان: تعديل لكل طلب على قيم الـ LARGE tier في main.py's
    # _lns_pipeline_settings (>=100 قطعة موضوعة)، بدل الاعتماد فقط على
    # متغيرات البيئة NESTING_LNS_MAX_ITERATIONS_LARGE /
    # NESTING_LNS_DESTROY_FRACTION_LARGE اللي بتتطلب إعادة تشغيل السيرفر.
    # None (الافتراضي) يعني "استخدم القيمة المحسوبة من الـ tier زي ما هي" --
    # فأي job من عميل قديم ما بيبعتش الحقلين دول سلوكه هيفضل زي قبل التغيير
    # ده تمامًا. الحدود العليا هنا مش اعتباطية:
    # - le=60 لـ iterations: نفس الـ default الموثق في lns.py's
    #   run_lns_optimization (max_iterations: int = 60) ونفس السقف اللي
    #   test_nesting_capacity.py بيشغّله فعليًا (max_iterations=60)، يعني ده
    #   سقف مُختبر ومُثبت إنه آمن، مش رقم متخيَّل.
    # - le=0.40 لـ destroy_fraction: أعلى قيمة موثقة ومُختبرة فعليًا في هذا
    #   الكود بالذات (test_lns.py: max_iterations=80, destroy_fraction=0.4).
    #   فوق الحد ده الخوارزمية بتبقى بتهدم جزء كبير جدًا من الترتيب في كل
    #   iteration، فبتقرب من إعادة بناء عشوائي بدل تحسين مستهدف -- ده "أكتر
    #   من كده هيبقى مبالغ فيه" اللي المستخدم قصده بالظبط.
    lns_max_iterations_large: int | None = Field(default=None, ge=1, le=60)
    lns_destroy_fraction_large: float | None = Field(default=None, gt=0, le=0.40)


class ContourPointPreview(BaseModel):
    x_mm: float
    y_mm: float


class PlacedPartPreview(BaseModel):
    part_id: str
    rotation_deg: int
    bounds_min_x_mm: float
    bounds_min_y_mm: float
    bounds_max_x_mm: float
    bounds_max_y_mm: float
    centroid_x_mm: float
    centroid_y_mm: float
    # Exact placed contour (rotated + translated), exterior ring only, in the
    # same sheet mm coordinate space as bounds_*/centroid_* above. Populated
    # from part.placed_shape_mm.exterior.coords in main.py's _placed_previews.
    # Optional/defaulted for backward compatibility with any client that
    # still expects the old bounds-only shape -- omitting it degrades
    # gracefully to the previous bounding-box rendering rather than failing
    # validation. A real, possibly irregular, contour is what the frontend's
    # review-before-export canvas now draws instead of a synthesized
    # axis-aligned rectangle for every part.
    contour_mm: list[ContourPointPreview] = []


class JobStatusResponse(CreateJobResponse):
    parts: list[JobPartStatus] = []
    sheet_width_mm: float | None = None
    sheet_height_mm: float | None = None
    sheet_margin_mm: float | None = None
    clearance_mm: float | None = None
    dpi: float | None = None
    packing_attempts: int | None = None
    placed_parts: list[PlacedPartPreview] = []
    sheets: list[SheetPreview] = []
    sheet_count: int = 0
    unplaced_part_ids: list[str] = []
    sheet_full: bool = False
    layout_message: str | None = None
    collision_report_valid: bool | None = None
    ready_to_confirm: bool = False


class ViolationPreview(BaseModel):
    severity: str
    part_id_a: str
    part_id_b: str | None
    detail: str
    measured_distance_mm: float | None


class SheetPreview(BaseModel):
    page_number: int
    placed_parts: list[PlacedPartPreview]
    collision_report_valid: bool
    violations: list[ViolationPreview] = []


class ComputeResponse(BaseModel):
    job_id: str
    placed_parts: list[PlacedPartPreview]
    sheets: list[SheetPreview] = []
    sheet_count: int = 0
    unplaced_part_ids: list[str]
    all_placed: bool
    collision_report_valid: bool
    violations: list[ViolationPreview]
    ready_to_confirm: bool
    sheet_full: bool
    processed_count: int
    total_count: int
    layout_message: str


class ProgressResponse(BaseModel):
    job_id: str
    done: int
    total: int
    placed: int | None = None
    message: str | None = None


class ConfirmRequest(BaseModel):
    mode: str = Field(default="RGB", pattern="^(RGB|RGBA)$")
    # CSS/Pillow colour notation, e.g. #ffffff, #202020, white, or gray.
    background_color: str = Field(default="#FFFFFF", min_length=1, max_length=64)


class QaViolationResponse(BaseModel):
    severity: str
    detail: str
    expected: str | None
    actual: str | None


class ConfirmResponse(BaseModel):
    job_id: str
    output_tiff_path: str
    export_accepted: bool
    qa_violations: list[QaViolationResponse]
    width_px: int
    height_px: int
    dpi: float
    page_count: int = 1
    layer_count: int = 0

