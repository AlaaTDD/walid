from PIL import Image, ImageDraw

from app.geometry.contour import extract_contour_from_image
from app.geometry.units import Resolution
from app.nesting.rotation import LockedRotation, rotate_shape


def test_90_degree_rotation_uses_image_orientation():
    image = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
    ImageDraw.Draw(image).polygon([(20, 15), (70, 20), (30, 60)], fill=(255, 0, 0, 255))
    contour = extract_contour_from_image(image, Resolution(300))
    rotated = rotate_shape(contour.polygon_mm, LockedRotation.DEG_90)
    assert rotated.centroid.distance(contour.polygon_mm.centroid) < 1e-9
    assert rotated.bounds != contour.polygon_mm.bounds
