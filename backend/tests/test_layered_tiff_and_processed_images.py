import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from psdtags import TiffImageSourceData

from app.api import job_storage
from app.main import app


def _png_bytes(color=(255, 0, 0, 255)) -> io.BytesIO:
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((10, 10, 70, 70), fill=color)
    stream = io.BytesIO()
    image.save(stream, "PNG")
    stream.seek(0)
    return stream


def _create_and_compute(client: TestClient, source: Path) -> str:
    job_id = client.post("/jobs").json()["job_id"]
    upload = client.post(
        "/upload",
        data={
            "job_id": job_id,
            "dpi": "100",
            "client_part_ids_json": '["a"]',
            "original_source_paths_json": f'["{source}"]',
        },
        files=[("files", (source.name, _png_bytes(), "image/png"))],
    )
    assert upload.status_code == 200
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
    return job_id


def test_export_writes_editable_layers_and_background_color(tmp_path, monkeypatch):
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path / "jobs")
    client = TestClient(app)
    source = tmp_path / "source.png"
    source.write_bytes(_png_bytes().read())
    job_id = _create_and_compute(client, source)

    confirm = client.post(
        f"/layout/confirm/{job_id}",
        json={"mode": "RGB", "background_color": "#123456"},
    )
    assert confirm.status_code == 200
    assert confirm.json()["layer_count"] == 2

    output = Path(confirm.json()["output_tiff_path"])
    with Image.open(output) as image:
        assert image.getpixel((0, 0)) == (18, 52, 86)
        data = image.tag_v2[37724]


def test_upload_mirrors_original_into_images_uploaded_dir(tmp_path, monkeypatch):
    """copy_uploaded_image_into_images_dir (job_storage.py) is called from
    /upload for every accepted part. This confirms the client-visible mirror
    at images/<job_id>/uploaded/ actually receives a copy under the client's
    own original filename, while the durable internal upload (under
    DEFAULT_JOBS_ROOT, which every downstream stage reads from) is untouched
    by this copy -- see copy_uploaded_image_into_images_dir's own docstring
    for why this must be a copy, not a move, and must never fail the upload
    itself.
    """
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(job_storage, "DEFAULT_IMAGES_ROOT", tmp_path / "images")
    client = TestClient(app)
    source = tmp_path / "my_part.png"
    source.write_bytes(_png_bytes().read())
    job_id = client.post("/jobs").json()["job_id"]

    upload = client.post(
        "/upload",
        data={
            "job_id": job_id,
            "dpi": "100",
            "client_part_ids_json": '["a"]',
            "original_source_paths_json": f'["{source}"]',
        },
        files=[("files", (source.name, _png_bytes(), "image/png"))],
    )
    assert upload.status_code == 200

    mirrored = tmp_path / "images" / job_id / "uploaded" / source.name
    assert mirrored.exists()
    assert mirrored.read_bytes() == source.read_bytes()
    # The durable internal copy every downstream stage reads from must exist
    # independently of the mirror above.
    part = job_storage.load_job_state(job_id).parts[0]
    assert Path(part.stored_image_path).exists()


def test_export_refreshes_final_and_remaining_without_accumulating(tmp_path, monkeypatch):
    """sync_images_final_and_remaining (job_storage.py), called from
    /layout/confirm, must CLEAR final/ and remaining/ before repopulating
    them -- a recompute+re-export for the same job must not leave a stale
    export or a stale unplaced-image list sitting next to the current one.
    This test forces two confirms for the same job (by recomputing between
    them) and asserts the second confirm's images/<job_id>/final/ holds
    exactly one TIFF, not two.
    """
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(job_storage, "DEFAULT_IMAGES_ROOT", tmp_path / "images")
    client = TestClient(app)
    source = tmp_path / "source.png"
    source.write_bytes(_png_bytes().read())
    job_id = _create_and_compute(client, source)

    first_confirm = client.post(
        f"/layout/confirm/{job_id}",
        json={"mode": "RGB", "background_color": "#123456"},
    )
    assert first_confirm.status_code == 200

    final_dir = tmp_path / "images" / job_id / "final"
    remaining_dir = tmp_path / "images" / job_id / "remaining"
    assert len(list(final_dir.iterdir())) == 1
    # The single uploaded part fits on a 100x100mm sheet, so nothing is
    # unplaced or rejected -- remaining/ must be empty after this export.
    assert list(remaining_dir.iterdir()) == []

    # Recompute (still same single part, still fits) and confirm again --
    # this is the same job_id, so sync_images_final_and_remaining must clear
    # the previous TIFF before copying the new one in, not leave both.
    recompute = client.post(
        f"/layout/compute/{job_id}",
        json={
            "sheet_width_mm": 100,
            "sheet_height_mm": 100,
            "sheet_margin_mm": 2,
            "clearance_mm": 2,
            "dpi": 100,
        },
    )
    assert recompute.status_code == 200
    second_confirm = client.post(
        f"/layout/confirm/{job_id}",
        json={"mode": "RGB", "background_color": "#123456"},
    )
    assert second_confirm.status_code == 200
    assert len(list(final_dir.iterdir())) == 1


def test_delete_job_removes_both_internal_and_client_visible_trees(tmp_path, monkeypatch):
    """Regression test for the bug fixed in delete_nesting_job (main.py):
    DELETE /jobs/<job_id> previously removed only jobs/<job_id>/ under
    DEFAULT_JOBS_ROOT, leaving images/<job_id>/{uploaded,remaining,final}/
    under DEFAULT_IMAGES_ROOT on disk even though the job had vanished from
    the UI. This confirms both trees are gone after delete.
    """
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(job_storage, "DEFAULT_IMAGES_ROOT", tmp_path / "images")
    client = TestClient(app)
    source = tmp_path / "source.png"
    source.write_bytes(_png_bytes().read())
    job_id = _create_and_compute(client, source)

    confirm = client.post(
        f"/layout/confirm/{job_id}",
        json={"mode": "RGB", "background_color": "#123456"},
    )
    assert confirm.status_code == 200

    images_job_tree = tmp_path / "images" / job_id
    jobs_job_tree = tmp_path / "jobs" / job_id
    assert images_job_tree.exists()
    assert jobs_job_tree.exists()

    delete = client.delete(f"/jobs/{job_id}")
    assert delete.status_code == 200

    assert not images_job_tree.exists()
    assert not jobs_job_tree.exists()


