#!/usr/bin/env python3
"""
ColonyScope full-plate scan, version 9: "map once, scan fast", with tiles lined
up by their overlapping cells when stitching.

STEP 1, once (and again only if the plate holder or stage is changed):
  python3 plate_scan.py --make-map
  Autofocuses 19 spots in every well (centre + rings of 6 at 8, 14 and 15.5 mm)
  and saves the shape of the plate surface to ~/scans/focus_map.json.
  Takes about 9 min for the whole plate.
  Use a plate with plenty of stained cells everywhere (a dense control plate is
  ideal): spots with nothing on them can't be focused and are skipped.

STEP 2, every scan:
  python3 plate_scan.py                 # all 6 wells
  python3 plate_scan.py --wells FR      # one well
  For each well it does ONE quick focus check at the centre to find how far
  this plate sits above/below the map (plates differ a little), then takes
  every photo at the height the map gives, with no focusing in between.
  After each well it checks every photo for blur and re-shoots only the
  blurry ones (with a quick re-focus), so misses are fixed straight away.

Other options:
  --z 19.6            rough focus height to start from (default: from the focus map)
  --rotate 180        turn the stitched pictures (default 180; camera is turned 180)
  --calibrate         re-measure the camera first (after the camera was moved/turned)
  --af-every 10       also re-focus every 10th photo (slower; only if a plate is very wavy)
  --no-recheck        skip the blur check / re-shoots
  --no-af             no focusing at all, every photo at --z
  --focus-once        one autofocus at the first well centre, everything at that height
  --stitch-only FOLDER   re-stitch an earlier scan (works on v8 scans too)
  --no-align          stitch by stage positions only (the old way)
  --radius 19         mm around each well centre to photograph (default 19)
Ctrl+C stops the scan and still stitches the photos taken so far.

Output: ~/scans/plate_<date>_<time>/  (same as before)
  BL_0000.jpg ...  positions.csv  focus.csv  BL_stitched.jpg ...  plate_stitched.jpg

Why it is faster than v7: v7 spent most of its time focusing (5 full focus
searches per well plus a re-focus every 5 photos). v8 does that work once in
the map, focuses once per well, and saves photos in the background while the
stage is already moving to the next spot.

Version 9 (2026-11-23):
- Z slack allowance raised from 0.05 to 0.25 mm. Every Z move now finishes by
  coming UP from 0.25 mm below, so the Z screw's slack can't leave photos a bit
  too low after the height goes down between rows (the banded blur in v8).
- Up to 25% of a well can be re-shot (was 10%).
- Stitching: each photo is lined up with its neighbours using the cells in the
  10% overlap, all corrections are solved together, and seams are blended.

Version 14 (2026-11-24): scan only, stitch later.
- No stitching on the Pi (use --stitch if you want it there anyway). Each scan
  folder gets its own copy of calibration.json, flat.npy, camera.json,
  focus_map.json and a scan_info.json, so it can be stitched anywhere with
  stitch_scan.py (on the laptop, much faster than the Pi).
- 5 focus check points per well (centre + 4 of the map's 14 mm ring); --check-points 7
  for the old 7.
- Re-shoots capped at 10% of a well (--reshoot-max 0.25 for the old cap).
- Well centres: if ~/scans/well_offsets.json exists (the stitching script
  suggests one), the well centres are corrected with it and the default scan
  radius drops from 19 to 18 mm (fewer photos).

Version 13 (2026-11-23):
- Tilt check re-measures 7 of the focus MAP'S OWN spots in each well (centre +
  6 around it) and compares each with what the map found at that very spot,
  so an odd spot (thick colony, debris) fools the map and the check equally and
  cancels out. A point that disagrees with the rest by more than 25 um is
  thrown out before fitting, so one bad reading can't tilt the whole well.
- Map has an extra ring at 15.5 mm (19 spots per well), so the edge of the well
  isn't guessed. Remake the map once with --make-map.
- Blur check also re-shoots edge photos that are at least 40% inside the well.
- Run without --settle: 0.3 s is the default (0.1 s smears photos).

Version 12 (2026-11-23):
- Z slack allowance 0.5 mm (was 0.25): Z now lands within a few um every time.
- Per-well tilt check: 5 quick focus points (centre + 4 at 11 mm) measure how
  high AND how tilted this plate sits compared with the map (a plate reloaded
  in the clip can sit at a slightly different tilt), and every photo's height
  is corrected for both. Replaces the single centre check.
- Blur check / re-shoots only for photos inside the well, never on the rim.
- --z is optional: the start height now comes from the focus map.
- --rotate (default 180, because the camera was turned 180 degrees): turns the
  stitched pictures so they match the plate's real orientation.

Version 11 (2026-11-23): --focus-once test mode: one autofocus at the first
well centre, then every photo at that same height (no map, no re-focusing,
no blur check). Prints how long each part took (homing, focus, photos,
stitching) so the time can be compared with the other modes.

Version 10 (2026-11-23): scans a 19 mm radius around each well centre (was
17.4 mm, the well itself) so the whole rim is in the picture even when the
plate sits 1-2 mm off in the holder. Change with --radius.
"""
import argparse
import json
import shutil
import math
import os
import queue
import re
import sys
import threading
import time

import cv2
import numpy as np
import serial
from picamera2 import Picamera2


# ---------------------------------------------------------------- ColonyScope colour setup
# Shared by the calibrate, live and scan scripts (same code in each file).
# The camera's own lens-shading correction is switched off ("neutral" tuning),
# white balance + exposure are locked to the values measured by
# flatfield_calibrate.py, and the pink corners are removed in
# software by dividing every image by a flat-field image of the blank light pad.
SCANS_DIR = os.path.expanduser("~/scans")
CAMERA_FILE = os.path.join(SCANS_DIR, "camera.json")
FLAT_NPY = os.path.join(SCANS_DIR, "flat.npy")
BG_LEVEL = 225.0                     # corrected background brightness (0-255)


def neutral_tuning():
    """Stock IMX219 tuning with the built-in lens-shading correction switched off."""
    import glob
    pi5 = "Raspberry Pi 5" in open("/proc/device-tree/model").read()
    src = glob.glob("/usr/share/libcamera/ipa/rpi/" + ("pisp" if pi5 else "vc4")
                    + "/imx219.json")[0]
    with open(src) as f:
        t = json.load(f)
    for a in t["algorithms"]:
        if "rpi.alsc" in a:
            al = a["rpi.alsc"]
            al["n_iter"] = 0
            al["luminance_strength"] = 0.0
            al.pop("calibrations_Cr", None)
            al.pop("calibrations_Cb", None)
    return t


def open_camera():
    return Picamera2(tuning=neutral_tuning())


def load_camera_settings():
    try:
        with open(CAMERA_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def apply_camera_settings(cam, s):
    """Lock exposure and white balance to the calibrated values."""
    cam.set_controls({"AeEnable": False, "AwbEnable": False,
                      "ExposureTime": int(s["ExposureTime"]),
                      "AnalogueGain": float(s["AnalogueGain"]),
                      "ColourGains": tuple(float(v) for v in s["ColourGains"])})


class FlatField:
    """Divides images by the flat-field so the background is even and white."""

    def __init__(self):
        self.flat = None
        if os.path.exists(FLAT_NPY):
            self.flat = np.load(FLAT_NPY).astype(np.float32)
        self._gain = {}

    def gain(self, w, h):
        if (w, h) not in self._gain:
            f = cv2.resize(self.flat, (w, h), interpolation=cv2.INTER_AREA)
            self._gain[(w, h)] = BG_LEVEL / np.maximum(f, 1.0)
        return self._gain[(w, h)]

    def apply(self, img):
        if self.flat is None:
            return img
        g = self.gain(img.shape[1], img.shape[0])
        return np.clip(img.astype(np.float32) * g, 0, 255).astype(np.uint8)

PORT = "/dev/ttyACM0"
SIZE = (1640, 1232)                    # photo size (use --full for 3280x2464)
SCANS = os.path.expanduser("~/scans")
CALIB_FILE = os.path.join(SCANS, "calibration.json")
MAP_FILE = os.path.join(SCANS, "focus_map.json")
WELLS = {  # mm from home, as the jog tool shows (X "left", Y "towards")
    "BL": (66.0, 42.3), "BR": (66.0, 81.4),
    "ML": (105.1, 42.3), "MR": (105.1, 81.4),
    "FL": (144.2, 42.3), "FR": (144.2, 81.4),
}
WELL_OFFSETS = {}                      # filled from well_offsets.json, mm
ORDER = ["BL", "BR", "MR", "ML", "FL", "FR"]   # snake through the plate
WELL_RADIUS = 17.4
WELL_MAX = 6000                        # longest side of each well stitch, px
PLATE_MAX = 8000                       # longest side of the plate stitch, px
Z_DEFAULT = 18.0                       # usual focus height, mm up from home
Z_LIMIT = 28.0                         # never go above this (soft limit 30 minus pull-off)
Z_FEED = 200                           # mm/min for focus moves
BACKLASH = 0.50                        # mm: Z always finishes moving upwards from this far below
TILT_R = 11.0                          # mm from the well centre for the 4 tilt-check points
TILT_MAX = 0.02                        # mm per mm: ignore tilt fits steeper than this (20 um/mm)
OUTPUT_ROTATE = 180                    # degrees; camera was turned 180 on 2026-11-23
MAP_SPOTS = [(0.0, 0.0)] + [                 # centre, inner ring (8 mm), outer ring (14 mm)
    (round(r * math.cos(math.radians(a)), 1), round(r * math.sin(math.radians(a)), 1))
    for r, a0 in ((8.0, 0), (14.0, 30), (15.5, 0)) for a in range(a0, a0 + 360, 60)]
DARK_LEVEL = 205                       # smoothed pixel darker than this = stained stuff
CONTENT_MIN = 0.01                     # >1% such pixels: tile has something to judge focus on
BLUR_RATIO = 0.70                      # sharpness below 70% of the well's typical = blurry
OUTLIER_UM = 0.025                     # check point this far off the others is thrown out
RESHOOT_MAX = 0.10                     # re-shoot at most 10% of a well's photos
OFFSETS_FILE = os.path.join(SCANS, "well_offsets.json")
_FLAT = None


# ---------------------------------------------------------------- machine settings
# Values that differ between Clonetron builds can be overridden in a settings
# file, so another lab edits one file instead of this script. The file is
# ~/clonetron.json (or the path in the CLONETRON_CONFIG environment variable).
# Anything left out keeps the default above. See config/clonetron.example.json.
SETTINGS_FILE = os.environ.get("CLONETRON_CONFIG",
                               os.path.expanduser("~/clonetron.json"))


def load_settings(path=SETTINGS_FILE):
    global PORT, WELLS, ORDER, WELL_RADIUS, Z_DEFAULT, Z_LIMIT, BACKLASH, OUTPUT_ROTATE
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"(couldn't read settings file {path}: {e}; using built-in defaults)")
        return
    PORT = cfg.get("serial_port", PORT)
    if "wells_mm" in cfg:
        WELLS = {k: (float(v[0]), float(v[1])) for k, v in cfg["wells_mm"].items()}
    ORDER = cfg.get("scan_order", ORDER)
    WELL_RADIUS = float(cfg.get("well_radius_mm", WELL_RADIUS))
    Z_DEFAULT = float(cfg.get("z_default_mm", Z_DEFAULT))
    Z_LIMIT = float(cfg.get("z_limit_mm", Z_LIMIT))
    BACKLASH = float(cfg.get("z_backlash_mm", BACKLASH))
    OUTPUT_ROTATE = int(cfg.get("output_rotate_deg", OUTPUT_ROTATE))
    print(f"Settings loaded from {path}")


load_settings()


def even_out(img):
    global _FLAT
    if _FLAT is None:
        _FLAT = FlatField()
    return _FLAT.apply(img)


# ---------------------------------------------------------------- GRBL
def open_grbl():
    s = serial.Serial(PORT, 115200, timeout=0.1)
    time.sleep(2)
    s.reset_input_buffer()
    return s


def send(s, line, timeout=180):
    s.write((line + "\n").encode())
    buf, t = "", time.time()
    while time.time() - t < timeout:
        buf += s.read(s.in_waiting or 1).decode(errors="ignore")
        if "ok" in buf:
            return
        if "error" in buf or "ALARM" in buf:
            raise RuntimeError(f"{line} -> {buf.strip()}")
    raise RuntimeError(f"{line} -> no reply")


def position(s):
    """Wait until the stage has stopped, return machine X, Y, Z."""
    for _ in range(1200):
        s.write(b"?")
        time.sleep(0.05)
        r = s.read(s.in_waiting or 1).decode(errors="ignore")
        m = re.search(r"<(\w+)[|:].*?MPos:(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)", r)
        if m and m.group(1) == "Idle":
            return tuple(float(v) for v in m.groups()[1:])
    raise RuntimeError("stage did not become idle")


XY_OVERSHOOT = [0.5]                   # mm; set from the measured slack after calibration
_xy = [None, None]


def move(s, x, y, feed):
    """XY move that always arrives travelling in +X and +Y (takes up belt slack)."""
    ox = XY_OVERSHOOT[0] if _xy[0] is None or x < _xy[0] - 1e-6 else 0.0
    oy = XY_OVERSHOOT[0] if _xy[1] is None or y < _xy[1] - 1e-6 else 0.0
    if ox or oy:
        send(s, f"G53 G1 X{x - ox:.3f} Y{y - oy:.3f} F{feed:.0f}")
    send(s, f"G53 G1 X{x:.3f} Y{y:.3f} F{feed:.0f}")
    position(s)
    _xy[0], _xy[1] = x, y


# ---------------------------------------------------------------- camera frames
HAS_LORES = [False]


def grab(cam, stream="main"):
    """A frame that started after this call (so never one taken while moving)."""
    try:
        req = cam.capture_request(flush=True)
    except TypeError:                              # older picamera2: drop one frame instead
        cam.capture_array("main")
        req = cam.capture_request()
    try:
        return req.make_array(stream)
    finally:
        req.release()


def focus_image(cam):
    """Small grey picture for judging focus (uses the camera's small stream if possible)."""
    if HAS_LORES[0]:
        a = grab(cam, "lores")
        return a[:480, :640]
    a = grab(cam)
    return cv2.resize(a[..., 1], (640, 480), interpolation=cv2.INTER_AREA)


class Writer:
    """Saves photos in the background while the stage moves on."""

    def __init__(self):
        self.q = queue.Queue(maxsize=6)
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            path, img = item
            cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 95])

    def put(self, path, img):
        self.q.put((path, img))

    def close(self):
        self.q.put(None)
        self.t.join()


def tile_quality(img):
    """(fraction of dark stained pixels, sharpness score) for one photo.
    The score compares fine detail to coarse detail, so it doesn't depend much
    on how many cells are in the picture, only on how sharp they are."""
    small = even_out(cv2.resize(img, (640, 480), interpolation=cv2.INTER_AREA))
    g = small[48:432, 64:576, 1].astype(np.float32)
    # judged on a smoothed copy, so a badly blurred photo still counts as "has cells"
    dark = float((cv2.GaussianBlur(g, (0, 0), 6) < DARK_LEVEL).mean())
    g1 = cv2.GaussianBlur(g, (0, 0), 1.0)          # remove pixel noise first, or a very
    hp = float(cv2.Laplacian(g1, cv2.CV_32F).var())   # blurry photo can score as "sharp"
    bp = float((g1 - cv2.GaussianBlur(g, (0, 0), 6)).var())
    return dark, hp / max(bp, 1e-3)


# ---------------------------------------------------------------- focus
_zpos = [None]


def move_z(s, z):
    """Machine Z move. Downward moves overshoot and come back up (backlash)."""
    if _zpos[0] is not None and abs(z - _zpos[0]) < 0.002:
        return
    if _zpos[0] is None or z < _zpos[0]:
        send(s, f"G53 G1 Z{z - BACKLASH:.4f} F{Z_FEED}")
    send(s, f"G53 G1 Z{z:.4f} F{Z_FEED}")
    position(s)
    _zpos[0] = z


def sharpness(cam):
    g = focus_image(cam).astype(np.float32)
    h, w = g.shape[:2]
    g = g[h // 5: 4 * h // 5, w // 5: 4 * w // 5]
    return float(cv2.Laplacian(g, cv2.CV_32F).var())


def peak(zs, vals):
    """Best Z: parabola through the highest point and its neighbours."""
    i = int(np.argmax(vals))
    if 0 < i < len(vals) - 1:
        y0, y1, y2 = vals[i - 1], vals[i], vals[i + 1]
        d = y0 - 2 * y1 + y2
        if d < 0:
            return zs[i] + 0.5 * (y0 - y2) / d * (zs[1] - zs[0]), i
    return zs[i], i


def sweep(s, cam, centre, half, step, zmin, zmax):
    zs = np.arange(centre - half, centre + half + step / 2, step)
    zs = zs[(zs >= zmin) & (zs <= zmax)]
    vals = []
    for z in zs:                                   # always upwards
        move_z(s, float(z))
        time.sleep(0.05)
        vals.append(sharpness(cam))
    return zs, np.array(vals)


def autofocus(s, cam, z_start, zmin, zmax):
    """Full search. Returns (best machine Z, contrast ratio) or (None, ratio)."""
    centre = z_start
    for _ in range(3):                             # coarse, moving the window if needed
        zs, v = sweep(s, cam, centre, 0.30, 0.05, zmin, zmax)
        if len(zs) < 3:
            return None, 0.0
        best, i = peak(zs, v)
        if 0 < i < len(zs) - 1:
            break
        centre = zs[i]                             # peak at the edge: look further
    ratio = float(v.max() / max(np.median(v), 1e-6))
    if ratio < 1.15:
        return None, ratio
    zs, v = sweep(s, cam, best, 0.06, 0.015, zmin, zmax)   # fine
    best, _ = peak(zs, v)
    return float(best), ratio


def fine_focus(s, cam, z_pred, zmin, zmax):
    """Quick local focus around z_pred. Returns best Z or None (nothing to focus on)."""
    centre = z_pred
    for _ in range(2):
        zs, v = sweep(s, cam, centre, 0.09, 0.015, zmin, zmax)
        if len(zs) < 3:
            return None
        best, i = peak(zs, v)
        if 0 < i < len(zs) - 1:
            break
        centre = zs[i]                             # sharpest at the edge: look further
    if v.max() / max(np.median(v), 1e-6) < 1.08:
        return None
    return float(best)


class Surface:
    """Smooth shape of the plate bottom from the focus map, plus the small
    local bumps the map measured. Coordinates: mm from home (X, Y, Z)."""

    def __init__(self, pts):
        P = np.array(pts, dtype=float)
        X, Y, Z = P[:, 0], P[:, 1], P[:, 2]
        self.xm, self.ym = float(X.mean()), float(Y.mean())
        self.lo, self.hi = float(Z.min()) - 0.15, float(Z.max()) + 0.15
        A = self._terms(X, Y, len(P))
        self.coef, *_ = np.linalg.lstsq(A, Z, rcond=None)
        self.n = len(P)
        fit = A @ self.coef
        self.res = [(x, y, z - f) for x, y, z, f in zip(X, Y, Z, fit)]

    def _terms(self, X, Y, n):
        u, v = np.asarray(X) - self.xm, np.asarray(Y) - self.ym
        one = np.ones_like(u, dtype=float)
        if n >= 10:
            return np.column_stack([one, u, v, u * u, u * v, v * v])
        if n >= 3:
            return np.column_stack([one, u, v])
        return one[:, None]

    def __call__(self, x, y):
        z = float((self._terms(np.array([x]), np.array([y]), self.n) @ self.coef)[0])
        near = [(math.hypot(x - rx, y - ry), r) for rx, ry, r in self.res]
        near = [(d, r) for d, r in near if d < 14.0]
        if near:
            w = np.array([1.0 / (d * d + 1.0) for d, _ in near])
            z += float(np.dot(w, [r for _, r in near]) / (w.sum() + 1.0 / 36.0))
        return float(np.clip(z, self.lo, self.hi))


class FocusMap:
    """Map height for each tile + local corrections from re-focusing (--af-every)."""

    def __init__(self, base):
        self.base = base
        self.fixes = []                            # (dx, dy, residual)

    def add(self, dx, dy, z):
        r = z - self.base(dx, dy)
        self.fixes.append((dx, dy, r))
        return r

    def __call__(self, dx, dy):
        z = self.base(dx, dy)
        near = [(np.hypot(dx - fx, dy - fy), r) for fx, fy, r in self.fixes]
        near = [(d, r) for d, r in near if d < 8.0]
        if near:
            w = np.array([1.0 / (d * d + 0.25) for d, _ in near])
            z += float(np.dot(w, [r for _, r in near]) / w.sum())
        return z


def load_map():
    try:
        with open(MAP_FILE) as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- tiles
def tiles_for_well(step_x, step_y, r=WELL_RADIUS):
    """Same path as the dry run: serpentine, every tile touching the well."""
    nx, ny = math.ceil(r / step_x), math.ceil(r / step_y)
    rows = []
    for j in range(-ny, ny + 1):
        dy = j * step_y
        row = []
        for i in range(-nx, nx + 1):
            dx = i * step_x
            cx, cy = max(abs(dx) - step_x / 2, 0), max(abs(dy) - step_y / 2, 0)
            if cx * cx + cy * cy <= r * r:
                row.append((dx, dy))
        if row:
            rows.append(row if len(rows) % 2 == 0 else row[::-1])
    return [t for row in rows for t in row]


# ---------------------------------------------------------------- camera
def start_camera(size):
    cam = open_camera()
    try:                                            # small extra stream for fast focusing
        cfg = cam.create_still_configuration(main={"size": size, "format": "RGB888"},
                                             lores={"size": (640, 480), "format": "YUV420"},
                                             buffer_count=3)
        cam.configure(cfg)
        cam.start()
        HAS_LORES[0] = True
    except Exception as e:
        print(f"(small focus stream not available: {e}; using the normal one)")
        try:
            cam.stop()
        except Exception:
            pass
        cam.configure(cam.create_still_configuration(main={"size": size, "format": "RGB888"},
                                                     buffer_count=2))
        cam.start()
        HAS_LORES[0] = False
    time.sleep(2)                                   # let exposure settle
    return cam


def lock_exposure(cam):
    """Freeze exposure and colour so every tile has the same brightness."""
    md = cam.capture_metadata()
    ctl = {"AeEnable": False, "AwbEnable": False,
           "ExposureTime": md["ExposureTime"], "AnalogueGain": md["AnalogueGain"]}
    if "ColourGains" in md:
        ctl["ColourGains"] = md["ColourGains"]
    cam.set_controls(ctl)
    time.sleep(0.5)
    print(f"Exposure locked: {md['ExposureTime']} us, gain {md['AnalogueGain']:.2f}")


def gray(cam):
    a = grab(cam).astype(np.float32).mean(axis=2)
    return cv2.resize(a, (a.shape[1] // 2, a.shape[0] // 2))


def calibrate(s, cam, size, dist=1.0):
    """Pixels the picture moves per +1 mm of machine X and Y, measured with the
    belt slack already taken up, plus how much slack each axis has."""
    def jog(axis, d):
        send(s, f"$J=G91 G21 {axis}{d:.3f} F1000")
        position(s)
        time.sleep(0.4)

    cols, resp, slack = [], [], {}
    for axis in "XY":
        jog(axis, -1.0)
        jog(axis, +1.0)                 # now arriving in + direction: slack taken up
        a = gray(cam)
        jog(axis, +dist)
        b = gray(cam)
        jog(axis, -dist)                # direction change: slack shows up here
        c = gray(cam)
        jog(axis, -1.0)
        jog(axis, +1.0)                 # back to start, slack taken up again
        win = cv2.createHanningWindow(a.shape[::-1], cv2.CV_32F)
        (dx, dy), r = cv2.phaseCorrelate(a, b, win)
        (ex, ey), _ = cv2.phaseCorrelate(a, c, win)
        col = np.array([dx, dy]) * 2 / dist
        cols.append(col)
        resp.append(r)
        slack[axis] = float(np.hypot(ex, ey) * 2 / max(np.linalg.norm(col), 1e-6))
        print(f"  {axis}: {np.linalg.norm(col):.0f} px per mm, slack {slack[axis]:.3f} mm")
    M = np.column_stack(cols)
    c = {"M": M.tolist(), "size": list(size), "response": resp, "slack_mm": slack,
         "um_per_px": 1000 / float(np.mean(np.linalg.norm(M, axis=0))),
         "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    os.makedirs(SCANS, exist_ok=True)
    with open(CALIB_FILE, "w") as f:
        json.dump(c, f, indent=1)
    print(f"Calibrated: {c['um_per_px']:.2f} um per pixel, "
          f"match quality {min(resp):.2f} (want > 0.1)")
    return c


def field_of_view(calib, size):
    """Field of view in mm along machine X and Y, from the calibration.
    Uses the largest axis-aligned rectangle that fits inside the (maybe
    slightly rotated) camera picture."""
    if not calib:
        return 2.8, 2.8                            # safe guess without calibration
    w, h = size
    M = np.array(calib["M"]) * (w / calib["size"][0])   # px per mm, columns = X, Y
    Minv = np.linalg.inv(M)                        # mm per px
    # image edges expressed in machine mm: half-extent along X and Y of the picture
    ux = Minv @ np.array([w, 0.0])                 # picture width as a machine vector
    uy = Minv @ np.array([0.0, h])                 # picture height as a machine vector
    # the picture is a parallelogram spanned by ux, uy; for small rotations the
    # inscribed axis-aligned box is about (|a|-|b|) along each axis
    fx = max(abs(ux[0]), abs(uy[0])) - min(abs(ux[0]), abs(uy[0]))
    fy = max(abs(ux[1]), abs(uy[1])) - min(abs(ux[1]), abs(uy[1]))
    return float(fx), float(fy)


def load_calib():
    try:
        with open(CALIB_FILE) as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- stitch
# Version 9: the stage positions only give a first guess. Each photo is then
# lined up with its neighbours using the cells in the overlap (phase
# correlation), all the small corrections are solved together so they agree,
# and the seams are blended.
REG_SCALE = 0.5                 # match at half size (fast, still ~2 px accurate)
REG_MIN_RESPONSE = 0.12         # weaker matches (blank overlap) are ignored
REG_MAX_SHIFT = 80              # px (full size): bigger corrections are rejected


def _tile_offsets(tiles, calib, w):
    M = np.array(calib["M"]) * (w / calib["size"][0])   # scale if photo size differs
    x0, y0 = tiles[0][1], tiles[0][2]
    return np.array([-(M @ np.array([x - x0, y - y0])) for _, x, y in tiles], dtype=float)


def refine_offsets(tiles, P, w, h):
    """Line up neighbouring photos by their overlapping cells.
    P = stage-based offsets (n x 2, px). Returns improved offsets."""
    n = len(tiles)
    if n < 2:
        return P
    sw, sh = int(w * REG_SCALE), int(h * REG_SCALE)
    small = []
    for path, _, _ in tiles:                        # small grey copies, kept in memory
        img = cv2.imread(path)
        if img is None:
            small.append(None)
            continue
        g = even_out(img)[..., 1]
        small.append(cv2.resize(g, (sw, sh), interpolation=cv2.INTER_AREA))
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            d = P[j] - P[i]
            ow, oh = w - abs(d[0]), h - abs(d[1])
            if ow < 0.04 * w or oh < 0.04 * h:      # not touching / just a corner
                continue
            if ow < w * 0.5 and oh < h * 0.5:       # diagonal neighbours: skip
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
            if a.shape != b.shape or min(a.shape) < 16:
                continue
            if a.std() < 6 or b.std() < 6:          # blank background: nothing to match
                continue
            win = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
            (sx, sy), r = cv2.phaseCorrelate(a, b, win)
            corr = -np.array([sx, sy]) / s          # true offset = predicted + corr
            if r < REG_MIN_RESPONSE or np.hypot(*corr) > REG_MAX_SHIFT:
                continue
            pairs.append((i, j, d + corr, r))
    if not pairs:
        print("  (no overlaps with enough cells to line up; using stage positions)")
        return P
    # least squares: Q_j - Q_i = measured offset (weighted), Q_i ~ P_i (weak)
    Q = P.copy()
    keep = list(range(len(pairs)))
    for _ in range(3):
        A = np.zeros((n, n))
        bx, by = np.zeros(n), np.zeros(n)
        prior = 0.02
        for k in range(n):
            A[k, k] += prior
            bx[k] += prior * P[k, 0]
            by[k] += prior * P[k, 1]
        for idx in keep:
            i, j, m, r = pairs[idx]
            wgt = min(1.0, r * 3)
            A[i, i] += wgt; A[j, j] += wgt; A[i, j] -= wgt; A[j, i] -= wgt
            bx[j] += wgt * m[0]; bx[i] -= wgt * m[0]
            by[j] += wgt * m[1]; by[i] -= wgt * m[1]
        Q = np.column_stack([np.linalg.solve(A, bx), np.linalg.solve(A, by)])
        res = [np.hypot(*((Q[pairs[k][1]] - Q[pairs[k][0]]) - pairs[k][2])) for k in keep]
        good = [k for k, e in zip(keep, res) if e < 6.0]
        if len(good) == len(keep):
            break
        keep = good
    moved = np.hypot(*(Q - P - (Q - P).mean(axis=0)).T)
    print(f"  lined up {len(keep)} overlaps; photos moved by up to {moved.max():.0f} px "
          f"(median {np.median(moved):.0f} px) from the stage positions")
    return Q


def _feather(tw, th, ramp):
    """Weight map: 0 at the photo edge rising to 1 'ramp' pixels in."""
    x = np.minimum(np.arange(tw), np.arange(tw)[::-1]).astype(np.float32)
    y = np.minimum(np.arange(th), np.arange(th)[::-1]).astype(np.float32)
    wgt = np.minimum.outer(y, x) / max(ramp, 1.0)
    return np.clip(wgt, 0.0, 1.0)[..., None]


def render(tiles, offs, w, h, out_path, max_side, rotate=0):
    mn = offs.min(axis=0)
    W, H = offs[:, 0].max() - mn[0] + w, offs[:, 1].max() - mn[1] + h
    sc = min(1.0, max_side / max(W, H))
    tw, th = max(1, round(w * sc)), max(1, round(h * sc))
    canvas = np.zeros((int(H * sc) + th + 2, int(W * sc) + tw + 2, 3), np.uint8)
    filled = np.zeros(canvas.shape[:2], np.uint8)
    feather = _feather(tw, th, 0.06 * min(tw, th))
    for (path, _, _), o in zip(tiles, offs):
        img = cv2.imread(path)
        if img is None:
            continue
        img = cv2.resize(even_out(img), (tw, th), interpolation=cv2.INTER_AREA)
        px, py = int(round((o[0] - mn[0]) * sc)), int(round((o[1] - mn[1]) * sc))
        reg = canvas[py:py + th, px:px + tw]
        f = filled[py:py + th, px:px + tw][..., None].astype(np.float32)
        a = np.where(f > 0, feather, 1.0)                # blend only where already painted
        canvas[py:py + th, px:px + tw] = (reg * (1 - a) + img * a).astype(np.uint8)
        filled[py:py + th, px:px + tw] = 1
    canvas = canvas[:int(H * sc), :int(W * sc)]
    rot = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
           270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(int(rotate) % 360)
    if rot is not None:
        canvas = cv2.rotate(canvas, rot)
    cv2.imwrite(out_path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"  saved {os.path.basename(out_path)}  {canvas.shape[1]} x {canvas.shape[0]} px")


def stitch_folder(folder, calib, align=True, rotate=OUTPUT_ROTATE):
    rows = []
    with open(os.path.join(folder, "positions.csv")) as f:
        next(f)
        for line in f:
            name, well, x, y = line.strip().split(",")[:4]
            rows.append((well, os.path.join(folder, name), float(x), float(y)))
    if not rows:
        print("No photos to stitch.")
        return
    print("Stitching..." + (" (with flat-field correction)" if os.path.exists(FLAT_NPY)
                            else " (no flat.npy yet, so no flat-field correction)"))
    first = cv2.imread(rows[0][1])
    h, w = first.shape[:2]
    all_tiles, all_offs = [], []
    ref = None
    for wl in dict.fromkeys(r[0] for r in rows):
        tiles = [(p, x, y) for ww, p, x, y in rows if ww == wl]
        P = _tile_offsets(tiles, calib, w)
        print(f" {wl}: {len(tiles)} photos")
        Q = refine_offsets(tiles, P, w, h) if align else P
        render(tiles, Q, w, h, os.path.join(folder, f"{wl}_stitched.jpg"), WELL_MAX, rotate)
        # for the whole-plate picture: keep each well's internal fix, place wells by stage
        if ref is None:
            ref = tiles[0]
        Pp = _tile_offsets([ref] + tiles, calib, w)[1:]
        all_tiles += tiles
        all_offs.append(Q - Q[0] + Pp[0])
    if len({r[0] for r in rows}) > 1:
        render(all_tiles, np.vstack(all_offs), w, h, os.path.join(folder, "plate_stitched.jpg"),
               PLATE_MAX, rotate)


# ---------------------------------------------------------------- setup shared by map + scan
def prepare(a, size):
    """Home, raise the objective, set colour/exposure. Returns (serial, camera, home)."""
    s = open_grbl()
    cam = start_camera(size)
    print("Homing...")
    send(s, "$H")
    hx, hy, hz = position(s)
    send(s, "G21")
    if not 0.5 <= a.z <= Z_LIMIT:
        raise SystemExit(f"--z {a.z} is outside 0.5 to {Z_LIMIT} mm")
    print(f"Raising the objective to {a.z:.2f} mm...")
    move_z(s, hz + a.z)
    return s, cam, (hx, hy, hz)


def set_colour(cam, full):
    settings = load_camera_settings()
    if settings and not full:
        apply_camera_settings(cam, settings)
        time.sleep(1)
        print(f"Colour calibration from {settings.get('made', '?')} loaded")
        return
    if settings:          # full-size photos: keep the white balance, re-measure exposure
        cam.set_controls({"AwbEnable": False, "ColourGains": tuple(settings["ColourGains"])})
        time.sleep(1.5)
    else:
        print("WARNING: no colour calibration (run flatfield_calibrate.py)")
    lock_exposure(cam)


def well_centre(w, home):
    hx, hy, _ = home
    return hx - WELLS[w][0], hy + WELLS[w][1]


# ---------------------------------------------------------------- make the map
def make_map(a, wells, size):
    s, cam, home = prepare(a, size)
    hx, hy, hz = home
    zmin, zmax = hz + max(0.5, a.z - 3), hz + Z_LIMIT
    pts, t0 = [], time.time()
    try:
        move(s, *well_centre(wells[0], home), a.feed)
        time.sleep(0.5)
        set_colour(cam, a.full)
        z_guess = hz + a.z
        with open(os.path.join(SCANS, "focus_map.csv"), "w") as f:
            f.write("well,dx_mm,dy_mm,z_mm_from_home\n")
            for w in wells:
                cx, cy = well_centre(w, home)
                print(f"\n{w}:")
                found = []
                for dx, dy in MAP_SPOTS:
                    move(s, cx + dx, cy + dy, a.feed)
                    if not found:                    # first good spot in this well: full search
                        z, ratio = autofocus(s, cam, z_guess, zmin, zmax)
                    else:                            # then a quick search near it
                        z = fine_focus(s, cam, float(np.mean([p[2] for p in found])) + hz,
                                       zmin, zmax)
                        ratio = None
                    where = "centre" if (dx, dy) == (0.0, 0.0) else f"({dx:+.0f},{dy:+.0f}) mm"
                    if z is None:
                        print(f"  {where:14s} skipped (nothing sharp here)")
                        continue
                    found.append((dx, dy, z - hz))
                    pts.append([w, cx + dx - hx, cy + dy - hy, z - hz])
                    f.write(f"{w},{dx},{dy},{z - hz:.4f}\n")
                    f.flush()
                    print(f"  {where:14s} Z {z - hz:6.3f} mm")
                    z_guess = z
                if len(found) >= 2:
                    zz = [p[2] for p in found]
                    print(f"  {len(found)} spots, range {1000 * (max(zz) - min(zz)):.0f} um")
                else:
                    print(f"  only {len(found)} spot(s) focused in this well")
                print(f"  ({(time.time() - t0) / 60:.1f} min so far)")
    except KeyboardInterrupt:
        print("\nStopped. Halting the stage...")
        s.write(b"!"); time.sleep(0.3); s.write(b"\x18"); time.sleep(1)
    finally:
        cam.stop(); cam.close(); s.close()

    if len(pts) < 3:
        sys.exit("\nFewer than 3 spots focused, so no map was saved. Use a plate with "
                 "stained cells spread across the wells, and check focus with the live tool.")
    zz = [p[3] for p in pts]
    m = {"made": time.strftime("%Y-%m-%d %H:%M:%S"), "points": pts,
         "note": "X, Y, Z in mm from home; Z = focus height"}
    os.makedirs(SCANS, exist_ok=True)
    with open(MAP_FILE, "w") as f:
        json.dump(m, f, indent=1)
    print(f"\nFocus map saved: {len(pts)} spots, whole plate range "
          f"{1000 * (max(zz) - min(zz)):.0f} um (depth of focus is ~55 um)")
    print(f"  {MAP_FILE}\n  {os.path.join(SCANS, 'focus_map.csv')}  (open in Excel to look at it)")
    print(f"Took {(time.time() - t0) / 60:.1f} min. Now scan with:  "
          f"python3 {os.path.basename(sys.argv[0])}")


# ---------------------------------------------------------------- scan
def check_offset(s, cam, surf, w, home, offset, first, a, zmin, zmax, mp):
    """One quick focus at the well centre (or a nearby map spot) to find how far
    this plate sits from the map. Returns the new offset (mm) or None."""
    hx, hy, hz = home
    cx, cy = well_centre(w, home)
    spots = [(0.0, 0.0)] + [(p[1] - (cx - hx), p[2] - (cy - hy)) for p in mp
                            if p[0] == w and (abs(p[1] - (cx - hx)) > 1 or abs(p[2] - (cy - hy)) > 1)][:2]
    for dx, dy in spots:
        X, Y = cx + dx - hx, cy + dy - hy
        pred = hz + surf(X, Y) + offset
        move(s, cx + dx, cy + dy, a.feed)
        if first:
            z, _ = autofocus(s, cam, pred, zmin, zmax)
        else:
            z = fine_focus(s, cam, pred, zmin, zmax)
        if z is not None:
            new = (z - hz) - surf(X, Y)
            where = "centre" if (dx, dy) == (0.0, 0.0) else f"({dx:+.0f},{dy:+.0f}) mm"
            print(f"  focus check at {where}: map {surf(X, Y):.3f} mm, here {z - hz:.3f} mm "
                  f"-> offset {1000 * new:+.0f} um")
            return new
    return None


def inside_fraction(dx, dy, fx, fy, r=WELL_RADIUS - 0.3):
    """Fraction of a photo (centre dx, dy; size fx x fy mm) that lies inside the well."""
    gx, gy = np.meshgrid(np.linspace(-fx / 2, fx / 2, 7), np.linspace(-fy / 2, fy / 2, 7))
    return float((np.hypot(dx + gx, dy + gy) <= r).mean())


def check_spots(mp_points, w, home, n=5):
    """Pick the centre + the 6 map spots of the 14 mm ring of this well.
    Returns [(dx, dy, map_z)], dx/dy in mm from the well centre."""
    hx, hy, _ = home
    cx, cy = well_centre(w, home)
    pts = [(p[1] - (cx - hx), p[2] - (cy - hy), p[3]) for p in mp_points if p[0] == w]
    centre = [p for p in pts if math.hypot(p[0], p[1]) < 2]
    ring = [p for p in pts if 11 < math.hypot(p[0], p[1]) < 15]     # the 14 mm ring
    if len(ring) < 4:
        ring = [p for p in pts if 6 < math.hypot(p[0], p[1]) < 15]
    out = centre[:1]
    for ang in ((30, 150, 90, 270) if n <= 5 else range(30, 390, 60)):
        if not ring:
            break
        best = min(ring, key=lambda p: abs((math.degrees(math.atan2(p[1], p[0])) - ang + 180)
                                           % 360 - 180))
        ring.remove(best)
        out.append(best)
    return out


def fit_plane(pts, b, c):
    """pts = [(dx, dy, r)]. Plane fit, dropping points that disagree.
    Returns (a0, b, c, kept, dropped)."""
    keep, dropped = list(pts), []
    while True:
        if len(keep) >= 3:
            A = np.array([[1.0, dx, dy] for dx, dy, _ in keep])
            rr = np.array([r for _, _, r in keep])
            coef, *_ = np.linalg.lstsq(A, rr, rcond=None)
            if abs(coef[1]) > TILT_MAX or abs(coef[2]) > TILT_MAX:
                coef = np.array([np.median(rr - b * A[:, 1] - c * A[:, 2]), b, c])
        else:
            coef = np.array([np.median([r - b * dx - c * dy for dx, dy, r in keep]), b, c])
        if len(keep) < 4:
            return (*map(float, coef), keep, dropped)
        res = [abs(coef[0] + coef[1] * dx + coef[2] * dy - r) for dx, dy, r in keep]
        k = int(np.argmax(res))
        if res[k] <= OUTLIER_UM:
            return (*map(float, coef), keep, dropped)
        dropped.append(keep.pop(k))


def check_plane(s, cam, surf, w, home, corr, a, zmin, zmax, fcsv=None, mp_points=()):
    """Re-measure 7 of the map's own spots in this well and fit how this plate
    sits compared with the map: residual = a0 + b*dx + c*dy (mm)."""
    hx, hy, hz = home
    cx, cy = well_centre(w, home)
    a0, b, c = corr
    spots = check_spots(mp_points, w, home, getattr(a, "check_points", 7))
    if len(spots) < 3:                             # map has too few spots here: use the surface
        spots = [(dx, dy, surf(cx + dx - hx, cy + dy - hy))
                 for dx, dy in ((0, 0), (11, 0), (-11, 0), (0, 11), (0, -11))]
    pts = []
    for k, (dx, dy, zmap) in enumerate(spots):
        pred = hz + zmap + a0 + b * dx + c * dy
        move(s, cx + dx, cy + dy, a.feed)
        z = fine_focus(s, cam, pred, zmin, zmax)
        if z is None and k == 0:                   # centre missed: full search there
            z, _ = autofocus(s, cam, pred, zmin, zmax)
        if z is None:
            continue
        r = (z - hz) - zmap
        pts.append((dx, dy, r))
        if k == 0:
            a0 = r                                 # aim the rest from the centre
        if fcsv:
            fcsv.write(f"{w},check-point,{dx:.1f},{dy:.1f},{z - hz:.4f},"
                       f"{1000 * r:+.0f} um from this spot in the map\n")
    if not pts:
        print("  focus check: nothing sharp at any check point, keeping the previous correction")
        return corr
    a0n, bn, cn, keep, dropped = fit_plane(pts, b, c)
    if fcsv:
        for dx, dy, r in dropped:
            fcsv.write(f"{w},check-dropped,{dx:.1f},{dy:.1f},,{1000 * r:+.0f} um "
                       f"(disagreed with the others)\n")
    res = [abs(a0n + bn * dx + cn * dy - r) for dx, dy, r in keep]
    print(f"  focus check ({len(keep)} of {len(spots)} points used"
          + (f", {len(dropped)} thrown out" if dropped else "") + f"): height "
          f"{1000 * a0n:+.0f} um, tilt {1000 * bn:+.1f} across / {1000 * cn:+.1f} up-down um/mm, "
          f"points fit within {1000 * max(res):.0f} um")
    return a0n, bn, cn


def scan(a, wells, size):
    T0 = time.time()
    mp = None if (a.no_af or a.focus_once) else load_map()
    if not (a.no_af or a.focus_once) and not mp:
        sys.exit("No focus map yet. Make one first (once, ~10-15 min):\n"
                 f"  python3 {os.path.basename(sys.argv[0])} --make-map")
    surf = Surface([p[1:] for p in mp["points"]]) if mp else None
    if mp:
        print(f"Focus map from {mp['made']} ({len(mp['points'])} spots)")

    folder = os.path.join(SCANS, time.strftime("plate_%Y-%m-%d_%H%M%S"))
    os.makedirs(folder, exist_ok=True)
    for f in ("camera.json", "flat.npy", "focus_map.json"):
        if os.path.exists(os.path.join(SCANS, f)):
            shutil.copy2(os.path.join(SCANS, f), folder)
    t_home = time.time()
    s, cam, home = prepare(a, size)
    t_home = time.time() - t_home
    hx, hy, hz = home
    zmin, zmax = hz + max(0.5, a.z - 3), hz + Z_LIMIT
    writer = Writer()
    taken, offset = 0, 0.0
    z_fixed = hz + a.z                         # height used when there is no map
    t_focus, t_photos, t_stitch = 0.0, 0.0, 0.0
    try:
        move(s, *well_centre(wells[0], home), a.feed)
        time.sleep(0.5)
        set_colour(cam, a.full)

        print(f"\n{wells[0]}:")
        if a.focus_once:
            tf = time.time()
            print("  focusing once at the well centre...")
            z, ratio = autofocus(s, cam, z_fixed, zmin, zmax)
            if z is None:
                print(f"  WARNING: nothing sharp to focus on (contrast {ratio:.2f}); "
                      f"using --z {a.z:.2f}")
            else:
                z_fixed = z
                print(f"  focus at Z {z - hz:.3f} mm (contrast {ratio:.1f})")
            move_z(s, z_fixed)
            t_focus = time.time() - tf
        first_offset = None
        if surf:
            first_offset = check_offset(s, cam, surf, wells[0], home, 0.0, True, a, zmin, zmax,
                                        mp["points"])
            c0 = np.array(well_centre(wells[0], home)) - [hx, hy]
            if first_offset is None and abs(surf(*c0) - a.z) > 0.3:
                print("  not found near the map height, trying from --z ...")
                first_offset = check_offset(s, cam, surf, wells[0], home, a.z - surf(*c0),
                                            True, a, zmin, zmax, mp["points"])
            if first_offset is None:
                print("  WARNING: couldn't focus at the first well; using the map as it is.\n"
                      "  If the photos are blurry, find focus in the live tool and pass --z.")
            offset = first_offset or 0.0
            move_z(s, hz + surf(*c0) + offset)
        corr = (offset, 0.0, 0.0)

        calib = calibrate(s, cam, size) if a.calibrate else load_calib()
        if calib and calib.get("slack_mm"):
            XY_OVERSHOOT[0] = float(min(1.5, max(0.3, 2 * max(calib["slack_mm"].values()))))
            print(f"Belt slack X {calib['slack_mm'].get('X', 0):.2f} mm, "
                  f"Y {calib['slack_mm'].get('Y', 0):.2f} mm -> overshoot {XY_OVERSHOOT[0]:.2f} mm")
        if not calib:
            print("WARNING: no calibration.json, photos will be taken but not stitched.")
        fx, fy = field_of_view(calib, size)
        fx, fy = a.fov_x or fx, a.fov_y or fy
        sx, sy = fx * (1 - a.overlap), fy * (1 - a.overlap)
        tiles = tiles_for_well(sx, sy, a.radius)
        info = {"script": os.path.basename(sys.argv[0]), "made": time.strftime("%Y-%m-%d %H:%M:%S"),
                "wells": wells, "radius_mm": a.radius, "rotate": a.rotate,
                "size": list(size), "field_of_view_mm": [fx, fy],
                "well_offsets_mm": WELL_OFFSETS,
                "well_centres_machine_mm": {w: list(well_centre(w, home)) for w in wells}}
        with open(os.path.join(folder, "scan_info.json"), "w") as f:
            json.dump(info, f, indent=1)
        total = len(tiles) * len(wells)
        print(f"Field of view {fx:.2f} x {fy:.2f} mm (X x Y), step {sx:.2f} x {sy:.2f} mm, "
              f"{len(tiles)} photos per well, {total} in total.")

        t0 = time.time()
        fcsv = open(os.path.join(folder, "focus.csv"), "w")
        fcsv.write("well,what,dx_mm,dy_mm,z_mm_from_home,note\n")
        with open(os.path.join(folder, "positions.csv"), "w") as csv:
            csv.write("file,well,machine_x_mm,machine_y_mm,machine_z_mm\n")
            for wi, w in enumerate(wells):
                tw = time.time()
                cx, cy = well_centre(w, home)
                if surf and wi > 0:                # the tilt is the whole plate's: carry it over
                    pcx, pcy = well_centre(wells[wi - 1], home)
                    corr = (corr[0] + corr[1] * (cx - pcx) + corr[2] * (cy - pcy),
                            corr[1], corr[2])
                if wi > 0:
                    print(f"\n{w}:")
                if surf:
                    corr = check_plane(s, cam, surf, w, home, corr, a, zmin, zmax, fcsv,
                                       mp["points"])
                    a0, bt, ct = corr
                    fcsv.write(f"{w},correction,0,0,,height {1000 * a0:+.0f} um tilt "
                               f"{1000 * bt:+.1f}/{1000 * ct:+.1f} um/mm\n")
                    base = lambda dx, dy, cx=cx, cy=cy, a0=a0, bt=bt, ct=ct: (
                        hz + surf(cx + dx - hx, cy + dy - hy) + a0 + bt * dx + ct * dy)
                else:
                    base = lambda dx, dy, zf=z_fixed: zf
                zf = FocusMap(base)
                done = []                          # (i, dx, dy, x, y, z, name, dark, q)
                for i, (dx, dy) in enumerate(tiles):
                    x, y = cx + dx, cy + dy
                    move(s, x, y, a.feed)
                    z = zf(dx, dy)
                    if surf and a.af_every > 0 and i % a.af_every == 0:
                        zl = fine_focus(s, cam, z, zmin, zmax)
                        if zl is not None:
                            r = zf.add(dx, dy, zl)
                            z = zl
                            fcsv.write(f"{w},refocus,{dx:.2f},{dy:.2f},{zl - hz:.4f},"
                                       f"{1000 * r:+.0f} um from map\n")
                    move_z(s, z)
                    time.sleep(a.settle)
                    img = grab(cam)
                    name = f"{w}_{i:04d}.jpg"
                    writer.put(os.path.join(folder, name), img)
                    dark, q = tile_quality(img)
                    done.append((i, dx, dy, x, y, z, name, dark, q))
                    csv.write(f"{name},{w},{x:.3f},{y:.3f},{z:.4f}\n")
                    csv.flush()
                    taken += 1
                    if taken % 20 == 0:
                        el = time.time() - t0
                        print(f"  {taken}/{total}  {el / 60:4.1f} min, "
                              f"~{el / taken * (total - taken) / 60:.1f} min left", flush=True)

                # ---- blur check: re-shoot only the photos that came out soft
                cont = [d for d in done if d[7] >= CONTENT_MIN
                        and inside_fraction(d[1], d[2], fx, fy) >= 0.4]   # mostly in the well
                if surf and not a.no_recheck and len(cont) >= 4:
                    med = float(np.median([d[8] for d in cont]))
                    soft = sorted([d for d in cont if d[8] < BLUR_RATIO * med], key=lambda d: d[8])
                    cap = max(3, int(a.reshoot_max * len(tiles)))
                    fixed = 0
                    if soft:
                        print(f"  {len(soft)} photo(s) look soft, re-shooting "
                              f"{min(len(soft), cap)}...")
                    for i, dx, dy, x, y, z, name, dark, q in soft[:cap]:
                        move(s, x, y, a.feed)
                        zl = fine_focus(s, cam, z, zmin, zmax)
                        if zl is None or abs(zl - z) < 0.02:
                            continue                   # it was in focus; just faint cells
                        move_z(s, zl)
                        time.sleep(a.settle)
                        img = grab(cam)
                        writer.put(os.path.join(folder, name), img)
                        _, q2 = tile_quality(img)
                        fixed += 1
                        fcsv.write(f"{w},reshoot,{dx:.2f},{dy:.2f},{zl - hz:.4f},"
                                   f"{name} was {1000 * (z - zl):+.0f} um off, sharpness "
                                   f"{q / med:.2f}->{q2 / med:.2f}\n")
                    if soft:
                        print(f"  re-shot {fixed} (the rest were already as sharp as they get)")
                elif surf and not a.no_recheck:
                    print("  blur check skipped (too few photos with cells in them)")
                fcsv.flush()
                print(f"  {w} done in {(time.time() - tw) / 60:.1f} min")
        fcsv.close()
        t_photos = time.time() - t0
        print(f"\nScan done: {taken} photos in {t_photos / 60:.1f} min")
        send(s, f"G53 G0 X{hx:.3f} Y{hy:.3f}")
        position(s)
    except KeyboardInterrupt:
        print("\nStopped. Halting the stage...")
        s.write(b"!"); time.sleep(0.3); s.write(b"\x18"); time.sleep(1)
    finally:
        writer.close()
        cam.stop()
        cam.close()
        s.close()

    calib = load_calib()
    if os.path.exists(CALIB_FILE):
        shutil.copy2(CALIB_FILE, folder)
    if taken and calib and a.stitch:
        ts = time.time()
        stitch_folder(folder, calib, align=not a.no_align, rotate=a.rotate)
        t_stitch = time.time() - ts
    print("\nTime taken:")
    print(f"  homing + camera start  {t_home / 60:5.1f} min")
    if a.focus_once:
        print(f"  focusing (once)        {t_focus / 60:5.1f} min")
    print(f"  photos                 {t_photos / 60:5.1f} min   ({taken} photos, "
          f"{t_photos / max(taken, 1):.2f} s each)")
    if a.stitch:
        print(f"  stitching              {t_stitch / 60:5.1f} min")
    print(f"  TOTAL                  {(time.time() - T0) / 60:5.1f} min")
    print(f"\nEverything is in {folder}")
    print("Copy to the laptop (in Windows PowerShell):")
    ip = (os.popen("hostname -I").read().split() or ["<Pi IP>"])[0]
    print(f'  scp -r geofreyjohn@{ip}:~/scans/{os.path.basename(folder)} '
          f'"C:\\My Documents\\Incubot\\scans"')
    if not a.stitch:
        print("Then stitch it on the laptop:")
        print(f'  python "C:\\My Documents\\Incubot\\2026-11-19-pi-scripts\\stitch_scan.py" '
              f'"C:\\My Documents\\Incubot\\scans\\{os.path.basename(folder)}"')


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--make-map", action="store_true",
                   help="focus 19 spots per well and save the plate shape (do once)")
    p.add_argument("--wells", default=",".join(ORDER), help="e.g. FR or BL,BR")
    p.add_argument("--fov-x", type=float, default=None,
                   help="mm, field of view along X (default: from calibration)")
    p.add_argument("--fov-y", type=float, default=None,
                   help="mm, field of view along Y (default: from calibration)")
    p.add_argument("--overlap", type=float, default=0.10, help="0.10 = 10%%")
    p.add_argument("--settle", type=float, default=0.3, help="s wait before each photo")
    p.add_argument("--feed", type=float, default=3000, help="mm/min between tiles")
    p.add_argument("--full", action="store_true", help="3280x2464 photos (slower, 4x bigger)")
    p.add_argument("--calibrate", action="store_true",
                   help="measure the camera at the first well before scanning")
    p.add_argument("--stitch-only", metavar="FOLDER", help="just stitch an earlier scan")
    p.add_argument("--no-align", action="store_true",
                   help="stitch by stage positions only, no image matching")
    p.add_argument("--radius", type=float, default=None,
                   help="mm around each well centre to photograph (default 19, or 18 once "
                        "well_offsets.json exists; the well itself is 17.4)")
    p.add_argument("--stitch", action="store_true", help="also stitch on the Pi (slow)")
    p.add_argument("--check-points", type=int, default=5, choices=(5, 7),
                   help="focus check points per well (default 5)")
    p.add_argument("--reshoot-max", type=float, default=RESHOOT_MAX,
                   help="largest fraction of a well that may be re-shot (default 0.10)")
    p.add_argument("--z", type=float, default=None,
                   help="rough focus height, mm up from home (default: from the focus map)")
    p.add_argument("--rotate", type=int, default=OUTPUT_ROTATE, choices=(0, 90, 180, 270),
                   help="turn the stitched pictures (default 180: camera is turned 180)")
    p.add_argument("--no-af", action="store_true", help="no focusing, scan everything at --z")
    p.add_argument("--focus-once", action="store_true",
                   help="one autofocus at the first well centre, every photo at that height")
    p.add_argument("--af-every", type=int, default=0,
                   help="also re-focus every N photos (0 = never, default)")
    p.add_argument("--no-recheck", action="store_true", help="skip the blur check and re-shoots")
    a = p.parse_args()
    if os.path.exists(OFFSETS_FILE):
        try:
            with open(OFFSETS_FILE) as f:
                offs = json.load(f)
            for w, (ox, oy) in offs.items():
                if w in WELLS and abs(ox) < 4 and abs(oy) < 4:
                    WELLS[w] = (WELLS[w][0] + ox, WELLS[w][1] + oy)
                    WELL_OFFSETS[w] = [ox, oy]
            print(f"Well centres corrected from {OFFSETS_FILE}")
        except Exception as e:
            print(f"(couldn't read {OFFSETS_FILE}: {e})")
    if a.radius is None:
        a.radius = 18.0 if WELL_OFFSETS else 19.0

    if a.stitch_only:
        calib = load_calib()
        if not calib:
            sys.exit("No calibration.json yet. Run with --calibrate first.")
        stitch_folder(os.path.expanduser(a.stitch_only), calib, align=not a.no_align,
                      rotate=a.rotate)
        return

    wells = [w.strip().upper() for w in a.wells.split(",") if w.strip()]
    bad = [w for w in wells if w not in WELLS]
    if bad:
        sys.exit(f"Unknown well {bad[0]}. Use: {', '.join(WELLS)}")
    size = (3280, 2464) if a.full else SIZE
    if a.z is None:                        # start height: from the focus map if there is one
        m = load_map()
        zs = [p[3] for p in (m or {}).get("points", []) if p[0] == wells[0]] or \
             [p[3] for p in (m or {}).get("points", [])]
        a.z = float(np.median(zs)) if zs else Z_DEFAULT
        print(f"Start height {a.z:.2f} mm" + (" (from the focus map)" if zs else " (default)"))
    if a.make_map:
        make_map(a, wells, size)
    else:
        scan(a, wells, size)


if __name__ == "__main__":
    main()
