import os
import time
import requests
import glob
from pathlib import Path

BASE_URL = "http://127.0.0.1:8001"
IMAGE_DIR = "/Volumes/alaassD/untitled folder/New folder"
DOWNLOADS_DIR = os.path.expanduser("~/Downloads/NestingResults")

def run_test():
    images = glob.glob(os.path.join(IMAGE_DIR, "*.*"))
    images = [f for f in images if f.lower().endswith(('.png', '.jpg', '.jpeg', '.tif', '.tiff'))]
    
    print(f"Uploading {len(images)} images to {BASE_URL}...")
    
    job_id = None
    batch_size = 20
    for i in range(0, len(images), batch_size):
        batch = images[i:i+batch_size]
        files = []
        for img_path in batch:
            files.append(('files', (os.path.basename(img_path), open(img_path, 'rb'), 'image/png')))
        
        import json
        source_paths = [os.path.abspath(img) for img in batch]
        data = {
            'dpi': 300,
            'original_source_paths_json': json.dumps(source_paths)
        }
        if job_id:
            data['job_id'] = job_id
            
        print(f"Uploading batch {i//batch_size + 1}...")
        resp = requests.post(f"{BASE_URL}/upload", files=files, data=data)
        res_json = resp.json()
        job_id = res_json['job_id']
        for _, f_tuple in files:
            f_tuple[1].close()

    print(f"Upload complete. Job ID: {job_id}")

    compute_data = {
        "sheet_width_mm": 790.0,
        "sheet_height_mm": 1190.0,
        "sheet_margin_mm": 5.0,
        "clearance_mm": 4.10,
        "dpi": 300,
        "packing_attempts": 1
    }
    print("Requesting compute...")
    t0 = time.time()
    resp = requests.post(f"{BASE_URL}/layout/compute/{job_id}", json=compute_data)
    t1 = time.time()
    print(f"Compute done in {t1-t0:.2f} seconds!")
    
    res_json = resp.json()
    sheets = res_json.get("sheets", [])
    sheet_1_parts = len(sheets[0].get("placed_parts", [])) if sheets else 0
    print(f"Images placed on Sheet 1: {sheet_1_parts}")
    
    if sheet_1_parts < 120:
        print(f"WARNING: Target not met! Got {sheet_1_parts} (target 120-130).")
    else:
        print("SUCCESS! Target density reached.")
        
    print(f"Exporting results to {DOWNLOADS_DIR}...")
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    
    confirm_data = {
        "mode": "RGB",
        "background_color": "#808080",
        "processed_images_path": DOWNLOADS_DIR
    }
    
    resp = requests.post(f"{BASE_URL}/layout/confirm/{job_id}", json=confirm_data)
    if resp.status_code == 200:
        print("Export complete!")
    else:
        print("Export failed:", resp.text)

if __name__ == "__main__":
    run_test()
