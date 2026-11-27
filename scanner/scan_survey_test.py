#!/usr/bin/env python3
"""
Survey test before changing how scans focus: in every well, focus at 9 spots
(centre, 4 at 7.5 mm, 4 near the rim at 14 mm), then photograph 8 spots near the
edge and 6 in the middle three ways, all with a 0.3 s wait:

  flat plane     height from a flat plane through the 9 spots (tilt only)
  curved         height from a gently curved surface through them (tilt + bowl/dome)
  autofocus      its own autofocus at that exact spot (the best possible)

Every photo is taken after a normal one-photo step (2.9 mm) onto the spot, like in
a real scan. Run with the app stopped:

  sudo systemctl stop clonetron
  cd ~/clonetron/scanner && python3 scan_survey_test.py
  sudo systemctl start clonetron

About 3 minutes per well. Results go to <USB stick>/Clonetron/survey_<date>_<time>/:
summary.txt (sharpness per well, edge vs middle, and how far the plane's height was
from each spot's own focus), survey_<well>.jpg (every spot, three ways side by side)
and photos/ (every photo).
"""
import argparse
import glob
import math
import os
import time

import cv2
import numpy as np

import plate_scan as ps

EDGE_R = 15.0                               # mm from the centre: near the rim, photo still inside
MIDDLE = [(-2.9, -2.2), (0.0, -2.2), (2.9, -2.2), (-2.9, 2.2), (0.0, 2.2), (2.9, 2.2)]
EDGE = [(round(EDGE_R * math.cos(math.radians(t)), 1), round(EDGE_R * math.sin(math.radians(t)), 1))
        for t in range(0, 360, 45)]
FOCUS_SPOTS = [(0.0, 0.0)] + \
    [(round(7.5 * math.cos(math.radians(t)), 1), round(7.5 * math.sin(math.radians(t)), 1)) for t in (0, 90, 180, 270)] + \
    [(round(14 * math.cos(math.radians(t)), 1), round(14 * math.sin(math.radians(t)), 1)) for t in (45, 135, 225, 315)]
WAYS = ("flat plane", "curved", "autofocus")


def measure(s, cam, w, home, z_guess, a, zmin, zmax):
    """Focus at the 9 spots (looking nearby for cells where a spot is empty).
    Returns [(dx, dy, z)] for the spots that focused."""
    cx, cy = ps.well_centre(w, home)
    pts = []
    for dx0, dy0 in FOCUS_SPOTS:
        dx, dy, dark = ps.find_cells(s, cam, cx, cy, dx0, dy0, a)
        ps.move(s, cx + dx, cy + dy, a.feed)
        if not pts:
            z, _ = ps.autofocus(s, cam, z_guess, zmin, zmax)
        else:
            z = ps.fine_focus(s, cam, float(np.median([p[2] for p in pts])), zmin, zmax)
        tag = "" if (dx, dy) == (dx0, dy0) else f" (cells {dx - dx0:+.0f},{dy - dy0:+.0f} mm away)"
        if z is None:
            print(f"  spot ({dx0:+5.1f},{dy0:+5.1f}) skipped{tag}", flush=True)
            continue
        pts.append((dx, dy, z))
        print(f"  spot ({dx0:+5.1f},{dy0:+5.1f})  Z {z - home[2]:.3f} mm{tag}", flush=True)
    return pts


def fit(pts, curved):
    """Least-squares surface z(dx, dy): flat = a + b dx + c dy; curved adds
    dx^2, dy^2, dx*dy (a bowl or dome, possibly lopsided). Drops one bad point."""
    P = np.array(pts, float)

    def design(x, y):
        cols = [np.ones_like(x), x, y]
        if curved:
            cols += [x * x, y * y, x * y]
        return np.stack(cols, -1)

    need = 6 if curved else 3
    if len(P) < need + 1:
        curved, need = False, 3
    keep = np.ones(len(P), bool)
    for _ in range(2):
        coef = np.linalg.lstsq(design(P[keep, 0], P[keep, 1]), P[keep, 2], rcond=None)[0]
        res = np.abs(design(P[:, 0], P[:, 1]) @ coef - P[:, 2])
        worst = int(np.argmax(np.where(keep, res, -1)))
        if res[worst] > 0.03 and keep.sum() > need + 1:
            keep[worst] = False                    # one spot disagreeing: leave it out
        else:
            break
    return lambda dx, dy: float(design(np.array([dx]), np.array([dy]))[0] @ coef)


def shoot(s, cam, x, y, z, settle, feed, sx):
    """Step onto (x, y) from one photo-width to the left, like a scan does, then shoot."""
    ps.move(s, x - sx, y, feed)
    ps.move(s, x, y, feed)
    ps.move_z(s, z)
    time.sleep(settle)
    return ps.grab(cam)


def main():
    p = argparse.ArgumentParser(description="Edge vs middle focus and wait-time survey")
    p.add_argument("--wells", default=",".join(ps.ORDER))
    p.add_argument("--feed", type=float, default=3000)
    a = p.parse_args()
    a.full = False
    wells = [w.strip().upper() for w in a.wells.split(",") if w.strip()]
    sticks = [d for d in glob.glob("/media/*/*/Clonetron") if os.access(d, os.W_OK)]
    folder = os.path.join(sticks[0] if sticks else ps.SCANS, time.strftime("survey_%Y-%m-%d_%H%M%S"))
    os.makedirs(os.path.join(folder, "photos"), exist_ok=True)
    print("Saving to", folder)

    m = ps.load_map()
    zs = [q[3] for q in (m or {}).get("points", [])]
    a.z = float(np.median(zs)) if zs else ps.Z_DEFAULT
    s, cam, home = ps.prepare(a, ps.SIZE)
    hz = home[2]
    zmin, zmax = hz + max(0.5, a.z - 3), hz + ps.Z_LIMIT
    rows, lines = [], []
    try:
        ps.set_colour(cam, False)
        fx, fy = ps.field_of_view(ps.load_calib(), ps.SIZE)
        sx = fx * 0.9
        z_guess = hz + a.z
        for w in wells:
            print(f"\n{w}: focusing at 9 spots...", flush=True)
            t0 = time.time()
            pts = measure(s, cam, w, home, z_guess, a, zmin, zmax)
            if len(pts) < 3:
                print("  fewer than 3 spots focused; well skipped", flush=True)
                continue
            z_guess = float(np.median([p[2] for p in pts]))
            flat, curved = fit(pts, False), fit(pts, True)
            t_plane = time.time() - t0
            cx, cy = ps.well_centre(w, home)
            spots = [("edge", d) for d in EDGE] + [("middle", d) for d in MIDDLE]
            shots = {}
            for k, (where, (dx, dy)) in enumerate(spots):
                zf, zc = flat(dx, dy), curved(dx, dy)
                x, y = cx + dx, cy + dy
                imgs = [shoot(s, cam, x, y, zf, 0.3, a.feed, sx),
                        shoot(s, cam, x, y, zc, 0.3, a.feed, sx)]
                za = ps.fine_focus(s, cam, zc, zmin, zmax)
                imgs.append(shoot(s, cam, x, y, za if za is not None else zc, 0.3, a.feed, sx))
                q = [ps.tile_quality(im) for im in imgs]
                shots[k] = imgs
                for j, im in enumerate(imgs):
                    cv2.imwrite(os.path.join(folder, "photos", f"{w}_{where}{k:02d}_{j}.jpg"), im,
                                [cv2.IMWRITE_JPEG_QUALITY, 92])
                rows.append({"well": w, "where": where, "k": k, "dark": q[0][0],
                             "q": [qq[1] for qq in q],
                             "dz_flat": None if za is None else 1000 * (za - zf),
                             "dz_curved": None if za is None else 1000 * (za - zc)})
                print(f"  {where:6s} ({dx:+5.1f},{dy:+5.1f}) mm  "
                      + "  ".join(f"{n}: {qq[1]:5.2f}" for n, qq in zip(WAYS, q))
                      + ("" if za is None else f"   own focus vs flat {1000 * (za - zf):+4.0f} um,"
                                               f" vs curved {1000 * (za - zc):+4.0f} um"),
                      flush=True)
            print(f"  {w} took {(time.time() - t0) / 60:.1f} min (plane {t_plane:.0f} s)", flush=True)
            # one picture per well: every spot, three ways (centre third of each photo)
            strips = []
            for k in sorted(shots):
                cells = []
                for im in shots[k]:
                    h, wd = im.shape[:2]
                    c = ps.even_out(im)[h // 3: 2 * h // 3, wd // 3: 2 * wd // 3]
                    cells.append(cv2.resize(c, None, fx=0.6, fy=0.6, interpolation=cv2.INTER_AREA))
                strips.append(np.hstack([np.pad(c, ((0, 4), (0, 4), (0, 0)), constant_values=255)
                                         for c in cells]))
            head = np.full((36, strips[0].shape[1], 3), 255, np.uint8)
            cw = strips[0].shape[1] // 3
            for j, n in enumerate(WAYS):
                cv2.putText(head, n, (8 + j * cw, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (40, 30, 30), 2, cv2.LINE_AA)
            cv2.imwrite(os.path.join(folder, f"survey_{w}.jpg"), np.vstack([head] + strips),
                        [cv2.IMWRITE_JPEG_QUALITY, 90])
    finally:
        try:
            ps.send(s, f"G53 G0 X{home[0]:.3f} Y{home[1]:.3f}")
            ps.position(s)
        except Exception:
            pass
        cam.stop(); cam.close(); s.close()

    # ---- summary: each photo's sharpness as % of the sharpest of its three
    def pct(r, j):
        return 100 * r["q"][j] / max(max(r["q"]), 1e-6)

    lines.append(f"{'well':5s} {'where':7s} {'spots':>5s}  " + "  ".join(f"{n:>11s}" for n in WAYS)
                 + "   off from own focus: flat / curved (median, worst)")
    for w in wells:
        for where in ("edge", "middle"):
            rs = [r for r in rows if r["well"] == w and r["where"] == where and r["dark"] >= ps.CONTENT_MIN]
            if not rs:
                continue
            med = [float(np.median([pct(r, j) for r in rs])) for j in range(3)]
            df = [abs(r["dz_flat"]) for r in rs if r["dz_flat"] is not None]
            dc = [abs(r["dz_curved"]) for r in rs if r["dz_curved"] is not None]
            lines.append(f"{w:5s} {where:7s} {len(rs):5d}  " + "  ".join(f"{v:10.0f}%" for v in med)
                         + (f"   {np.median(df):3.0f},{max(df):4.0f} um / {np.median(dc):3.0f},{max(dc):4.0f} um"
                            if df else ""))
    for where in ("edge", "middle"):
        rs = [r for r in rows if r["where"] == where and r["dark"] >= ps.CONTENT_MIN]
        if rs:
            med = [float(np.median([pct(r, j) for r in rs])) for j in range(3)]
            lines.append(f"{'ALL':5s} {where:7s} {len(rs):5d}  " + "  ".join(f"{v:10.0f}%" for v in med))
    lines += ["", "% = sharpness of that photo compared with the sharpest of the three photos",
              "of the same spot (100% = the best). Only spots with cells in them count.",
              "off from own focus = how far the flat / curved surface's height was from the",
              "height the spot's own autofocus found (sharp depth is about +-25 um). All 0.3 s."]
    text = "\n".join(lines)
    print("\n" + text)
    with open(os.path.join(folder, "summary.txt"), "w") as f:
        f.write(text + "\n")
    print(f"\nSaved summary.txt and survey_<well>.jpg in {folder}")


if __name__ == "__main__":
    main()
