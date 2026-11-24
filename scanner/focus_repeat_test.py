#!/usr/bin/env python3
"""
Focus repeatability test: stays at one spot and measures where the blur comes from.

  python3 ~/focus_repeat_test.py              # FR well centre, 5 repeats
  python3 ~/focus_repeat_test.py --well BL --repeats 8

Needs ~/plate_scan.py in the same folder (it borrows its
stage, camera and focus code). Takes about 3-5 minutes.

D  standing still : sharpness of 5 pictures with nothing moving (shimmer / flicker)
A  Z only         : Z moved away and back, then refocused
B  XY away + back : stage moved 5 mm away and back, then refocused
C  back to best Z : goes to the best height from above / below, sharpness there
"""
import argparse
import importlib.util
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "ps", os.path.join(HERE, "plate_scan.py"))
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--well", default="FR")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--z", type=float, default=None,
                    help="start height (default: from the focus map, else 18.0)")
    a = ap.parse_args()
    w = a.well.upper()
    n = a.repeats

    z_start = a.z
    mp = ps.load_map()
    if z_start is None and mp:
        pts = [p for p in mp["points"] if p[0] == w]
        if pts:
            z_start = float(np.median([p[3] for p in pts]))
    if z_start is None:
        z_start = ps.Z_DEFAULT
    args = argparse.Namespace(z=z_start, full=False, feed=3000)

    s, cam, home = ps.prepare(args, ps.SIZE)
    hx, hy, hz = home
    zmin, zmax = hz + max(0.5, z_start - 3), hz + ps.Z_LIMIT
    cx, cy = ps.well_centre(w, home)
    res = {}
    try:
        ps.move(s, cx, cy, 3000)
        time.sleep(0.5)
        ps.set_colour(cam, False)
        print(f"\n{w} centre, starting from Z {z_start:.3f} mm")
        z0, ratio = ps.autofocus(s, cam, hz + z_start, zmin, zmax)
        if z0 is None:
            sys.exit(f"Couldn't focus here (contrast {ratio:.2f}). Try another well or --z.")
        print(f"best focus Z {z0 - hz:.4f} mm (contrast {ratio:.1f})")
        ps.move_z(s, z0)

        # peak sharpness for reference: small sweep around z0
        zs, v = ps.sweep(s, cam, z0, 0.045, 0.015, zmin, zmax)
        peak = float(v.max())
        ps.move_z(s, z0)

        print(f"\nD  standing still ({n} pictures, nothing moving)...")
        time.sleep(0.3)
        d = [ps.sharpness(cam) for _ in range(n)]
        res["D"] = d

        print(f"A  Z away and back + refocus, {n}x...")
        za = []
        for k in range(n):
            ps.move_z(s, z0 + (0.2 if k % 2 == 0 else -0.2))
            z = ps.fine_focus(s, cam, z0, zmin, zmax)
            za.append(np.nan if z is None else z - hz)
        res["A"] = za

        print(f"B  XY 5 mm away and back + refocus, {n}x...")
        zb = []
        for k in range(n):
            ps.move(s, cx + 5, cy + 5, 3000)
            ps.move(s, cx, cy, 3000)
            time.sleep(0.1)
            z = ps.fine_focus(s, cam, z0, zmin, zmax)
            zb.append(np.nan if z is None else z - hz)
        res["B"] = zb

        print(f"C  back to best Z from above / below, {n}x each...")
        c_up, c_dn = [], []
        for k in range(n):
            ps.move_z(s, z0 - 0.1)          # below -> arrive moving up
            ps.move_z(s, z0)
            time.sleep(0.1)
            c_up.append(ps.sharpness(cam))
            ps.move_z(s, z0 + 0.1)          # above -> script overshoots below and comes up
            ps.move_z(s, z0)
            time.sleep(0.1)
            c_dn.append(ps.sharpness(cam))
        res["C_up"], res["C_dn"] = c_up, c_dn
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        cam.stop(); cam.close(); s.close()

    def um(xs):
        xs = np.array(xs, float)
        xs = xs[~np.isnan(xs)]
        if len(xs) < 2:
            return "not enough focus results"
        return (f"spread {1000 * (xs.max() - xs.min()):.0f} um, "
                f"st.dev {1000 * xs.std():.0f} um  ({len(xs)} ok)")

    def pct(xs):
        xs = np.array(xs, float) / peak * 100
        return f"{xs.mean():.0f}% of peak (range {xs.min():.0f}-{xs.max():.0f}%)"

    print("\n================ RESULTS ================")
    print(f"peak sharpness score at best focus: {peak:.1f}")
    if "D" in res:
        dd = np.array(res["D"]) / peak * 100
        print(f"D standing still : {pct(res['D'])}, wobble {dd.max() - dd.min():.0f}%")
    if "A" in res:
        print(f"A Z only         : {um(res['A'])}   heights {np.round(res['A'], 3)}")
    if "B" in res:
        print(f"B XY away + back : {um(res['B'])}   heights {np.round(res['B'], 3)}")
    if "C_up" in res:
        print(f"C from below     : {pct(res['C_up'])}")
        print(f"C from above     : {pct(res['C_dn'])}")
    print("""
How to read it (depth of focus is about +-27 um):
  D wobble over ~15%        -> the picture itself shimmers (air / light / vibration)
  A spread over ~30 um      -> autofocus or Z positioning is not precise enough
  B much bigger than A      -> moving the stage shifts the plate height (plate/holder/stage rocking)
  C well below ~85% of peak -> going back to the same Z doesn't land in the same place""")


if __name__ == "__main__":
    main()
