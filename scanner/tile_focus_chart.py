#!/usr/bin/env python3
"""
Tile focus chart: how sharp is every photo of a scan, drawn over the stitched well.

  python3 ~/tile_focus_chart.py ~/scans/plate_2026-11-23_204914
  python3 ~/tile_focus_chart.py OLD_FOLDER NEW_FOLDER          # compare two scans
  python3 ~/tile_focus_chart.py FOLDER --tiles                 # also save white copies

For each folder it writes <well>_focus_chart.jpg into that folder:
  green  = sharp          (sharpness at least 85% of the well's typical)
  yellow = a bit soft     (70-85%)
  red    = blurry         (below 70%)
  grey   = nothing to judge (no cells in the photo)
Every box shows the photo number (FR_0034 -> 34) so you can open that photo.

With two folders it also writes compare_<well>.csv in the second folder and
prints which photos are blurry in both scans (same place = not the plate's fault).

--tiles saves a corrected (white background, like the stitched picture) copy of
every photo in <folder>/corrected/ so single photos are easy to look at.

Needs ~/plate_scan.py in the same folder (uses its flat-field,
sharpness and calibration code) and ~/scans/calibration.json.
"""
import argparse
import csv
import importlib.util
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "ps", os.path.join(HERE, "plate_scan.py"))
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)

GOOD, SOFT = 0.85, 0.70
COL = {"good": (60, 180, 60), "soft": (0, 200, 255), "bad": (40, 40, 230), "none": (150, 150, 150)}


def analyse(folder, well, export):
    with open(os.path.join(folder, "positions.csv")) as f:
        rows = [r for r in csv.DictReader(f) if r["well"] == well]
    tiles = [(os.path.join(folder, r["file"]), float(r["machine_x_mm"]),
              float(r["machine_y_mm"])) for r in rows]
    if not tiles:
        return None
    first = cv2.imread(tiles[0][0])
    h, w = first.shape[:2]
    if export:
        os.makedirs(os.path.join(folder, "corrected"), exist_ok=True)
    res = []
    for k, (path, _, _) in enumerate(tiles):
        img = cv2.imread(path)
        if img is None:
            res.append((None, None))
            continue
        res.append(ps.tile_quality(img))
        if export:
            cv2.imwrite(os.path.join(folder, "corrected", os.path.basename(path)),
                        ps.even_out(img), [cv2.IMWRITE_JPEG_QUALITY, 92])
        if (k + 1) % 50 == 0:
            print(f"   {k + 1}/{len(tiles)} photos checked", flush=True)
    content = [q for d, q in res if d is not None and d >= ps.CONTENT_MIN]
    med = float(np.median(content)) if content else 1.0
    out = []
    for (path, x, y), (d, q) in zip(tiles, res):
        num = int(os.path.splitext(os.path.basename(path))[0].split("_")[-1])
        if d is None or d < ps.CONTENT_MIN:
            out.append((num, x, y, None, "none"))
        else:
            r = q / med
            out.append((num, x, y, r, "good" if r >= GOOD else ("soft" if r >= SOFT else "bad")))
    return tiles, out, w, h


def draw(folder, well, tiles, out, w, h, rotate):
    calib = ps.load_calib()
    P = ps._tile_offsets(tiles, calib, w)
    mn = P.min(axis=0)
    W, H = P[:, 0].max() - mn[0] + w, P[:, 1].max() - mn[1] + h
    sc = min(1.0, ps.WELL_MAX / max(W, H))
    cw, ch = int(W * sc), int(H * sc)
    bg_path = os.path.join(folder, f"{well}_stitched.jpg")
    bg = cv2.imread(bg_path)
    if bg is not None:
        if rotate in (90, 270):
            bg = cv2.rotate(bg, cv2.ROTATE_90_COUNTERCLOCKWISE if rotate == 90
                            else cv2.ROTATE_90_CLOCKWISE)
        elif rotate == 180:
            bg = cv2.rotate(bg, cv2.ROTATE_180)
        canvas = cv2.resize(bg, (cw, ch))               # back to the un-rotated layout
    else:
        canvas = np.full((ch, cw, 3), 255, np.uint8)
    over = canvas.copy()
    tw, th = int(w * sc), int(h * sc)
    boxes = []
    for (num, _, _, r, cls), o in zip(out, P):
        x0, y0 = int((o[0] - mn[0]) * sc), int((o[1] - mn[1]) * sc)
        boxes.append((num, cls, r, x0, y0))
        cv2.rectangle(over, (x0 + 4, y0 + 4), (x0 + tw - 4, y0 + th - 4), COL[cls], -1)
    canvas = cv2.addWeighted(over, 0.35, canvas, 0.65, 0)
    for num, cls, r, x0, y0 in boxes:
        cv2.rectangle(canvas, (x0 + 4, y0 + 4), (x0 + tw - 4, y0 + th - 4), COL[cls], 6)
    rot = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
           270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(rotate % 360)
    if rot is not None:
        canvas = cv2.rotate(canvas, rot)
    # numbers are drawn after rotating so they read the right way up
    for num, cls, r, x0, y0 in boxes:
        cx, cy = x0 + tw // 2, y0 + th // 2
        if rotate % 360 == 180:
            cx, cy = cw - cx, ch - cy
        elif rotate % 360 == 90:
            cx, cy = ch - cy, cx
        elif rotate % 360 == 270:
            cx, cy = cy, cw - cx
        txt = str(num)
        fs = max(1.0, tw / 120)
        (tw2, th2), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, fs, 3)
        cv2.putText(canvas, txt, (cx - tw2 // 2, cy + th2 // 2), cv2.FONT_HERSHEY_SIMPLEX, fs,
                    (255, 255, 255), 9, cv2.LINE_AA)
        cv2.putText(canvas, txt, (cx - tw2 // 2, cy + th2 // 2), cv2.FONT_HERSHEY_SIMPLEX, fs,
                    (0, 0, 0), 3, cv2.LINE_AA)
    s = 2400 / max(canvas.shape[:2])
    canvas = cv2.resize(canvas, (int(canvas.shape[1] * s), int(canvas.shape[0] * s)),
                        interpolation=cv2.INTER_AREA)
    path = os.path.join(folder, f"{well}_focus_chart.jpg")
    cv2.imwrite(path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="+", help="one or two scan folders")
    ap.add_argument("--well", default=None, help="default: every well in the scan")
    ap.add_argument("--tiles", action="store_true", help="also save corrected copies of every photo")
    ap.add_argument("--rotate", type=int, default=ps.OUTPUT_ROTATE, choices=(0, 90, 180, 270),
                    help="same turn as the stitched picture (default 180)")
    a = ap.parse_args()
    if not ps.load_calib():
        sys.exit("No ~/scans/calibration.json, so photo positions can't be drawn.")
    folders = [os.path.expanduser(f.rstrip("/")) for f in a.folders][:2]
    results = []
    for folder in folders:
        with open(os.path.join(folder, "positions.csv")) as f:
            wells = list(dict.fromkeys(r["well"] for r in csv.DictReader(f)))
        if a.well:
            wells = [a.well.upper()]
        per = {}
        for wl in wells:
            print(f"{os.path.basename(folder)}  {wl}: checking photos...")
            r = analyse(folder, wl, a.tiles)
            if r is None:
                continue
            tiles, out, w, h = r
            path = draw(folder, wl, tiles, out, w, h, a.rotate)
            n = {k: sum(1 for o in out if o[4] == k) for k in COL}
            print(f"   sharp {n['good']}, a bit soft {n['soft']}, blurry {n['bad']}, "
                  f"nothing to judge {n['none']}")
            print(f"   blurry photos: {', '.join(str(o[0]) for o in out if o[4] == 'bad') or 'none'}")
            print(f"   chart: {path}")
            if a.tiles:
                print(f"   white copies of every photo: {os.path.join(folder, 'corrected')}")
            per[wl] = {o[0]: o for o in out}
        results.append(per)
    if len(results) == 2:
        for wl in set(results[0]) & set(results[1]):
            A, B = results[0][wl], results[1][wl]
            both = sorted(k for k in A if k in B and A[k][4] == "bad" and B[k][4] == "bad")
            only_a = sorted(k for k in A if A[k][4] == "bad" and (k not in B or B[k][4] != "bad"))
            only_b = sorted(k for k in B if B[k][4] == "bad" and (k not in A or A[k][4] != "bad"))
            print(f"\n{wl} comparison (same photo number = same place in the well):")
            print(f"   blurry in BOTH scans : {', '.join(map(str, both)) or 'none'}")
            print(f"   blurry only in first : {', '.join(map(str, only_a)) or 'none'}")
            print(f"   blurry only in second: {', '.join(map(str, only_b)) or 'none'}")
            cpath = os.path.join(folders[1], f"compare_{wl}.csv")
            with open(cpath, "w") as f:
                f.write("photo,first_sharpness,first,second_sharpness,second\n")
                for k in sorted(set(A) | set(B)):
                    ra = A.get(k, (k, 0, 0, None, "-"))
                    rb = B.get(k, (k, 0, 0, None, "-"))
                    f.write(f"{k},{'' if ra[3] is None else f'{ra[3]:.2f}'},{ra[4]},"
                            f"{'' if rb[3] is None else f'{rb[3]:.2f}'},{rb[4]}\n")
            print(f"   table: {cpath}")


if __name__ == "__main__":
    main()
