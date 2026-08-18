from PIL import Image, ImageDraw
from shapely.geometry import Polygon

from app.geometry.units import Resolution
from app.nesting.engine import PlacedPart
from app.nesting.rotation import LockedRotation
from app.rasterization.tiff_export import export_multi_sheet_tiff
from app.validation.qa_check import run_qa_check


def test_multi_page_tiff_is_written_and_qa_checks_every_page(tmp_path):
    source = tmp_path / "part.png"
    image = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((0, 0, 3, 3), fill=(255, 0, 0, 255))
    image.save(source)
    part = PlacedPart(
        part_id="part",
        source_image_path=str(source),
        placed_shape_mm=Polygon([(1, 1), (5, 1), (5, 5), (1, 5)]),
        rotation=LockedRotation.DEG_0,
        source_centroid_px=(2, 2),
        alpha_bbox_px=(0, 0, 4, 4),
    )
    result = export_multi_sheet_tiff(
        [[part], [part]],
        10,
        8,
        Resolution(dpi=25.4),
        tmp_path / "pages.tiff",
    )

    assert result.page_count == 2
    # One image layer plus one independent Background layer on each page.
    assert result.layer_count == 4
    with Image.open(result.file_path) as exported:
        assert exported.n_frames == 2
        assert exported.size == (10, 8)

    qa = run_qa_check(
        result.file_path,
        [[part], [part]],
        10,
        8,
        0,
        Resolution(dpi=25.4),
        clearance_mm=1,
    )
    assert qa.is_valid
    assert qa.page_count == 2
    assert qa.checked_layers
