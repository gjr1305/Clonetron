#!/usr/bin/env python3
"""
ColonyScope flat-field calibration (fixes the pink corners in software).

Before running:
  * stop the live tool / any other camera program
  * take the plate OUT, so the objective looks straight at the light pad
  * light pad ON, at the brightness you will image at

Run:   python3 flatfield_calibrate.py
Makes: ~/scans/camera.json   locked exposure + white balance
       ~/scans/flat.npy      flat-field image (what "blank" looks like)
       ~/colourtest/E_raw.jpg and F_corrected.jpg   before / after check
Run it again whenever the light pad brightness or the optics change.
"""
import json
import os
import time

import cv2
import numpy as np
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


SIZE = (1640, 1232)
TEST_DIR = os.path.expanduser("~/colourtest")


def centre_mean(img):
    h, w = img.shape[:2]
    return img[h // 3: 2 * h // 3, w // 3: 2 * w // 3].reshape(-1, 3).astype(np.float64).mean(0)


def corner_mean(img):
    h, w = img.shape[:2]
    return np.concatenate([img[:h // 6, :w // 6].reshape(-1, 3), img[-h // 6:, -w // 6:].reshape(-1, 3),
                           img[:h // 6, -w // 6:].reshape(-1, 3), img[-h // 6:, :w // 6].reshape(-1, 3)]
                          ).astype(np.float64).mean(0)


def report(name, img):
    c, k = centre_mean(img), corner_mean(img)
    print(f"  {name:10s} centre R{c[2]:5.0f} G{c[1]:5.0f} B{c[0]:5.0f}   "
          f"corners R{k[2]:5.0f} G{k[1]:5.0f} B{k[0]:5.0f}")


def grab(cam, n=1):
    frames = [cam.capture_array("main").astype(np.float32) for _ in range(n)]
    return np.mean(frames, axis=0)


def set_manual(cam, exp, gain, rg, bg):
    cam.set_controls({"AeEnable": False, "AwbEnable": False, "ExposureTime": int(exp),
                      "AnalogueGain": float(gain), "ColourGains": (float(rg), float(bg))})
    time.sleep(0.8)
    cam.capture_array("main")          # throw away one frame so the new settings apply


def fit_exposure(cam, exp, gain, rg, bg, target=200.0):
    """Adjust exposure until the centre is about `target` and nothing clips."""
    for _ in range(10):
        set_manual(cam, exp, gain, rg, bg)
        img = grab(cam)
        c = centre_mean(img).max()
        top = np.percentile(img.max(axis=2), 99.5)
        if top >= 252:
            exp *= 0.5
        elif abs(c - target) > 6:
            exp *= target / max(c, 1.0)
        else:
            break
        if exp > 60000:                      # longer than this gets slow: use gain
            gain, exp = min(8.0, gain * exp / 60000), 60000
        exp = max(exp, 30)
    return exp, gain, img


def main():
    print("Plate OUT, light pad ON (scan brightness), nothing else using the camera.")
    print("Starting in 3 s (Ctrl+C to cancel)...")
    time.sleep(3)
    os.makedirs(SCANS_DIR, exist_ok=True)
    os.makedirs(TEST_DIR, exist_ok=True)

    cam = open_camera()
    cam.configure(cam.create_still_configuration(main={"size": SIZE, "format": "RGB888"}))
    cam.start()
    time.sleep(2.5)
    md = cam.capture_metadata()
    exp = md["ExposureTime"] * md["AnalogueGain"]      # move all brightness into exposure
    gain = 1.0
    rg, bg = md["ColourGains"]
    try:
        print("1/4 Exposure...")
        exp, gain, img = fit_exposure(cam, exp, gain, rg, bg)
        print(f"    exposure {exp / 1000:.2f} ms, gain {gain:.2f}")
        report("before WB", img)

        print("2/4 White balance (centre made neutral)...")
        for _ in range(4):
            b, g, r = centre_mean(img)
            rg, bg = rg * g / max(r, 1), bg * g / max(b, 1)
            rg, bg = min(max(rg, 0.3), 8.0), min(max(bg, 0.3), 8.0)
            set_manual(cam, exp, gain, rg, bg)
            img = grab(cam)
        exp, gain, img = fit_exposure(cam, exp, gain, rg, bg)
        print(f"    colour gains red {rg:.2f}, blue {bg:.2f}")

        print("3/4 Recording the flat field (8 frames)...")
        flat = cv2.GaussianBlur(grab(cam, 8), (0, 0), 2)
        if flat.min() < 15:
            print("    WARNING: some part of the image is almost black. Is the plate out and")
            print("    the light pad centred over the objective? Saving anyway.")
        test = cam.capture_array("main")
    finally:
        cam.stop()
        cam.close()

    np.save(FLAT_NPY, flat.astype(np.float32))
    cv2.imwrite(os.path.join(SCANS_DIR, "flat_preview.png"), np.clip(flat, 0, 255).astype(np.uint8))
    settings = {"ExposureTime": int(exp), "AnalogueGain": round(float(gain), 3),
                "ColourGains": [round(float(rg), 4), round(float(bg), 4)],
                "size": list(SIZE), "bg_level": BG_LEVEL,
                "made": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "neutral tuning (lens shading off) + software flat-field"}
    with open(CAMERA_FILE, "w") as f:
        json.dump(settings, f, indent=1)

    print("4/4 Check:")
    corrected = FlatField().apply(test)
    report("raw", test)
    report("corrected", corrected)
    cv2.imwrite(os.path.join(TEST_DIR, "E_raw.jpg"), test, [cv2.IMWRITE_JPEG_QUALITY, 92])
    cv2.imwrite(os.path.join(TEST_DIR, "F_corrected.jpg"), corrected, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"\nSaved {CAMERA_FILE} and {FLAT_NPY}")
    print(f"Check photos: {TEST_DIR}/E_raw.jpg and F_corrected.jpg")
    print("Run this again whenever you change the light pad brightness or touch the optics.")


if __name__ == "__main__":
    main()
