#!/usr/bin/env python3
"""
ColonyScope stitcher: runs on the laptop (or the Pi) after a scan.

  python stitch_scan.py "E:\\Clonetron\\2026-11-26_101500_GL261_4Gy.clonescan"
  python stitch_scan.py "C:\\My Documents\\Incubot\\scans\\plate_2026-11-24_110000"   (old folder scans)
  python stitch_scan.py FOLDER1 FOLDER2 ...        several scans in one go
  python stitch_scan.py "C:\\My Documents\\Incubot\\scans" --all
                                                              every scan in there not stitched yet
Options:
  --no-align     place photos by stage position only (no image matching)
  --no-plate     skip the whole-plate picture
  --rotate N     0 / 90 / 180 / 270 (default: what the scan used, else 180)
  --redo         stitch again even if <well>_stitched.jpg already exists

Needs Python 3 with numpy and opencv:
  pip install numpy opencv-python

Everything it needs is inside the .clonescan file (or the scan folder:
positions.csv, the photos, calibration.json, flat.npy and scan_info.json).
The .clonescan file itself is never changed: stitched pictures go into a
"<scan name> results" folder next to it.

It also measures where each well's rim really is and writes
well_offsets_suggested.json next to the stitched pictures. Copy that to the Pi as
~/scans/well_offsets.json and the next scans are centred on the wells and take
fewer photos (scan v14 uses it automatically).
"""
import argparse
import csv
import io
import json
import math
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clonescan import Scan, find_scans  # noqa: E402

BG_LEVEL = 225.0
WELL_RADIUS = 17.4
WELL_MAX = 6000
PLATE_MAX = 8000
REG_SCALE = 0.5
REG_MIN_RESPONSE = 0.12
REG_MAX_SHIFT = 80


# ---------------------------------------------------------------- flat-field
class FlatField:
    def __init__(self, flat):
        self.flat = None if flat is None else flat.astype(np.float32)
        self._gain = {}

    def apply(self, img):
        if self.flat is None:
            return img
        h, w = img.shape[:2]
        if (w, h) not in self._gain:
            f = cv2.resize(self.flat, (w, h), interpolation=cv2.INTER_AREA)
            self._gain[(w, h)] = BG_LEVEL / np.maximum(f, 1.0)
        return np.clip(img.astype(np.float32) * self._gain[(w, h)], 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- placing photos
def scaled_M(calib, w):
    return np.array(calib["M"], float) * (w / calib["size"][0])


def tile_offsets(tiles, M):
    x0, y0 = tiles[0][1], tiles[0][2]
    return np.array([-(M @ np.array([x - x0, y - y0])) for _, x, y in tiles], dtype=float)


def refine_offsets(tiles, P, w, h, flat, read):
    """Line up neighbouring photos by the cells in their overlap."""
    n = len(tiles)
    if n < 2:
        return P
    sw, sh = int(w * REG_SCALE), int(h * REG_SCALE)
    small = []
    for name, _, _ in tiles:
        img = read(name)
        small.append(None if img is None else
                     cv2.resize(flat.apply(img)[..., 1], (sw, sh), interpolation=cv2.INTER_AREA))
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            d = P[j] - P[i]
            ow, oh = w - abs(d[0]), h - abs(d[1])
            if ow < 0.04 * w or oh < 0.04 * h or (ow < w * 0.5 and oh < h * 0.5):
                continue
            if small[i] is None or small[j] is None:
                continue
            x0, x1 = max(0.0, d[0]), min(float(w), w + d[0])
            y0, y1 = max(0.0, d[1]), min(float(h), h + d[1])
            s = REG_SCALE
            ax0, ax1, ay0, ay1 = (int(round(v * s)) for v in (x0, x1, y0, y1))
            bx0, by0 = int(round((x0 - d[0]) * s)), int(round((y0 - d[1]) * s))
            a = small[i][ay0:ay1, ax0:ax1].astype(np.float32)
            b = small[j][by0:by0 + (ay1 - ay0), bx0:bx0 + (ax1 - ax0)].astype(np.float32)
            if a.shape != b.shape or min(a.shape) < 16 or a.std() < 6 or b.std() < 6:
                continue
            win = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
            (sx, sy), r = cv2.phaseCorrelate(a, b, win)
            corr = -np.array([sx, sy]) / s
            if r >= REG_MIN_RESPONSE and np.hypot(*corr) <= REG_MAX_SHIFT:
                pairs.append((i, j, d + corr, r))
    if not pairs:
        print("   (no overlaps with enough cells to line up; using stage positions)")
        return P
    keep = list(range(len(pairs)))
    Q = P.copy()
    for _ in range(3):
        A = np.zeros((n, n))
        bx, by = 0.02 * P[:, 0].copy(), 0.02 * P[:, 1].copy()
        A[np.diag_indices(n)] = 0.02
        for k in keep:
            i, j, m, r = pairs[k]
            wg = min(1.0, r * 3)
            A[i, i] += wg; A[j, j] += wg; A[i, j] -= wg; A[j, i] -= wg
            bx[j] += wg * m[0]; bx[i] -= wg * m[0]
            by[j] += wg * m[1]; by[i] -= wg * m[1]
        Q = np.column_stack([np.linalg.solve(A, bx), np.linalg.solve(A, by)])
        res = [np.hypot(*((Q[pairs[k][1]] - Q[pairs[k][0]]) - pairs[k][2])) for k in keep]
        good = [k for k, e in zip(keep, res) if e < 6.0]
        if len(good) == len(keep):
            break
        keep = good
    moved = np.hypot(*(Q - P - (Q - P).mean(axis=0)).T)
    print(f"   lined up {len(keep)} overlaps; photos moved up to {moved.max():.0f} px "
          f"(median {np.median(moved):.0f} px)")
    return Q


def _feather(tw, th, ramp):
    x = np.minimum(np.arange(tw), np.arange(tw)[::-1]).astype(np.float32)
    y = np.minimum(np.arange(th), np.arange(th)[::-1]).astype(np.float32)
    return np.clip(np.minimum.outer(y, x) / max(ramp, 1.0), 0.0, 1.0)[..., None]


def render(tiles, offs, w, h, max_side, flat, read):
    """Returns (canvas, scale, mn) before any rotation."""
    mn = offs.min(axis=0)
    W, H = offs[:, 0].max() - mn[0] + w, offs[:, 1].max() - mn[1] + h
    sc = min(1.0, max_side / max(W, H))
    tw, th = max(1, round(w * sc)), max(1, round(h * sc))
    canvas = np.zeros((int(H * sc) + th + 2, int(W * sc) + tw + 2, 3), np.uint8)
    filled = np.zeros(canvas.shape[:2], np.uint8)
    feather = _feather(tw, th, 0.06 * min(tw, th))
    for (name, _, _), o in zip(tiles, offs):
        img = read(name)
        if img is None:
            continue
        img = cv2.resize(flat.apply(img), (tw, th), interpolation=cv2.INTER_AREA)
        px, py = int(round((o[0] - mn[0]) * sc)), int(round((o[1] - mn[1]) * sc))
        reg = canvas[py:py + th, px:px + tw]
        f = filled[py:py + th, px:px + tw][..., None]
        a = np.where(f > 0, feather, 1.0)
        canvas[py:py + th, px:px + tw] = (reg * (1 - a) + img * a).astype(np.uint8)
        filled[py:py + th, px:px + tw] = 1
    return canvas[:int(H * sc), :int(W * sc)], sc, mn


def rotate(img, deg):
    r = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
         270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(int(deg) % 360)
    return img if r is None else cv2.rotate(img, r)


# ---------------------------------------------------------------- where is the well?
def find_rim(canvas, r_px):
    """Centre of the well rim circle in canvas pixels, or None."""
    k = 1000.0 / max(canvas.shape[:2])
    g = cv2.resize(canvas[..., 1], None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    g = cv2.GaussianBlur(g, (0, 0), 2)
    r = r_px * k
    c = cv2.HoughCircles(g, cv2.HOUGH_GRADIENT, dp=1, minDist=1000, param1=60, param2=25,
                         minRadius=int(r * 0.93), maxRadius=int(r * 1.07))
    if c is None:
        return None
    x, y, rr = c[0][0]
    return np.array([x, y]) / k, rr / k


def well_offset(canvas, sc, mn, tiles, Q, M, w, h, centre):
    """How far (mm, machine X/Y) the camera must move so a photo is centred on the rim."""
    ppm = math.sqrt(abs(np.linalg.det(M)))
    found = find_rim(canvas, WELL_RADIUS * ppm * sc)
    if found is None:
        return None
    qd, r_found = found
    c = np.array(centre)
    k = int(np.argmin([math.hypot(x - c[0], y - c[1]) for _, x, y in tiles]))
    pk = np.array(tiles[k][1:])
    qc = (Q[k] - M @ (c - pk) - mn + np.array([w / 2, h / 2])) * sc
    delta = -np.linalg.solve(M, (qd - qc) / sc)
    return delta, r_found / (ppm * sc)


# ---------------------------------------------------------------- one scan folder
def stitch_folder(path, a):
    """Stitch one scan: a .clonescan file or an unpacked scan folder."""
    try:
        scan = Scan(path)
    except ValueError as e:
        print(e)
        return
    if not scan.has("positions.csv"):
        print(f"{scan.name}: no positions.csv, skipped")
        return
    if not scan.has("calibration.json"):
        print(f"{scan.name}: no calibration.json in the scan. Copy ~/scans/calibration.json "
              f"from the Pi into the folder and run again.")
        return
    calib = scan.read_json("calibration.json")
    info = scan.read_json("scan_info.json", {})
    rot = a.rotate if a.rotate is not None else info.get("rotate", 180)
    flat = FlatField(scan.load_npy("flat.npy"))
    rows = list(csv.DictReader(io.StringIO(scan.read_text("positions.csv"))))
    wells = list(dict.fromkeys(r["well"] for r in rows))
    folder = scan.results_folder()
    read = scan.read_image
    print(f"\n{scan.name}: {len(rows)} photos, wells {', '.join(wells)}"
          + ("" if scan.manifest.get("complete", True) else "  (scan was stopped early)")
          + ("" if flat.flat is not None else "  (no flat.npy: no white-background correction)"))
    if scan.zip is not None:
        print(f" results go in: {folder}")
    t0 = time.time()
    first = read(rows[0]["file"])
    h, w = first.shape[:2]
    M = scaled_M(calib, w)
    all_tiles, all_offs, ref = [], [], None
    suggested = {}
    old = info.get("well_offsets_mm", {})
    centres = info.get("well_centres_machine_mm", {})
    for wl in wells:
        out = os.path.join(folder, f"{wl}_stitched.jpg")
        tiles = [(r["file"], float(r["machine_x_mm"]),
                  float(r["machine_y_mm"])) for r in rows if r["well"] == wl]
        print(f" {wl}: {len(tiles)} photos")
        P = tile_offsets(tiles, M)
        Q = refine_offsets(tiles, P, w, h, flat, read) if not a.no_align else P
        if a.redo or not os.path.exists(out):
            canvas, sc, mn = render(tiles, Q, w, h, WELL_MAX, flat, read)
            cv2.imwrite(out, rotate(canvas, rot), [cv2.IMWRITE_JPEG_QUALITY, 92])
            print(f"   saved {os.path.basename(out)}")
            centre = centres.get(wl)
            if centre is None:                       # older scans: middle of the photo grid
                xs, ys = [t[1] for t in tiles], [t[2] for t in tiles]
                centre = [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2]
            res = well_offset(canvas, sc, mn, tiles, Q, M, w, h, centre)
            if res is not None and math.hypot(*res[0]) > 3.0:   # probably the next well's rim
                res = None
            if res is None:
                print("   (couldn't find the well rim, no centre correction for this well)")
            else:
                d, r_mm = res
                ox, oy = old.get(wl, [0.0, 0.0])
                # well_centre(): cx = hx - WELLS_x, cy = hy + WELLS_y
                new = [round(ox - d[0], 2), round(oy + d[1], 2)]
                suggested[wl] = new
                print(f"   rim found (radius {r_mm:.1f} mm): photos were "
                      f"{math.hypot(*d):.2f} mm off the well centre")
        else:
            print(f"   {os.path.basename(out)} already there (use --redo to make it again)")
        if ref is None:
            ref = tiles[0]
        Pp = tile_offsets([ref] + tiles, M)[1:]
        all_tiles += tiles
        all_offs.append(Q - Q[0] + Pp[0])
    if len(wells) > 1 and not a.no_plate:
        out = os.path.join(folder, "plate_stitched.jpg")
        if a.redo or not os.path.exists(out):
            canvas, _, _ = render(all_tiles, np.vstack(all_offs), w, h, PLATE_MAX, flat, read)
            cv2.imwrite(out, rotate(canvas, rot), [cv2.IMWRITE_JPEG_QUALITY, 92])
            print(" saved plate_stitched.jpg")
    if suggested:
        for wl, v in old.items():
            suggested.setdefault(wl, v)
        path = os.path.join(folder, "well_offsets_suggested.json")
        with open(path, "w") as f:
            json.dump(suggested, f, indent=1)
        print(f" well centre corrections (mm): "
              + ", ".join(f"{k} {v[0]:+.2f}/{v[1]:+.2f}" for k, v in suggested.items()))
        print(f" saved {os.path.basename(path)}  (copy to the Pi as ~/scans/well_offsets.json)")
    print(f" done in {(time.time() - t0) / 60:.1f} min")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="+", help=".clonescan files or scan folders")
    ap.add_argument("--all", action="store_true", help="folder holds many scans: do each one")
    ap.add_argument("--no-align", action="store_true")
    ap.add_argument("--no-plate", action="store_true")
    ap.add_argument("--rotate", type=int, default=None, choices=(0, 90, 180, 270))
    ap.add_argument("--redo", action="store_true")
    a = ap.parse_args()
    folders = []
    for f in a.folders:
        f = os.path.expanduser(f.rstrip("/\\"))
        if a.all:
            folders += find_scans(f)
        else:
            folders.append(f)
    if not folders:
        sys.exit("No scans found.")
    for f in folders:
        stitch_folder(f, a)


if __name__ == "__main__":
    main()
