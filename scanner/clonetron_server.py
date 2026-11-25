#!/usr/bin/env python3
"""
Clonetron scanner app: control the scanner from a browser on any laptop.

First time on the Pi, choose the password:
    python3 clonetron_server.py --set-password
Then start it:
    python3 clonetron_server.py
and open  http://<IP shown on the LCD>:8000  in a browser on the lab network.

Try it without the scanner (any computer with Python, numpy and opencv):
    python clonetron_server.py --sim
then open http://127.0.0.1:8000 . The simulator asks you to set a password
on first run too.

Uses only Python's standard library plus numpy and OpenCV, which the Pi
already has. Steps 2a-2b: login, live view, jog, home, stop, go to well, autofocus,
flat-field calibration and focus map.
"""
import argparse
import getpass
import hashlib
import hmac
import json
import re
import os
import secrets
import shutil
import signal
import socketserver
import subprocess
import textwrap
import sys
import threading
import time
from http import server
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import hw      # noqa: E402
import lcd     # noqa: E402
import buttons # noqa: E402
import usb     # noqa: E402

WEB = os.path.join(HERE, "web")
AUTH_FILE = os.path.expanduser("~/.clonetron_auth.json")
SESSION_HOURS = 12
GRBL_ERRORS = {   # the GRBL messages people actually meet, in plain words
    "error:8": "the stage isn't ready: press Home first",
    "error:9": "the stage is locked (alarm): press Home first",
    "error:15": "that move would go past the end of the stage",
    "ALARM:1": "a limit switch was hit: press Home",
    "ALARM:2": "that move would go past the end of the stage: press Home",
    "ALARM:8": "homing didn't find the switch: check the endstop wiring, then press Home",
    "ALARM:9": "homing didn't find the switch: check the endstop wiring, then press Home",
}


def plain(err):
    for code, text in GRBL_ERRORS.items():
        if code in err:
            return f"{text} (GRBL {code})"
    return err


TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


# ---------------------------------------------------------------- password
def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def set_password():
    print("Choose the Clonetron password (anyone on the lab network who knows it can")
    print("move the stage, so don't reuse an important password).")
    while True:
        a = getpass.getpass("New password: ")
        if len(a) < 6:
            print("Use at least 6 characters.")
            continue
        if a != getpass.getpass("Same again: "):
            print("They didn't match, try again.")
            continue
        break
    salt = secrets.token_hex(16)
    with open(AUTH_FILE, "w") as f:
        json.dump({"salt": salt, "hash": _hash(a, salt)}, f)
    os.chmod(AUTH_FILE, 0o600)
    print("Password saved.")


def check_password(pw):
    try:
        with open(AUTH_FILE) as f:
            d = json.load(f)
    except Exception:
        return False
    return hmac.compare_digest(_hash(pw, d["salt"]), d["hash"])


# ---------------------------------------------------------------- shared state
MAP_FILE = os.path.join(config.SCANS_DIR, "focus_map.json")
CAMERA_FILE = os.path.join(config.SCANS_DIR, "camera.json")
FLAT_FILE = os.path.join(config.SCANS_DIR, "flat.npy")
OFFSETS_FILE = os.path.join(config.SCANS_DIR, "well_offsets.json")
PHOTOS_PER_WELL = 213          # 35 mm well, 3.2 x 2.4 mm photos, 10% overlap
MB_PER_PHOTO = 1.3             # JPEG at quality 95 is ~0.8-1.1 MB; extra for re-shoots
MIN_PER_WELL = 4.0             # rough time per well with the focus map and blur check
PROGRESS_RE = re.compile(r"^\s*(\d+)/(\d+)\s+([\d.]+) min, ~([\d.]+) min left")


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


class App:
    def __init__(self, cfg, sim):
        self.cfg, self.sim = cfg, sim
        self.cam_lock = threading.Lock()
        self.busy = threading.Lock()              # one stage action (or job) at a time
        self.action = ""                          # what the stage is doing, for the page
        self.sharp = 0.0
        self.frame = None
        self.frame_cond = threading.Condition()
        self.sessions = {}                        # token -> expiry time
        self.stop_evt = threading.Event()
        self.fail = {}                            # ip -> (count, last time): slow down guessing
        self._last_pos = (0.0, 0.0, 0.0)
        self.job = None                           # {"kind", "status", "log", "proc", ...}
        self.stage, self.cam, self.flat = hw.open_hardware(cfg, sim)
        if sim:
            os.makedirs(usb.SIM_ROOT, exist_ok=True)       # a pretend USB stick
        self._latest = (None, 0.0)                         # newest scan photo shown in the preview
        self.viewers = 0                                   # browsers showing the live picture
        self.port = int(cfg.get("web_port", 8000))
        self.lcd = lcd.Lcd(enabled=not sim)
        self.lcd_lines = []                                # what the LCD shows, also sent to the page
        self.lcd_lock = threading.Lock()
        self.menu_stack = []                               # LCD menus, innermost last
        self.confirm = None                                # {"title", "question", "yes"}
        self.flash = None                                  # (lines, show until)
        self.menu_t = 0.0                                  # last button press
        self.buttons = buttons.Buttons(self.on_button, enabled=not sim)
        threading.Thread(target=self._preview_loop, daemon=True).start()
        threading.Thread(target=self._lcd_loop, daemon=True).start()

    # -- live picture
    def _preview_loop(self):
        while not self.stop_evt.is_set():
            t = time.time()
            if self.viewers == 0:                 # nobody is watching: don't spend the Pi's CPU
                self.sharp = 0.0
                time.sleep(0.3)
                continue
            try:
                if self.cam is None:              # a job has the camera
                    j = self.job or {}
                    small = self._latest_photo(j)
                    if small is None:
                        small = hw.placeholder(j.get("title", "Busy"), (j.get("log") or [""])[-1][:70])
                    self.sharp = 0.0
                    time.sleep(0.5)
                else:
                    with self.cam_lock:
                        img = self.cam.capture_array("main")
                    small = cv2.resize(img, hw.PREVIEW, interpolation=cv2.INTER_AREA)
                    self.sharp = hw.sharpness_of(small)
                    small = self.flat.apply(small)
                ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    with self.frame_cond:
                        self.frame = jpg.tobytes()
                        self.frame_cond.notify_all()
            except Exception as e:
                print("preview error:", e)
                time.sleep(0.5)
            time.sleep(max(0.0, 0.1 - (time.time() - t)))

    def _latest_photo(self, j):
        """During a scan, show the newest photo saved (the second newest, in case
        the newest is still being written)."""
        folder = (j.get("progress") or {}).get("folder")
        if j.get("kind") != "scan" or not folder or not os.path.isdir(folder):
            return None
        path, t = self._latest
        if time.time() - t > 1.5 or path is None:
            try:
                files = sorted((e for e in os.scandir(folder)
                                if e.name.endswith(".jpg") and "_stitched" not in e.name),
                               key=lambda e: e.stat().st_mtime)
                path = files[-2].path if len(files) > 1 else None
            except OSError:
                path = None
            self._latest = (path, time.time())
        if not path:
            return None
        img = cv2.imread(path)
        if img is None:
            return None
        img = cv2.resize(img, hw.PREVIEW, interpolation=cv2.INTER_AREA)
        p = j["progress"]
        txt = f"{os.path.basename(path)}   {p.get('taken', 0)}/{p.get('total', '?')}"
        cv2.rectangle(img, (0, 0), (hw.PREVIEW[0], 34), (20, 20, 20), -1)
        cv2.putText(img, txt, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (250, 139, 167), 2, cv2.LINE_AA)
        return img

    # -- front-panel LCD
    @staticmethod
    def ip():
        ips = os.popen("hostname -I 2>/dev/null").read().split()
        return ips[0] if ips else None

    # -- front-panel buttons and LCD menu
    MENU_TIMEOUT = 30

    def _main_menu(self):
        wells = sorted(self.cfg["wells_mm"], key=lambda w: tuple(self.cfg["wells_mm"][w]))
        return {"title": "MENU", "i": 0, "items": [
            ("Home stage", lambda: self._do("Homing...", self.home)),
            ("Go to well  >", {"title": "GO TO WELL", "i": 0, "items": [
                (w, (lambda w=w: self._do(f"Going to {w}...", lambda: self.goto_well(w))))
                for w in wells]}),
            ("Scan whole plate", lambda: self._ask("SCAN", "Scan all 6 wells?", self._button_scan)),
            ("Eject USB", lambda: self._do("Ejecting USB...", self.eject, "Safe to remove")),
            ("Show address", lambda: self._show([f"{self.ip() or 'no network'}", f"port {self.port}",
                                                 "open in a browser"], 8)),
            ("Shut down Pi", lambda: self._ask("SHUT DOWN", "Turn the Pi off?", self._shutdown)),
            ("Close menu", lambda: None),
        ]}

    def on_button(self, name):
        """A front-panel (or web page) button press."""
        print(f"button {name}", flush=True)
        self.menu_t = time.time()
        self.flash = None
        if self.confirm:
            c, self.confirm = self.confirm, None
            if name == "CENTRE":
                threading.Thread(target=c["yes"], daemon=True).start()
            self._lcd_refresh()
            return
        j = self.job or {}
        if j.get("status") in ("running", "cancelling") and not self.menu_stack:
            if name == "CENTRE" and j.get("status") == "running":
                self._ask("STOP", f"Stop {j.get('kind', 'job')}?", self.cancel_job)
            self._lcd_refresh()
            return
        if not self.menu_stack:
            if name == "CENTRE":
                self.menu_stack = [self._main_menu()]
            self._lcd_refresh()
            return
        m = self.menu_stack[-1]
        n = len(m["items"])
        if name == "UP":
            m["i"] = (m["i"] - 1) % n
        elif name == "DOWN":
            m["i"] = (m["i"] + 1) % n
        elif name == "LEFT":
            self.menu_stack.pop()
        elif name in ("CENTRE", "RIGHT"):
            label, what = m["items"][m["i"]]
            if isinstance(what, dict):
                what["i"] = 0
                self.menu_stack.append(what)
            else:
                self.menu_stack = []
                print(f"menu: selected '{label}'", flush=True)
                what()
        self._lcd_refresh()
        print("lcd: " + " | ".join(x.strip() for x in self.lcd_lines), flush=True)

    def _ask(self, title, question, yes):
        self.menu_stack = []
        self.confirm = {"title": title, "question": question, "yes": yes}

    def _show(self, lines, secs=4):
        self.flash = (lines, time.time() + secs)
        self._lcd_refresh()

    def _do(self, busy_msg, fn, ok_msg="Done"):
        """Run a stage/USB action in the background and report the result on the LCD."""
        def work():
            self._show([busy_msg], 600)
            print(f"menu action: {busy_msg}", flush=True)
            err = fn()
            print(f"menu action result: {err or 'ok'}", flush=True)
            if err:
                self._show(["Problem:"] + textwrap.wrap(str(err), 20)[:3], 8)
            else:
                self._show([ok_msg], 3)
        threading.Thread(target=work, daemon=True).start()

    def _button_scan(self):
        mode = "map" if os.path.exists(MAP_FILE) else "focus-once"
        err = self.start_job("scan", {"wells": list(self.cfg["scan_order"]), "mode": mode,
                                      "reshoot": True, "name": "plate"})
        if err:
            self._show(["Can't scan:"] + textwrap.wrap(err, 20)[:3], 8)

    def _shutdown(self):
        self._show(["Shutting down...", "Wait 30 s, then", "it's safe to unplug"], 600)
        time.sleep(1)
        for cmd in (["systemctl", "poweroff"], ["sudo", "-n", "/sbin/poweroff"]):
            if self.sim:
                print("[simulator] would run:", " ".join(cmd))
                return
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                return
        self._show(["Couldn't shut down:", "run install_service", "again, or use", "sudo poweroff"], 10)

    def _menu_text(self):
        if self.confirm:
            c = self.confirm
            return [c["title"], c["question"], "CENTRE = yes", "LEFT = no"]
        m = self.menu_stack[-1]
        items, i = m["items"], m["i"]
        top = min(max(0, i - 1), max(0, len(items) - 3))
        rows = [("> " if k == i else "  ") + items[k][0] for k in range(top, min(top + 3, len(items)))]
        return [m["title"]] + rows

    def _lcd_refresh(self):
        with self.lcd_lock:
            self.lcd_lines = self._lcd_text()
            self.lcd.show(self.lcd_lines)

    def _lcd_text(self):
        if (self.menu_stack or self.confirm) and time.time() - self.menu_t > self.MENU_TIMEOUT:
            self.menu_stack, self.confirm = [], None
        if self.flash and time.time() < self.flash[1]:
            return list(self.flash[0])
        if self.menu_stack or self.confirm:
            return self._menu_text()
        j = self.job or {}
        running = j.get("status") in ("running", "cancelling")
        if running and j.get("kind") == "scan":
            p = j.get("progress") or {}
            tot = p.get("total") or 0
            pct = f" {100 * p.get('taken', 0) // tot}%" if tot else ""
            left = p.get("left_min")
            name = j["title"].replace("Scanning ", "", 1)
            return [f"Scanning {p.get('well') or ''}".strip(),
                    f"{p.get('taken', 0)}/{tot or '?'} photos{pct}",
                    f"~{left:.0f} min left" if left is not None else "starting...",
                    name if j["status"] == "running" else "stopping..."]
        if running:
            last = (j.get("log") or [""])[-1].strip()
            return [j.get("title", "Working"), last[:20], last[20:40], ""]
        ip = self.ip()
        line2 = f"{ip}:{self.port}" if ip else "no network"
        if self.action:
            line3 = self.action[:20]
        else:
            try:
                st, _ = self.stage.pos()
            except Exception:
                st = "?"
            line3 = "Press Home" if st.startswith("Alarm") else "Ready"
            if isinstance(self.cam, hw.MissingCamera):
                line3 = "CAMERA NOT FOUND"
            elif isinstance(self.stage, hw.MissingStage):
                line3 = "ARDUINO NOT FOUND"
        stick = None
        try:
            ss = usb.sticks(self.sim)
            stick = ss[0] if ss else None
        except Exception:
            pass
        line4 = f"USB {stick['free'] / 1e9:.1f}GB free" if stick else "No USB stick"
        if j.get("kind") == "scan" and j.get("status") == "done" and time.time() - j.get("t_end", 0) < 3600:
            line3 = "Scan done: Eject" if stick else "Scan done"
        elif j.get("status") == "failed" and time.time() - j.get("t_end", 0) < 3600:
            line3 = "Last job FAILED"
        return ["Clonetron", line2, line3, line4]

    def _lcd_loop(self):
        while not self.stop_evt.is_set():
            try:
                self._lcd_refresh()
            except Exception as e:
                print("lcd error:", e)
            self.stop_evt.wait(1)

    # -- status
    def state(self):
        if self.cam is None:
            st, p = "Job running", self._last_pos
        elif self.busy.locked() and not self.sim:
            st, p = "Moving", self._last_pos      # GRBL is busy with the move; don't wait on it
        else:
            try:
                st, p = self.stage.pos()
                self._last_pos = p
            except Exception as e:
                st, p = "Error: " + str(e), self._last_pos
        x, y, z = p
        j = self.job
        return {"state": st, "x": round(x, 3), "y": round(y, 3), "z": round(z, 4),
                "sharp": round(self.sharp, 1), "busy": self.busy.locked(),
                "action": self.action, "sim": self.sim,
                "z_limit": self.cfg["z_limit_mm"], "lcd": self.lcd_lines,
                "job": None if not j else {k: j.get(k) for k in ("kind", "title", "status", "started",
                                                                   "progress")}}

    def info(self):
        mp = read_json(MAP_FILE)
        cam = read_json(CAMERA_FILE)
        offs = read_json(OFFSETS_FILE) or {}
        return {
            "wells": self.cfg["wells_mm"], "order": self.cfg["scan_order"],
            "well_radius": self.cfg["well_radius_mm"], "z_default": self.cfg["z_default_mm"],
            "map": None if not mp else {"made": mp.get("made"), "spots": len(mp.get("points", [])),
                                        "range_um": self._map_range(mp)},
            "camera": None if not cam else {"made": cam.get("made"),
                                            "exposure_ms": round(cam.get("ExposureTime", 0) / 1000, 2)},
            "flat": os.path.exists(FLAT_FILE), "offsets": sorted(offs),
        }

    @staticmethod
    def _map_range(mp):
        zz = [p[3] for p in mp.get("points", [])]
        return round(1000 * (max(zz) - min(zz))) if zz else None

    # -- stage actions
    def run(self, label, fn):
        """Do a stage action unless another one is running. Returns an error string or None."""
        if self.cam is None:
            return "a job is running (" + (self.job or {}).get("title", "") + ")"
        if not self.busy.acquire(blocking=False):
            return "the stage is busy (" + self.action + ")"
        self.action = label
        try:
            fn()
            return None
        except Exception as e:
            return plain(str(e))
        finally:
            self.action = ""
            self.busy.release()

    def _need_home(self):
        st, _ = self.stage.pos()
        if st.startswith("Alarm"):
            raise RuntimeError("the stage hasn't been homed: press Home first")

    def jog(self, axis, dist):
        if axis not in ("X", "Y", "Z") or not 0 < abs(dist) <= 50:
            return "bad jog"

        def do():
            self._need_home()
            if axis == "Z" and dist > 0:
                _, (_, _, z) = self.stage.pos()
                if z + dist > self.cfg["z_limit_mm"]:
                    raise RuntimeError(f"that would go above the Z limit ({self.cfg['z_limit_mm']} mm)")
            self.stage.jog(axis, dist)
        return self.run(f"moving {axis}", do)

    def home(self):
        return self.run("homing", self.stage.do_home)

    def stop(self):
        self.stage.stop()

    def well_target(self, w):
        """Centre of well w in mm from home (X negative), and a focus height."""
        if w not in self.cfg["wells_mm"]:
            raise RuntimeError("unknown well " + w)
        wx, wy = self.cfg["wells_mm"][w]
        off = (read_json(OFFSETS_FILE) or {}).get(w)
        if off and abs(off[0]) < 4 and abs(off[1]) < 4:
            wx, wy = wx + off[0], wy + off[1]
        x, y = -wx, wy
        z = self.cfg["z_default_mm"]
        mp = read_json(MAP_FILE)
        if mp:
            pts = [q for q in mp.get("points", []) if q[0] == w]
            if pts:
                z = min(pts, key=lambda q: (q[1] - x) ** 2 + (q[2] - y) ** 2)[3]
        return x, y, z

    def goto_well(self, w):
        def do():
            self._need_home()
            x, y, z = self.well_target(w)
            self.stage.goto(z=max(0.5, z - 2))      # come in a little low, then up to focus
            self.stage.goto(x, y)
            self.stage.goto(z=z)
        return self.run(f"going to {w}", do)

    def autofocus(self):
        result = {}

        def do():
            self._need_home()
            _, (_, _, z) = self.stage.pos()
            if z < 5:
                z = self.cfg["z_default_mm"]
            result["z"], result["contrast"] = hw.autofocus(
                self.stage, self.cam, self.cam_lock, z, self.cfg["z_limit_mm"])
            if result["contrast"] < 1.15:
                raise RuntimeError("couldn't find a clear focus here (nothing sharp nearby). "
                                   "Move to a spot with cells, get roughly into focus by eye, "
                                   "and try again")
        err = self.run("autofocusing", do)
        return err, result

    # -- jobs: the tested Pi scripts, run with the camera and stage handed over to them
    JOBS = {
        "flatfield": ("Flat-field calibration", ["flatfield_calibrate.py"]),
        "map": ("Making the focus map", ["plate_scan.py", "--make-map"]),
        "scan": ("Scanning", ["plate_scan.py"]),
    }

    def start_job(self, kind, opts):
        if kind not in self.JOBS:
            return "unknown job"
        if self.job and self.job["status"] == "running":
            return "a job is already running"
        if not self.busy.acquire(blocking=False):
            return "the stage is busy (" + self.action + ")"
        title, argv = self.JOBS[kind]
        if kind == "map":
            z = float(opts.get("z") or self.cfg["z_default_mm"])
            if not 5 <= z <= self.cfg["z_limit_mm"]:
                self.busy.release()
                return "starting focus height must be between 5 and the Z limit"
            argv = argv + ["--z", f"{z:.3f}"]
            wells = [w for w in opts.get("wells", []) if w in self.cfg["wells_mm"]]
            if wells:
                argv += ["--wells", ",".join(wells)]
        if kind == "scan":
            err, extra = self._scan_args(opts)
            if err:
                self.busy.release()
                return err
            argv = argv + extra
            title = "Scanning " + (opts.get("name") or "plate")
        if self.sim:
            argv = ["sim_job.py", kind] + (argv[1:] if kind == "scan" else [])
        self.action = title
        self.job = {"kind": kind, "title": title, "status": "running", "log": [],
                    "started": time.strftime("%H:%M:%S"), "proc": None, "code": None,
                    "progress": {"taken": 0, "total": None, "well": None, "left_min": None,
                                 "folder": None} if kind == "scan" else None}
        threading.Thread(target=self._run_job, args=(argv,), daemon=True).start()
        return None

    def _run_job(self, argv):
        j = self.job
        try:
            with self.cam_lock:                    # hand the hardware to the script
                cam, self.cam = self.cam, None
            hw.close_hardware(self.stage, cam)
            j["log"].append("$ python3 " + " ".join(argv))
            proc = subprocess.Popen([sys.executable, "-u"] + argv, cwd=HERE, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
                                    preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
            j["proc"] = proc
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    j["log"].append(line)
                    del j["log"][:-400]
                    if j.get("progress") is not None:
                        self._parse_progress(j["progress"], line)
            j["code"] = proc.wait()
            if j["status"] == "cancelling":
                j["status"] = "cancelled"
            else:
                j["status"] = "done" if j["code"] == 0 else "failed"
            j["t_end"] = time.time()
            if j.get("progress") is not None and j["status"] == "done":
                p = j["progress"]
                p["taken"], p["left_min"] = p.get("total") or p["taken"], 0
        except Exception as e:
            j["log"].append("Error: " + str(e))
            j["status"] = "failed"
        finally:
            j["proc"] = None
            j["log"].append("Reconnecting to the scanner...")
            try:
                self.stage, cam, self.flat = hw.open_hardware(self.cfg, self.sim)
                self.cam = cam
                if j["status"] == "done":          # the script homed already, so this is safe
                    j["log"].append("Homing...")
                    self.stage.do_home()
                j["log"].append("Ready.")
            except Exception as e:
                j["log"].append("Couldn't reconnect: " + str(e) + ". Restart the app.")
            self.action = ""
            self.busy.release()

    @staticmethod
    def _parse_progress(p, line):
        m = PROGRESS_RE.match(line)
        if m:
            p["taken"], p["total"] = int(m[1]), int(m[2])
            p["left_min"] = float(m[4])
        elif line.startswith("SCAN_FOLDER: "):
            p["folder"] = line[len("SCAN_FOLDER: "):].strip()
        elif re.fullmatch(r"[A-Z]{2}:", line.strip()):
            p["well"] = line.strip()[:-1]
        elif "photos per well" in line:
            m2 = re.search(r"(\d+) in total", line)
            if m2:
                p["total"] = int(m2[1])

    def estimate(self, wells):
        n = len([w for w in wells if w in self.cfg["wells_mm"]])
        return {"photos": n * PHOTOS_PER_WELL, "mb": round(n * PHOTOS_PER_WELL * MB_PER_PHOTO),
                "minutes": round(n * MIN_PER_WELL + 1)}

    def _scan_args(self, opts):
        wells = [w for w in opts.get("wells", []) if w in self.cfg["wells_mm"]]
        if not wells:
            return "tick at least one well", None
        wells = [w for w in self.cfg["scan_order"] if w in wells] + \
                [w for w in wells if w not in self.cfg["scan_order"]]
        stick = usb.get(self.sim)
        if not stick:
            return "plug in a USB stick: scans are only saved to USB", None
        if not stick["writable"]:
            return f"can't write to the USB stick {stick['label']} (is it write-protected?)", None
        need = self.estimate(wells)["mb"] * 1e6
        if stick["free"] < need:
            return (f"not enough space on {stick['label']}: needs about {need / 1e9:.1f} GB, "
                    f"has {stick['free'] / 1e9:.1f} GB free"), None
        out = os.path.join(stick["mount"], usb.SCAN_SUBFOLDER)
        os.makedirs(out, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(opts.get("name", "")).strip()).strip("-")[:40]
        args = ["--out", out, "--wells", ",".join(wells)]
        if name:
            args += ["--name", name]
        if opts.get("mode") == "focus-once":
            args.append("--focus-once")
        if opts.get("reshoot") is False:
            args.append("--no-recheck")
        return None, args

    def usb_state(self):
        st = usb.sticks(self.sim)
        stick = st[0] if st else None
        return {"stick": stick, "unmounted": usb.unmounted(self.sim),
                "recent": usb.recent_scans(stick) if stick else [],
                "sim": self.sim, "sim_ejected": self.sim and not stick}

    def eject(self):
        j = self.job
        if j and j["kind"] == "scan" and j["status"] in ("running", "cancelling"):
            return "a scan is still running: stop it first"
        st = usb.sticks(self.sim)
        if not st:
            return "no USB stick to eject"
        err = usb.eject(st[0], self.sim)
        if not err and self.sim:
            shutil.move(usb.SIM_ROOT, usb.SIM_ROOT + "-ejected")
        return err

    def sim_plug(self):
        if self.sim and os.path.isdir(usb.SIM_ROOT + "-ejected") and not os.path.isdir(usb.SIM_ROOT):
            shutil.move(usb.SIM_ROOT + "-ejected", usb.SIM_ROOT)

    def cancel_job(self):
        j = self.job
        if not j or j["status"] != "running" or not j.get("proc"):
            return "no job is running"
        j["status"] = "cancelling"
        j["proc"].send_signal(signal.SIGINT)       # the scripts stop the stage cleanly on Ctrl+C
        threading.Timer(30, lambda p=j["proc"]: p and p.poll() is None and p.kill()).start()
        return None

    def job_state(self):
        j = self.job
        if not j:
            return {"job": None}
        return {"job": {k: j.get(k) for k in ("kind", "title", "status", "started", "code",
                                              "progress")},
                "log": j["log"][-200:]}

    def close(self):
        self.stop_evt.set()
        time.sleep(0.2)
        self.lcd.close()
        if self.cam is not None:
            hw.close_hardware(self.stage, self.cam)


# ---------------------------------------------------------------- web server
def make_handler(app):
    class H(server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # -- helpers
        def _send(self, code, body, ctype="application/json", headers=()):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _file(self, name):
            path = os.path.join(WEB, name)
            if not os.path.isfile(path):
                return self._send(404, {"error": "not found"})
            with open(path, "rb") as f:
                self._send(200, f.read(), TYPES.get(os.path.splitext(name)[1], "application/octet-stream"))

        def _token(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return c["ct"].value if "ct" in c else None

        def _authed(self):
            t = self._token()
            exp = app.sessions.get(t)
            if exp and exp > time.time():
                return True
            app.sessions.pop(t, None)
            return False

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

        # -- routes
        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/login", "/login.html"):
                return self._file("login.html")
            if path in ("/style.css", "/favicon.svg"):
                return self._file(path[1:])
            if not self._authed():
                if path.startswith("/api") or path.endswith(".mjpg"):
                    return self._send(401, {"error": "please log in"})
                return self._send(302, "", "text/plain", [("Location", "/login")])
            if path == "/":
                return self._file("index.html")
            if path in ("/app.js",):
                return self._file(path[1:])
            if path == "/api/state":
                return self._send(200, app.state())
            if path == "/api/info":
                return self._send(200, app.info())
            if path == "/api/job":
                return self._send(200, app.job_state())
            if path == "/setup":
                return self._file("setup.html")
            if path == "/scan":
                return self._file("scan.html")
            if path == "/scan.js":
                return self._file("scan.js")
            if path == "/api/usb":
                return self._send(200, app.usb_state())
            if path == "/api/estimate":
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, app.estimate((q.get("wells", [""])[0]).split(",")))
            if path == "/setup.js":
                return self._file("setup.js")
            if path == "/stream.mjpg":
                return self._stream()
            if path == "/logout":
                app.sessions.pop(self._token(), None)
                return self._send(302, "", "text/plain",
                                  [("Location", "/login"), ("Set-Cookie", "ct=; Max-Age=0; Path=/")])
            self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/login":
                ip = self.client_address[0]
                n, last = app.fail.get(ip, (0, 0))
                if n >= 5 and time.time() - last < 60:
                    return self._send(429, {"error": "too many tries, wait a minute"})
                if check_password(self._body().get("password", "")):
                    app.fail.pop(ip, None)
                    tok = secrets.token_urlsafe(24)
                    app.sessions[tok] = time.time() + SESSION_HOURS * 3600
                    return self._send(200, {"ok": True}, headers=[(
                        "Set-Cookie", f"ct={tok}; HttpOnly; SameSite=Strict; Path=/; "
                                      f"Max-Age={SESSION_HOURS * 3600}")])
                app.fail[ip] = (n + 1, time.time())
                time.sleep(1)
                return self._send(403, {"error": "wrong password"})
            if not self._authed():
                return self._send(401, {"error": "please log in"})
            b = self._body()
            if path == "/api/jog":
                try:
                    err = app.jog(str(b.get("axis", "")).upper(), float(b.get("dist", 0)))
                except ValueError:
                    err = "bad jog"
            elif path == "/api/home":
                err = app.home()
            elif path == "/api/stop":
                app.stop()
                err = None
            elif path == "/api/well":
                err = app.goto_well(str(b.get("well", "")))
            elif path == "/api/autofocus":
                err, res = app.autofocus()
                if not err:
                    d = app.state()
                    d["msg"] = f"In focus at Z {res['z']:.3f} mm (contrast {res['contrast']:.1f})"
                    return self._send(200, d)
            elif path == "/api/job/start":
                err = app.start_job(str(b.get("kind", "")), b)
                if not err:
                    return self._send(200, app.job_state())
            elif path == "/api/job/cancel":
                err = app.cancel_job()
            elif path == "/api/usb/eject":
                err = app.eject()
                d = app.usb_state()
                if err:
                    d["error"] = err
                return self._send(200, d)
            elif path == "/api/button":
                if b.get("name") in buttons.NAMES:
                    app.on_button(b["name"])
                return self._send(200, app.state())
            elif path == "/api/usb/simplug":
                app.sim_plug()
                return self._send(200, app.usb_state())
            else:
                return self._send(404, {"error": "not found"})
            d = app.state()
            if err:
                d["error"] = err
            self._send(200, d)

        def _stream(self):
            app.viewers += 1
            try:
                self._stream_frames()
            finally:
                app.viewers -= 1

        def _stream_frames(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while not app.stop_evt.is_set():
                    with app.frame_cond:
                        app.frame_cond.wait(timeout=2)
                        jpg = app.frame
                    if jpg is None:
                        continue
                    self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                    self.wfile.write(jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    return H


class Server(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    p = argparse.ArgumentParser(description="Clonetron scanner app")
    p.add_argument("--sim", action="store_true", help="simulated scanner, for trying it on a laptop")
    p.add_argument("--set-password", action="store_true", help="choose the login password")
    p.add_argument("--port", type=int, help="web port (default 8000)")
    p.add_argument("--local", action="store_true", help="only this computer can connect")
    a = p.parse_args()

    if a.set_password or not os.path.exists(AUTH_FILE):
        if not a.set_password:
            print("No password set yet.")
            if not sys.stdin.isatty():
                print("Run  python3 clonetron_server.py --set-password  in a terminal first.")
                sys.exit(1)
        set_password()
        if a.set_password:
            return

    cfg = config.load()
    port = a.port or int(cfg.get("web_port", 8000))
    app = App(cfg, a.sim)
    app.port = port
    host = "127.0.0.1" if a.local else ""
    httpd = Server((host, port), make_handler(app))
    if a.local or a.sim:
        where = "127.0.0.1"
    else:
        ips = os.popen("hostname -I 2>/dev/null").read().split()
        where = ips[0] if ips else "<Pi IP>"
    print(f"\nClonetron{' (SIMULATOR)' if a.sim else ''} is running.")
    print(f"Open in a browser:  http://{where}:{port}")
    print("Ctrl+C here to stop.\n")
    def _term(*_):                               # "systemctl stop" sends SIGTERM: shut down cleanly
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        app.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
