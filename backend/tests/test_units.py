from app.geometry.units import Resolution, mm_to_px, px_to_mm, sheet_canvas_size_px


def test_clearance_roundtrip():
    res = Resolution(300)
    clearance = 4.10
    px = mm_to_px(clearance, res)
    assert abs(px - 48.4251968503937) < 1e-9
    assert abs(px_to_mm(px, res) - clearance) < 1e-12


def test_sheet_canvas_size_uses_ceil():
    width, height = sheet_canvas_size_px(790, 1190, Resolution(300))
    assert width == 9331
    assert height == 14056
