"""Stand-in for the Pi scripts in simulator mode: prints progress like they do."""
import sys
import time

kind = sys.argv[1] if len(sys.argv) > 1 else "job"
steps = {"flatfield": ["1/4 Exposure...", "2/4 White balance (centre made neutral)...",
                       "3/4 Flat-field image...", "4/4 Checking...", "Saved camera.json and flat.npy"],
         "map": [f"{w}: spot {i}/19  Z 19.{580 + i:03d} mm" for w in ("BL", "BR", "MR", "ML", "FL", "FR")
                 for i in range(1, 20, 6)] + ["Focus map saved: 114 spots (simulated)"]}
try:
    print(f"[simulator] pretending to run {kind}", flush=True)
    for line in steps.get(kind, ["working..."] * 5):
        time.sleep(0.6)
        print(line, flush=True)
except KeyboardInterrupt:
    print("Stopped.", flush=True)
    sys.exit(1)
