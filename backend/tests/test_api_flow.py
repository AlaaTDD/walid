import io

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.api import job_storage
from app.api.job_storage import StoredPart
from app.main import app


def _png_bytes() -> io.BytesIO:
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((10, 10, 70, 70), fill=(255, 0, 0, 255))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    stream.seek(0)
    return stream


def _jpeg_bytes() -> io.BytesIO:
    image = Image.new("RGB", (100, 100), (20, 130, 240))
    stream = io.BytesIO()
    image.save(stream, "JPEG")
    stream.seek(0)
    return stream


def test_rgb_jpeg_is_accepted_and_exported(tmp_path, monkeypatch):
    """Ordinary photos are normalized to opaque RGBA, not rejected for alpha."""
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path)
    client = TestClient(app)
    job_id = client.post("/jobs").json()["job_id"]

    upload = client.post(
        "/upload",
        data={"job_id": job_id, "dpi": "100", "client_part_ids_json": '["photo"]'},
        files=[("files", ("photo.jpg", _jpeg_bytes(), "image/jpeg"))],
    )
    assert upload.status_code == 200
    part = upload.json()["parts"][0]
    assert part["is_valid"] is True
    assert part["rejection_reason"] is None

    compute = client.post(
        f"/layout/compute/{job_id}",
        json={
            "sheet_width_mm": 100,
            "sheet_height_mm": 100,
            "sheet_margin_mm": 2,
            "clearance_mm": 2,
            "dpi": 100,
        },
    )
    assert compute.status_code == 200
    assert compute.json()["all_placed"] is True

    confirm = client.post(f"/layout/confirm/{job_id}", json={"mode": "RGB"})
    assert confirm.status_code == 200
    assert confirm.json()["export_accepted"] is True


def test_get_job_upgrades_legacy_rgb_rejection(tmp_path, monkeypatch):
    """Old jobs recover without forcing the user to upload the RGB photo again."""
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path)
    client = TestClient(app)
    state = job_storage.create_job()
    image_path = job_storage.uploads_dir(state.job_id) / "legacy.jpg"
    image_path.write_bytes(_jpeg_bytes().read())
    state.source_dpi = 100
    state.parts.append(
        StoredPart(
            part_id="legacy-photo",
            client_part_id="legacy-client",
            content_sha256="legacy",
            original_filename="legacy.jpg",
            stored_image_path=str(image_path),
            is_valid=False,
            rejection_reason="الصورة بصيغة RGB وليست RGBA — مالهاش alpha channel.",
        )
    )
    job_storage.save_job_state(state)

    status = client.get(f"/jobs/{state.job_id}")
    assert status.status_code == 200
    assert status.json()["parts"][0]["is_valid"] is True
    assert job_storage.load_job_state(state.job_id).parts[0].contour_wkt is not None


def test_full_api_flow_and_dpi_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path)
    client = TestClient(app)

    created = client.post("/jobs")
    assert created.status_code == 200
    job_id = created.json()["job_id"]

    upload = client.post(
        "/upload",
        data={"job_id": job_id, "dpi": "100", "client_part_ids_json": '["a","b"]'},
        files=[
            ("files", ("a.png", _png_bytes(), "image/png")),
            ("files", ("b.png", _png_bytes(), "image/png")),
        ],
    )
    assert upload.status_code == 200
    assert upload.json()["job_id"] == job_id

    mismatch = client.post(
        f"/layout/compute/{job_id}",
        json={"sheet_width_mm": 100, "sheet_height_mm": 100, "dpi": 200},
    )
    assert mismatch.status_code == 400

    compute = client.post(
        f"/layout/compute/{job_id}",
        json={
            "sheet_width_mm": 100,
            "sheet_height_mm": 100,
            "sheet_margin_mm": 2,
            "clearance_mm": 2,
            "dpi": 100,
        },
    )
    assert compute.status_code == 200
    payload = compute.json()
    assert payload["ready_to_confirm"] is True

    confirm = client.post(f"/layout/confirm/{job_id}", json={"mode": "RGB"})
    assert confirm.status_code == 200
    assert confirm.json()["export_accepted"] is True


def test_api_fills_one_tiff_page_and_leaves_the_rest_unplaced(tmp_path, monkeypatch):
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path)
    client = TestClient(app)
    job_id = client.post("/jobs").json()["job_id"]
    upload = client.post(
        "/upload",
        data={"job_id": job_id, "dpi": "100", "client_part_ids_json": '["a","b","c"]'},
        files=[
            ("files", ("a.png", _png_bytes(), "image/png")),
            ("files", ("b.png", _png_bytes(), "image/png")),
            ("files", ("c.png", _png_bytes(), "image/png")),
        ],
    )
    assert upload.status_code == 200

    compute = client.post(
        f"/layout/compute/{job_id}",
        json={
            "sheet_width_mm": 24,
            "sheet_height_mm": 24,
            "sheet_margin_mm": 2,
            "clearance_mm": 2,
            "dpi": 100,
        },
    )
    assert compute.status_code == 200
    payload = compute.json()
    assert payload["sheet_count"] == 1
    assert len(payload["sheets"]) == 1
    assert len(payload["sheets"][0]["placed_parts"]) == 1
    assert len(payload["unplaced_part_ids"]) == 2
    assert payload["sheet_full"] is True

    confirm = client.post(f"/layout/confirm/{job_id}", json={"mode": "RGB"})
    assert confirm.status_code == 200
    assert confirm.json()["page_count"] == 1
    assert confirm.json()["export_accepted"] is True
