#!/usr/bin/env python3
"""
Speed and sharpness test: photographs the same 40 spots in the middle of one well
several times, each time a different way, and compares time per photo and how
sharp the photos are. Run with the app stopped (it needs the stage and camera):

  sudo systemctl stop clonetron
  cd ~/clonetron/scanner && python3 scan_speed_test.py --well MR
  sudo systemctl start clonetron

The ways compared (same 40 spots, same order):
  A  as now:            wait 0.3 s before each photo, sharpness check, refocus every 10
  B  shorter wait:      wait 0.1 s, otherwise as A
  C  + background:      wait 0.1 s, sharpness check done while the stage moves on
  D  no refocusing:     as C, focus found once at the start and never again
  E  3 heights:         as D, but 3 photos per spot 40 um apart (keeps the sharpest)

Results go to <USB stick>/Clonetron/speedtest_<date>_<time>/ (or ~/scans/ without
a stick): summary.txt, comparison.jpg (the same 4 spots from every way, plus the 3
heights merged into one all-in-focus picture), and every photo.
"""
import argparse
import glob
import math
import os
import queue
import threading
import time

import cv2
import numpy as np

import plate_scan as ps


def merge_stack(imgs):
    """All-in-focus picture from photos at different heights: each pixel from the
    photo that is sharpest around it (local fine detail, smoothed)."""
    sharp = []
    for im in imgs:
        g = cv2.GaussianBlur(im[..., 1].astype(np.float32), (0, 0), 1.0)
        e = cv2.GaussianBlur(np.abs(cv2.Laplacian(g, cv2.CV_32F)), (0, 0), 6)
        sharp.append(e)
    pick = np.argmax(np.stack(sharp), axis=0)
    out = np.zeros_like(imgs[0])
    for k, im in enumerate(imgs):
        out[pick == k] = im[pick == k]
    return out


class Checker:
    """Sharpness check in a background thread (way C onwards)."""

    def __init__(self):
        self.q, self.res = queue.Queue(), {}
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            key, img = item
            self.res[key] = ps.tile_quality(img)

    def put(self, key, img):
        self.q.put((key, img))

    def finish(self):
        self.q.put(None)
        self.t.join()
        return self.res


def run_way(name, s, cam, tiles, cx, cy, z0, a, zlims, folder, settle, background,
            refocus, heights):
    """Photograph every tile one way. Returns (seconds, {tile: (dark, q)}, z_end)."""
    os.makedirs(os.path.join(folder, name), exist_ok=True)
    writer = ps.Writer()
    checker = Checker() if background else None
    res, z, since = {}, z0, 0
    print(f"\n{name}: {len(tiles)} spots, wait {settle} s, "
          f"{'background' if background else 'inline'} check, "
          f"{'refocus every 10' if refocus else 'no refocus'}, {len(heights)} height(s)",
          flush=True)
    t0 = time.time()
    for i, (dx, dy) in enumerate(tiles):
        ps.move(s, cx + dx, cy + dy, a.feed)
        for k, dz in enumerate(heights):
            ps.move_z(s, z + dz)
            time.sleep(settle)
            img = ps.grab(cam)
            key = (i, k)
            writer.put(os.path.join(folder, name, f"spot{i:02d}_h{k}.jpg"), img)
            if checker:
                checker.put(key, img)
            else:
                res[key] = ps.tile_quality(img)
        if refocus:
            since += 1
            dark = res[(i, 0)][0] if (i, 0) in res else 1.0
            if since >= 10 and dark >= ps.CONTENT_GOOD:
                since = 0
                zl = ps.fine_focus(s, cam, z, *zlims)
                if zl is not None:
                    z = zl
    secs = time.time() - t0
    if checker:
        res = checker.finish()
    writer.close()
    print(f"  {secs:.0f} s, {secs / len(tiles):.2f} s per spot", flush=True)
    return secs, res, z


def main():
    p = argparse.ArgumentParser(description="Compare ways of taking the photos")
    p.add_argument("--well", default="MR")
    p.add_argument("--cols", type=int, default=8)
    p.add_argument("--rows", type=int, default=5)
    p.add_argument("--dz", type=float, default=0.04, help="mm between the 3 heights (way E)")
    p.add_argument("--feed", type=float, default=3000)
    a = p.parse_args()
    a.full = False
    w = a.well.upper()
    if w not in ps.WELLS:
        raise SystemExit(f"Unknown well {w}. Use: {', '.join(ps.WELLS)}")

    sticks = [d for d in glob.glob("/media/*/*/Clonetron") if os.access(d, os.W_OK)]
    base = sticks[0] if sticks else ps.SCANS
    folder = os.path.join(base, time.strftime("speedtest_%Y-%m-%d_%H%M%S"))
    os.makedirs(folder, exist_ok=True)
    print("Saving to", folder)

    m = ps.load_map()
    zs = [q[3] for q in (m or {}).get("points", [])]
    a.z = float(np.median(zs)) if zs else ps.Z_DEFAULT
    size = ps.SIZE
    s, cam, home = ps.prepare(a, size)
    hz = home[2]
    zlims = (hz + max(0.5, a.z - 3), hz + ps.Z_LIMIT)
    try:
        ps.set_colour(cam, False)
        fx, fy = ps.field_of_view(ps.load_calib(), size)
        sx, sy = fx * 0.9, fy * 0.9
        tiles = []                                         # serpentine block in the middle
        for r in range(a.rows):
            row = [((c - (a.cols - 1) / 2) * sx, (r - (a.rows - 1) / 2) * sy) for c in range(a.cols)]
            tiles += row if r % 2 == 0 else row[::-1]
        cx, cy = ps.well_centre(w, home)

        print(f"\nFinding focus in {w}...", flush=True)
        tf = time.time()
        z0, where = ps.find_focus_spiral(s, cam, cx, cy, ps.spiral_order(tiles, sx, sy),
                                         hz + a.z, a, *zlims, hz)
        t_focus = time.time() - tf
        if z0 is None:
            raise SystemExit("Couldn't find focus in the middle of this well. "
                             "Try another well with --well.")
        print(f"  focus at Z {z0 - hz:.3f} mm, took {t_focus:.0f} s", flush=True)

        ways = [  # name, settle, background, refocus, heights
            ("A_as_now", 0.3, False, True, [0.0]),
            ("B_wait_0.1s", 0.1, False, True, [0.0]),
            ("C_background_check", 0.1, True, True, [0.0]),
            ("D_no_refocus", 0.1, True, False, [0.0]),
            ("E_3_heights", 0.1, True, False, [-a.dz, 0.0, a.dz]),
        ]
        results = {}
        for name, settle, bg, refocus, heights in ways:
            secs, res, _ = run_way(name, s, cam, tiles, cx, cy, z0, a, zlims, folder,
                                   settle, bg, refocus, heights)
            results[name] = (secs, res, len(heights))
    finally:
        try:
            ps.send(s, f"G53 G0 X{home[0]:.3f} Y{home[1]:.3f}")
            ps.position(s)
        except Exception:
            pass
        cam.stop(); cam.close(); s.close()

    # ---- compare: each spot's sharpness as % of the sharpest photo of that spot (any way)
    n = len(tiles)
    best = {}
    for name, (_, res, nh) in results.items():
        for i in range(n):
            for k in range(nh):
                d, q = res[(i, k)]
                best[i] = max(best.get(i, 0.0), q)
    has_cells = [i for i in range(n)
                 if max(results["A_as_now"][1][(i, 0)][0], 0) >= ps.CONTENT_MIN]
    lines = [f"Speed test in {w}: {n} spots ({len(has_cells)} with cells), "
             f"focus found in {t_focus:.0f} s at Z {z0 - hz:.3f} mm", "",
             f"{'way':22s} {'s/spot':>7s} {'plate est.':>10s} {'sharpness':>10s} {'sharp>=80%':>11s}"]
    for name, (secs, res, nh) in results.items():
        pct = [100 * max(res[(i, k)][1] for k in range(nh)) / max(best[i], 1e-6) for i in has_cells]
        med = float(np.median(pct)) if pct else float("nan")
        good = 100 * np.mean([p >= 80 for p in pct]) if pct else float("nan")
        plate_min = secs / n * 1278 / 60
        lines.append(f"{name:22s} {secs / n:7.2f} {plate_min:8.0f} min {med:8.0f} % {good:9.0f} %")
    lines += ["", "sharpness = each spot's sharpest photo as % of the sharpest photo of that",
              "spot taken any way (100 % = as sharp as it got). plate est. = time for 1278",
              "photos at this speed (not counting focus search, homing, packing)."]
    text = "\n".join(lines)
    print("\n" + text)
    with open(os.path.join(folder, "summary.txt"), "w") as f:
        f.write(text + "\n")

    # ---- comparison picture: 4 spots with the most cells, centre crop from every way
    pick = sorted(has_cells or range(n), key=lambda i: -results["A_as_now"][1][(i, 0)][0])[:4]
    cols = []
    for name, (_, res, nh) in results.items():
        col = []
        for i in pick:
            k = max(range(nh), key=lambda k: res[(i, k)][1])
            im = cv2.imread(os.path.join(folder, name, f"spot{i:02d}_h{k}.jpg"))
            col.append(im)
        cols.append((name, col))
    stack = []
    for i in pick:
        imgs = [cv2.imread(os.path.join(folder, "E_3_heights", f"spot{i:02d}_h{k}.jpg")) for k in range(3)]
        stack.append(merge_stack(imgs))
    cols.append(("E_merged_3_heights", stack))
    for i, im in zip(pick, stack):
        cv2.imwrite(os.path.join(folder, "E_3_heights", f"spot{i:02d}_merged.jpg"), im)
    H, W = cols[0][1][0].shape[:2]
    ch, cw = H // 3, W // 3                                # centre third, full detail
    tiles_img = []
    for name, col in cols:
        cells = [ps.even_out(im)[ch:2 * ch, cw:2 * cw] for im in col]
        head = np.full((40, cw, 3), 255, np.uint8)
        cv2.putText(head, name, (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (40, 30, 30), 2, cv2.LINE_AA)
        tiles_img.append(np.vstack([head] + cells))
    sheet = np.hstack([np.pad(t, ((0, 0), (0, 8), (0, 0)), constant_values=255) for t in tiles_img])
    cv2.imwrite(os.path.join(folder, "comparison.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"\nSaved summary.txt and comparison.jpg in {folder}")


if __name__ == "__main__":
    main()
