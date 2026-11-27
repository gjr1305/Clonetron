"""Stand-in for the Pi scripts in simulator mode: prints progress like they do,
and for a scan writes small fake photos into the scan folder."""
import argparse
import json
import os
import signal
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis"))
import clonescan  # noqa: E402


TW, TH = 410, 308                            # small fake photos
PX_MM = TW / 3.22                            # px per mm (3.22 mm field of view)


def fake_well(rng, r_mm=19.0):
    """A fake stained well: pale background, purple colonies of many sizes, dark rim."""
    n = int(2 * r_mm * PX_MM)
    img = np.full((n, n, 3), 232, np.uint8)
    c = n // 2
    for _ in range(int(rng.integers(40, 90))):
        a, rr = rng.uniform(0, 2 * np.pi), 16.5 * PX_MM * np.sqrt(rng.uniform())
        x, y = int(c + rr * np.cos(a)), int(c + rr * np.sin(a))
        size = int(rng.choice([6, 14, 30, 55]))
        for _ in range(size):
            dx, dy = rng.normal(0, size * 0.6, 2)
            cv2.circle(img, (int(x + dx), int(y + dy)), int(rng.integers(2, 5)), (140, 50, 120), -1)
    cv2.circle(img, (c, c), int(17.4 * PX_MM), (90, 90, 90), 6)
    return img


def vignette():
    """The camera's pink corners: green falls off faster than red and blue."""
    y, x = np.mgrid[0:TH, 0:TW].astype(np.float32)
    r2 = ((x - TW / 2) / (TW / 2)) ** 2 + ((y - TH / 2) / (TH / 2)) ** 2
    return np.stack([1 - 0.22 * r2, 1 - 0.45 * r2, 1 - 0.20 * r2], -1)   # B, G, R


def fake_scan(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="/tmp")
    p.add_argument("--name", default="")
    p.add_argument("--wells", default="BL,BR,MR,ML,FL,FR")
    a, _ = p.parse_known_args(argv)
    wells = a.wells.split(",")
    folder = os.path.join(a.out, time.strftime("%Y-%m-%d_%H%M%S") + ("_" + a.name if a.name else ""))
    os.makedirs(folder, exist_ok=True)
    print(f"SCAN_FOLDER: {folder}", flush=True)
    rng = np.random.default_rng()
    vig = vignette()
    np.save(os.path.join(folder, "flat.npy"), (232 * vig).astype(np.float32))
    with open(os.path.join(folder, "calibration.json"), "w") as f:
        json.dump({"M": [[-PX_MM, 0], [0, -PX_MM]], "size": [TW, TH]}, f)
    sx, sy = 3.22 * 0.9, 2.41 * 0.9
    R = 12.0                                  # smaller than the real 19 mm, to keep it quick
    grid = [(dx, dy) for dy in np.arange(-R, R + 0.01, sy) for dx in np.arange(-R, R + 0.01, sx)
            if dx * dx + dy * dy <= (R + 1.6) ** 2]
    total = len(grid) * len(wells)
    centres = {w: [100.0 - 40 * (i % 3), 50.0 + 40 * (i // 3)] for i, w in enumerate(wells)}
    with open(os.path.join(folder, "scan_info.json"), "w") as f:
        json.dump({"script": "sim_job.py", "made": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "wells": wells, "radius_mm": R, "rotate": 0, "size": [TW, TH],
                   "field_of_view_mm": [3.22, 2.41], "well_centres_machine_mm": centres}, f)
    print(f"Field of view 3.22 x 2.41 mm, {len(grid)} photos per well, {total} in total.", flush=True)
    taken, t0 = 0, time.time()
    csv = open(os.path.join(folder, "positions.csv"), "w")
    csv.write("file,well,machine_x_mm,machine_y_mm,machine_z_mm\n")
    for w in wells:
        print(f"\n{w}:", flush=True)
        big = fake_well(rng)
        c = big.shape[0] // 2
        for i, (dx, dy) in enumerate(grid):
            x0, y0 = int(c + dx * PX_MM - TW / 2), int(c + dy * PX_MM - TH / 2)
            img = (big[y0:y0 + TH, x0:x0 + TW].astype(np.float32) * vig).clip(0, 255).astype(np.uint8)
            name = f"{w}_{i:04d}.jpg"
            cv2.imwrite(os.path.join(folder, name), img)
            csv.write(f"{name},{w},{centres[w][0] + dx:.3f},{centres[w][1] + dy:.3f},19.6\n")
            taken += 1
            time.sleep(0.03)
            if taken % 20 == 0:
                el = time.time() - t0
                print(f"  {taken}/{total}  {el / 60:4.1f} min, "
                      f"~{el / taken * (total - taken) / 60:.1f} min left", flush=True)
        print(f"  {w} done in 0.1 min", flush=True)
    csv.close()
    print(f"\nScan done: {taken} photos", flush=True)
    print("PACKING", flush=True)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    out = clonescan.pack(folder, log=lambda m: print(m, flush=True))
    print(f"SCAN_FILE: {out}", flush=True)
    print(f"\nEverything is in {out}", flush=True)


kind = sys.argv[1] if len(sys.argv) > 1 else "job"
steps = {"flatfield": ["1/4 Exposure...", "2/4 White balance (centre made neutral)...",
                       "3/4 Flat-field image...", "4/4 Checking...", "Saved camera.json and flat.npy"],
         "map": [f"{w}: spot {i}/19  Z 19.{580 + i:03d} mm" for w in ("BL", "BR", "MR", "ML", "FL", "FR")
                 for i in range(1, 20, 6)] + ["Focus map saved: 114 spots (simulated)"]}
try:
    print(f"[simulator] pretending to run {kind}", flush=True)
    if kind == "scan":
        fake_scan(sys.argv[2:])
    elif kind == "map":                       # same lines plate_scan.py --make-map prints
        ws = sys.argv[sys.argv.index("--wells") + 1].split(",") if "--wells" in sys.argv else ["BL", "BR"]
        for w in ws:
            print(f"\n{w}:", flush=True)
            for i in range(19):
                time.sleep(0.08)
                print(f"  spot {i + 1:2d}       Z {19.58 + 0.002 * i:6.3f} mm", flush=True)
            print("  19 spots, range 38 um", flush=True)
        print(f"Focus map saved: {19 * len(ws)} spots (simulated)", flush=True)
    else:
        for line in steps.get(kind, ["working..."] * 5):
            time.sleep(0.6)
            print(line, flush=True)
except KeyboardInterrupt:
    print("\nStopped. Halting the stage...", flush=True)
    sys.exit(1)
