import time
import math
from shapely.geometry import Polygon
from app.nesting.nfp import compute_nfp

def create_star(points=50, radius1=50, radius2=20):
    coords = []
    for i in range(points * 2):
        angle = i * math.pi / points
        r = radius1 if i % 2 == 0 else radius2
        coords.append((r * math.cos(angle), r * math.sin(angle)))
    return Polygon(coords)

star = create_star(points=100) # 200 vertices

for tol in [0.0, 0.5, 1.0, 2.0, 5.0]:
    shape = star.simplify(tol) if tol > 0 else star
    t0 = time.time()
    for _ in range(5):
        try:
            compute_nfp(shape, shape)
        except Exception as e:
            print("Error", e)
    t1 = time.time()
    print(f"Tolerance {tol}mm: {len(shape.exterior.coords)} vertices, NFP takes {(t1-t0)/5:.4f} seconds")
