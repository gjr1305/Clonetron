"""
Clonetron hardware: the stage (GRBL) and the camera.

open_hardware(cfg, sim=False) returns (stage, camera, flat).
  stage:  do_home(), jog(axis, mm), goto(x, y, z), z_up_to(z), pos(), stop(), close()
          positions are mm from home; X is negative towards the wells (left).
  camera: capture_array("main") -> RGB image, stop()
  flat:   apply(img) -> flat-field corrected image

With sim=True nothing is opened: a simulated stage and a camera that draws a
fake stained plate (in focus only near the right height) are used instead,
so the app can be tried on any laptop.
"""
import math
import threading
import time

import cv2
import numpy as np

PREVIEW = (820, 616)        # size of the live picture sent to the browser
MAIN_SIZE = (1640, 1232)    # camera frame size
FOV_MM = (3.22, 2.41)       # what one camera frame covers on the plate


def sharpness_of(img):
    """Edge detail in the middle of the picture (same measure as the autofocus)."""
    g = img[..., 1]
    h, w = g.shape[:2]
    g = g[h // 5: 4 * h // 5, w // 5: 4 * w // 5]
    g = cv2.resize(g, (384, int(384 * g.shape[0] / g.shape[1])), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(g.astype(np.float32), cv2.CV_32F).var())


# ---------------------------------------------------------------- real hardware
def _open_real(cfg):
    import live_focus as lf                       # needs picamera2 + pyserial (Pi only)
    lf.Z_LIMIT = float(cfg["z_limit_mm"])
    lf.BACKLASH_Z = float(cfg["z_backlash_mm"])

    class RealStage(lf.Grbl):
        def stop(self):
            self.s.write(b"\x85")                 # GRBL jog cancel: stops a jog at once

        def unlock(self):
            """Clear GRBL's alarm without homing. Some alarms (a failed homing, a
            hit limit switch) need a soft reset first, so always reset, then $X."""
            with self.lock:
                self.s.write(b"\x18")              # soft reset (Ctrl+X); keeps settings
                time.sleep(1.5)
                self.s.reset_input_buffer()
                self._send("$X")
                st, _ = self._status(False)
                print(f"unlock: GRBL now {st}", flush=True)
                if st.startswith("Alarm"):
                    raise RuntimeError("GRBL is still locked. Is an endstop switch pressed right now? "
                                       "Move that axis off its switch by hand (12 V off), then try again")

    try:
        print("Connecting to GRBL on", cfg["serial_port"])
        stage = RealStage(cfg["serial_port"])
    except Exception as e:
        print("Arduino/GRBL not found:", e)
        stage = MissingStage("Arduino not found: check its USB cable, then restart the app")

    try:
        print("Starting camera...")
        cam = lf.open_camera()
        cam.configure(cam.create_video_configuration(main={"size": MAIN_SIZE, "format": "RGB888"},
                                                     controls={"FrameRate": 15}))
        cam.start()
        s = lf.load_camera_settings()
        if s:
            lf.apply_camera_settings(cam, s)
            print(f"Colour calibration loaded (exposure {s['ExposureTime'] / 1000:.2f} ms)")
        else:
            print("No colour calibration yet: run the flat-field calibration with the plate out.")
    except Exception as e:
        print("Camera not found:", e)
        cam = MissingCamera("Camera not found", "Power off, reseat the camera ribbon at both ends, power on")
    return stage, cam, lf.FlatField()


class MissingStage:
    """Stands in for the Arduino when it can't be opened, so the app still starts
    and can say what's wrong instead of crashing."""
    def __init__(self, why):
        self.why = why

    def pos(self):
        return "Error: " + self.why, (0.0, 0.0, 0.0)

    def _fail(self, *a, **k):
        raise RuntimeError(self.why)

    do_home = jog = goto = z_up_to = unlock = _fail

    def stop(self):
        pass

    def close(self):
        pass


class MissingCamera:
    def __init__(self, title, hint):
        self.title, self.hint = title, hint

    def capture_array(self, name="main"):
        time.sleep(0.5)
        return cv2.resize(placeholder(self.title, self.hint), MAIN_SIZE)

    def stop(self):
        pass


# ---------------------------------------------------------------- simulator
class SimStage:
    SPEED = {"X": 50.0, "Y": 50.0, "Z": 3.0}       # mm/s, roughly like the real machine

    def __init__(self, z_limit):
        self.z_limit = z_limit
        self.p = [0.0, 0.0, 0.0]
        self.state = "Alarm"                       # like GRBL before homing
        self.lock = threading.Lock()
        self._cancel = threading.Event()

    def _move_to(self, target):
        dist = max(abs(t - c) for t, c in zip(target, self.p))
        t_need = min(2.0, dist / 40.0 + 0.05)
        self.state = "Jog"
        start, t0 = list(self.p), time.time()
        while time.time() - t0 < t_need:
            if self._cancel.is_set():
                break
            f = (time.time() - t0) / t_need
            self.p = [s + (t - s) * f for s, t in zip(start, target)]
            time.sleep(0.02)
        else:
            self.p = list(target)
        self._cancel.clear()
        self.state = "Idle"

    def do_home(self):
        with self.lock:
            self._move_to([0.0, 0.0, 0.0])

    def jog(self, axis, dist):
        if self.state == "Alarm":
            raise RuntimeError("not homed yet: press Home first")
        i = "XYZ".index(axis)
        with self.lock:
            t = list(self.p)
            t[i] += dist
            self._move_to(t)

    def goto(self, x=None, y=None, z=None):
        with self.lock:
            t = list(self.p)
            if x is not None and y is not None:
                t[0], t[1] = x, y
            if z is not None:
                t[2] = min(z, self.z_limit)
            self._move_to(t)

    def z_up_to(self, z):
        self.goto(z=z)

    def pos(self):
        return self.state, tuple(self.p)

    def stop(self):
        self._cancel.set()

    def unlock(self):
        if self.state == "Alarm":
            self.state = "Idle"

    def close(self):
        self.state = "Alarm"      # like GRBL resetting when the serial port is reopened


class SimCamera:
    """Draws a fake crystal-violet plate: colonies of small dark cells inside
    six wells, blurred the further the stage is from the focus height."""

    FOCUS_Z = 19.6

    def __init__(self, stage, cfg):
        self.stage = stage
        self.r_well = float(cfg["well_radius_mm"])
        rng = np.random.default_rng(1)
        self.wells = [(-x, y) for x, y in cfg["wells_mm"].values()]
        cols = []
        for wx, wy in self.wells:
            for _ in range(150):
                a, r = rng.uniform(0, 2 * math.pi), self.r_well * math.sqrt(rng.uniform(0, 0.95))
                n = int(rng.choice([8, 30, 120, 400]))
                cols.append((wx + r * math.cos(a), wy + r * math.sin(a), n, rng.integers(1 << 30)))
        self.colonies = cols

    def focus_at(self, x, y):
        return self.FOCUS_Z + 0.02 * (x + 105) / 40 + 0.015 * (y - 62) / 20   # slight plate tilt

    def capture_array(self, name="main"):
        _, (x, y, z) = self.stage.pos()
        w, h = PREVIEW
        px = w / FOV_MM[0]                                   # pixels per mm in this picture
        img = np.full((h, w, 3), 232, np.uint8)
        x0, y0 = x - FOV_MM[0] / 2, y - FOV_MM[1] / 2
        for wx, wy in self.wells:                            # well edge
            d = math.hypot(x - wx, y - wy)
            if abs(d - self.r_well) < 3:
                cv2.circle(img, (int((wx - x0) * px), int((wy - y0) * px)),
                           int(self.r_well * px), (160, 150, 160), int(0.3 * px))
        dot = max(1, int(0.006 * px))
        for gi in range(math.floor(x0), math.floor(x0 + FOV_MM[0]) + 1):     # scattered single cells
            for gj in range(math.floor(y0), math.floor(y0 + FOV_MM[1]) + 1):
                r = np.random.default_rng((gi * 7919 + gj * 104729) & 0x7FFFFFFF)
                for ux, uy in r.uniform(0, 1, (25, 2)):
                    cx, cy = gi + ux, gj + uy
                    if any(math.hypot(cx - wx, cy - wy) < self.r_well for wx, wy in self.wells):
                        cv2.circle(img, (int((cx - x0) * px), int((cy - y0) * px)), dot, (120, 40, 110), -1)
        for cx, cy, n, seed in self.colonies:
            spread = 0.02 * math.sqrt(n) + 0.03
            if abs(cx - x) > FOV_MM[0] / 2 + spread * 3 or abs(cy - y) > FOV_MM[1] / 2 + spread * 3:
                continue
            r = np.random.default_rng(int(seed))
            pts = r.normal(0, spread, (n, 2))
            for dx, dy in pts:
                cv2.circle(img, (int((cx + dx - x0) * px), int((cy + dy - y0) * px)),
                           dot, (120, 40, 110), -1)
        blur = abs(z - self.focus_at(x, y)) * 60
        if blur > 0.3:
            img = cv2.GaussianBlur(img, (0, 0), min(blur, 25))
        noise = np.random.default_rng().normal(0, 3, img.shape)
        img = np.clip(img + noise, 0, 255).astype(np.uint8)
        time.sleep(0.03)
        return img

    def stop(self):
        pass


def autofocus(stage, cam, cam_lock, z_start, z_limit):
    """Coarse then fine Z sweep around z_start (mm from home), same method as the
    scan script: Z only ever moves up during a sweep. Returns (best z, contrast)."""
    def score_at(z, first):
        if first:
            stage.goto(z=z)                 # dip below and come up
        else:
            stage.z_up_to(z)
        time.sleep(0.1)
        with cam_lock:
            cam.capture_array("main")       # throw away a frame taken while moving
            img = cam.capture_array("main")
        return sharpness_of(cv2.resize(img, PREVIEW, interpolation=cv2.INTER_AREA))

    def sweep(centre, half, step):
        zs = np.arange(centre - half, centre + half + step / 2, step)
        zs = zs[(zs <= z_limit) & (zs >= 0.5)]
        return zs, np.array([score_at(float(z), k == 0) for k, z in enumerate(zs)])

    def peak(zs, v):
        i = int(np.argmax(v))
        if 0 < i < len(v) - 1:
            d = v[i - 1] - 2 * v[i] + v[i + 1]
            if d < 0:
                return zs[i] + 0.5 * (v[i - 1] - v[i + 1]) / d * (zs[1] - zs[0]), i
        return zs[i], i

    centre = z_start
    for _ in range(3):
        zs, v = sweep(centre, 0.30, 0.05)
        best, i = peak(zs, v)
        if 0 < i < len(zs) - 1:
            break
        centre = zs[i]
    ratio = float(v.max() / max(np.median(v), 1e-6))
    zs, v = sweep(best, 0.06, 0.015)
    best, _ = peak(zs, v)
    stage.goto(z=float(best))
    return float(best), ratio


def placeholder(text, sub=""):
    """Grey picture with a message, shown while a job has the camera."""
    w, h = PREVIEW
    img = np.full((h, w, 3), 28, np.uint8)
    for t, y, sc, col in ((text, h // 2 - 10, 1.0, (250, 139, 167)), (sub, h // 2 + 30, 0.6, (200, 200, 200))):
        if t:
            (tw, _), _ = cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, sc, 2)
            cv2.putText(img, t, ((w - tw) // 2, y), cv2.FONT_HERSHEY_SIMPLEX, sc, col, 2, cv2.LINE_AA)
    return img


def close_hardware(stage, cam):
    for f in (getattr(cam, "stop", None), getattr(cam, "close", None), stage.close):
        try:
            if f:
                f()
        except Exception as e:
            print("(while closing:", e, ")")


class NoFlat:
    flat = None

    def apply(self, img):
        return img


def open_hardware(cfg, sim=False):
    if sim:
        stage = SimStage(float(cfg["z_limit_mm"]))
        return stage, SimCamera(stage, cfg), NoFlat()
    return _open_real(cfg)
