"""Stand-in for the Pi scripts in simulator mode: prints progress like they do,
and for a scan writes small fake photos into the scan folder."""
import argparse
import os
import sys
import time

import cv2
import numpy as np


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
    per = 40                                 # the real scan takes ~213 per well
    total = per * len(wells)
    print(f"Field of view 3.22 x 2.41 mm, {per} photos per well, {total} in total.", flush=True)
    taken, t0 = 0, time.time()
    rng = np.random.default_rng()
    for w in wells:
        print(f"\n{w}:", flush=True)
        for i in range(per):
            img = np.full((308, 410, 3), 230, np.uint8)
            for _ in range(int(rng.integers(5, 60))):
                c = tuple(int(v) for v in rng.integers(0, (410, 308)))
                cv2.circle(img, c, 2, (120, 40, 110), -1)
            cv2.putText(img, f"{w} {i:04d}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (60, 60, 60), 2)
            cv2.imwrite(os.path.join(folder, f"{w}_{i:04d}.jpg"), img)
            taken += 1
            time.sleep(0.05)
            if taken % 20 == 0:
                el = time.time() - t0
                print(f"  {taken}/{total}  {el / 60:4.1f} min, "
                      f"~{el / taken * (total - taken) / 60:.1f} min left", flush=True)
        print(f"  {w} done in 0.1 min", flush=True)
    print(f"\nScan done: {taken} photos", flush=True)
    print(f"\nEverything is in {folder}", flush=True)


kind = sys.argv[1] if len(sys.argv) > 1 else "job"
steps = {"flatfield": ["1/4 Exposure...", "2/4 White balance (centre made neutral)...",
                       "3/4 Flat-field image...", "4/4 Checking...", "Saved camera.json and flat.npy"],
         "map": [f"{w}: spot {i}/19  Z 19.{580 + i:03d} mm" for w in ("BL", "BR", "MR", "ML", "FL", "FR")
                 for i in range(1, 20, 6)] + ["Focus map saved: 114 spots (simulated)"]}
try:
    print(f"[simulator] pretending to run {kind}", flush=True)
    if kind == "scan":
        fake_scan(sys.argv[2:])
    else:
        for line in steps.get(kind, ["working..."] * 5):
            time.sleep(0.6)
            print(line, flush=True)
except KeyboardInterrupt:
    print("\nStopped. Halting the stage...", flush=True)
    sys.exit(1)
