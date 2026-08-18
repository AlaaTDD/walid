import io

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.api import job_storage
from app.main import app


def _png_bytes(seed: int) -> io.BytesIO:
    image = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((5 + seed, 5, 50 + seed, 50), fill=(255, 0, 0, 255))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    stream.seek(0)
    return stream


def test_upload_is_idempotent_and_resumable(tmp_path, monkeypatch):
    monkeypatch.setattr(job_storage, "DEFAULT_JOBS_ROOT", tmp_path)
    client = TestClient(app)

    created = client.post("/jobs")
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    ids = ["part-1", "part-2", "part-3"]

    first = client.post(
        "/upload",
        data={"job_id": job_id, "dpi": "100", "client_part_ids_json": '["part-1","part-2"]'},
        files=[
            ("files", ("a.png", _png_bytes(1), "image/png")),
            ("files", ("b.png", _png_bytes(2), "image/png")),
        ],
    )
    assert first.status_code == 200
    assert first.json()["new_count"] == 2
    assert first.json()["received_count"] == 2

    retry_all = client.post(
        "/upload",
        data={"job_id": job_id, "dpi": "100", "client_part_ids_json": str(ids).replace("'", '"')},
        files=[
            ("files", ("a.png", _png_bytes(1), "image/png")),
            ("files", ("b.png", _png_bytes(2), "image/png")),
            ("files", ("c.png", _png_bytes(3), "image/png")),
        ],
    )
    assert retry_all.status_code == 200
    payload = retry_all.json()
    assert payload["existing_count"] == 2
    assert payload["new_count"] == 1
    assert payload["received_count"] == 3

    status = client.get(f"/jobs/{job_id}")
    assert status.status_code == 200
    assert status.json()["received_count"] == 3
    assert len(status.json()["parts"]) == 3
