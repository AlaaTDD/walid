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
    layers = TiffImageSourceData.frombytes(data).layers
    part_id = job_storage.load_job_state(job_id).parts[0].part_id
    assert [layer.name for layer in layers] == [f"Image 0001 ({part_id})", "Background"]
    assert layers[0].asarray().shape[-1] == 4


def test_only_exported_originals_are_moved_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path / "jobs")
    client = TestClient(app)
    source = tmp_path / "picked.png"
    source.write_bytes(_png_bytes().read())
    untouched = tmp_path / "untouched.png"
    untouched.write_bytes(_png_bytes((0, 255, 0, 255)).read())
    job_id = _create_and_compute(client, source)
    archive_root = tmp_path / "processed"

    confirm = client.post(
        f"/layout/confirm/{job_id}",
        json={
            "mode": "RGB",
            "background_color": "black",
            "processed_images_path": str(archive_root),
        },
    )
    assert confirm.status_code == 200
    payload = confirm.json()
    archive = Path(payload["processed_images_directory"])
    assert payload["moved_processed_images_count"] == 1
    assert archive.parent == archive_root
    # move_processed_originals organises files into placed/ and unplaced/
    # subdirectories of the operation directory (see api/processed_images.py's
    # own docstring and _new_operation_directory/placed_dir/unplaced_dir). The
    # single uploaded part here is always placed (one small part on a 100x100mm
    # sheet), so it lands under the "placed" subdirectory, not directly under
    # the operation directory itself.
    assert (archive / "placed" / source.name).exists()
    assert not source.exists()
    assert untouched.exists()


def test_unplaced_originals_are_archived_separately_from_placed(tmp_path, monkeypatch):
    """A one-page export archives every uploaded original: placed parts go
    under the operation directory's placed/ subfolder, unplaced parts go
    under its unplaced/ subfolder -- see api/processed_images.py's
    move_processed_originals docstring for the documented, intentional
    placed/unplaced split. Both leave the original source path, so this test
    only asserts each ends up in the correct destination subfolder.
    """
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path / "jobs")
    client = TestClient(app)
    first_source = tmp_path / "first.png"
    second_source = tmp_path / "second.png"
    first_source.write_bytes(_png_bytes().read())
    second_source.write_bytes(_png_bytes((0, 255, 0, 255)).read())
    job_id = client.post("/jobs").json()["job_id"]

    upload = client.post(
        "/upload",
        data={
            "job_id": job_id,
            "dpi": "100",
            "client_part_ids_json": '["first", "second"]',
            "original_source_paths_json": f'["{first_source}", "{second_source}"]',
        },
        files=[
            ("files", (first_source.name, _png_bytes(), "image/png")),
            ("files", (second_source.name, _png_bytes((0, 255, 0, 255)), "image/png")),
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
    assert len(payload["unplaced_part_ids"]) == 1

    source_by_part = {
        part.part_id: Path(part.original_source_path)
        for part in job_storage.load_job_state(job_id).parts
    }
    placed_id = payload["sheets"][0]["placed_parts"][0]["part_id"]
    unplaced_id = payload["unplaced_part_ids"][0]
    confirm = client.post(
        f"/layout/confirm/{job_id}",
        json={"mode": "RGB", "processed_images_path": str(tmp_path / "archive")},
    )
    assert confirm.status_code == 200
    payload = confirm.json()
    # Both the placed and the unplaced original are moved (organised into
    # separate placed/ and unplaced/ subfolders of the operation directory),
    # not just the placed one -- see move_processed_originals' own docstring.
    assert payload["moved_processed_images_count"] == 2
    archive = Path(payload["processed_images_directory"])
    assert not source_by_part[placed_id].exists()
    assert not source_by_part[unplaced_id].exists()
    assert (archive / "placed" / source_by_part[placed_id].name).exists()
    assert (archive / "unplaced" / source_by_part[unplaced_id].name).exists()


def test_original_files_are_not_moved_when_qa_fails_or_source_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path / "jobs")
    client = TestClient(app)
    source = tmp_path / "changed.png"
    source.write_bytes(_png_bytes().read())
    job_id = _create_and_compute(client, source)
    # The image used for the export is the server copy; changing the original
    # must stop the post-success move rather than move unrelated new content.
    source.write_bytes(_png_bytes((0, 0, 255, 255)).read())

    confirm = client.post(
        f"/layout/confirm/{job_id}",
        json={"mode": "RGB", "processed_images_path": str(tmp_path / "archive")},
    )
    assert confirm.status_code == 409
    assert source.exists()
    assert job_storage.load_job_state(job_id).stage == "computed"
