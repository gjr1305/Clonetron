#!/usr/bin/env python3
"""
ColonyScope live control: live camera view + stage moving + photo capture,
all in a web page you open in your laptop's browser.

2026-11-23 version (v3): focus check panel. Pick a well, click one of the
focus-map spots and the stage goes there at the height the map predicts.
Find the sharpest height by eye (fine Z steps + a live sharpness number),
optionally press "Auto-focus here" to see where the autofocus lands, then
"Save my focus". Every save goes to ~/scans/manual_focus.csv with the map,
auto and your heights side by side. Z moves always finish going UP from
0.5 mm below, the same way the scan does, so heights are comparable.

2026-11-21 version: colour-corrected. Uses ~/scans/camera.json and
~/scans/flat.npy from flatfield_calibrate.py, so the live view and
photos have a white background with no pink corners. Each photo is saved
corrected, and the untouched original goes in a raw/ subfolder.

Run on the Pi:   python3 live_focus.py
Then on laptop:  open  http://<Pi IP>:8000   (find the IP with: hostname -I)
Stop:            Ctrl+C in the Pi terminal

Keys in the browser (click the page first):
  a / d   X left / right        w / s   Y away / towards
  r / f   Z up / down           1 2 3 4 step 0.1 / 1 / 5 / 10 mm
                                5 6 7   step 0.01 / 0.02 / 0.05 mm (for focusing)
  c       capture a photo       h       home again

Photos go to ~/scans/live_<date>/ with the position in the file name.
Nothing else may use the camera while this runs (close rpicam-hello first).
"""
import io
import json
import os
import re
import socketserver
import threading
import time
from http import server
from urllib.parse import parse_qs, urlparse

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

PORT_SERIAL = "/dev/ttyACM0"
HTTP_PORT = 8000
SIZE = (1640, 1232)          # live view and photo size
FEED = {"X": 1000, "Y": 1000, "Z": 200}
BACKLASH_Z = 0.50            # Z always finishes moving up from this far below
XY_OVER = 0.6                # XY always arrives moving in + direction from this far
Z_LIMIT = 28.0
MAP_FILE = os.path.join(SCANS_DIR, "focus_map.json")
MANUAL_CSV = os.path.join(SCANS_DIR, "manual_focus.csv")
WELLS = {"BL": (66.0, 42.3), "BR": (66.0, 81.4), "ML": (105.1, 42.3),
         "MR": (105.1, 81.4), "FL": (144.2, 42.3), "FR": (144.2, 81.4)}
SHARP = {"v": 0.0}           # live sharpness of the preview
PREVIEW = (820, 616)         # live view size sent to the browser
PREVIEW_FPS = 10


# ---------------------------------------------------------------- GRBL
class Grbl:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=0.1)
        time.sleep(2)
        self.s.reset_input_buffer()
        self.lock = threading.Lock()
        self.home = (0.0, 0.0, 0.0)

    def _send(self, line, timeout=180):
        self.s.write((line + "\n").encode())
        buf, t = "", time.time()
        while time.time() - t < timeout:
            buf += self.s.read(self.s.in_waiting or 1).decode(errors="ignore")
            if "ok" in buf:
                return
            if "error" in buf or "ALARM" in buf:
                raise RuntimeError(buf.strip())
        raise RuntimeError("no reply from GRBL")

    def _status(self, wait_idle):
        for _ in range(1200):
            self.s.write(b"?")
            time.sleep(0.05)
            r = self.s.read(self.s.in_waiting or 1).decode(errors="ignore")
            m = re.search(r"<(\w+)[|:].*?MPos:(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)", r)
            if m and (not wait_idle or m.group(1) == "Idle"):
                return m.group(1), tuple(float(v) for v in m.groups()[1:])
        raise RuntimeError("no status from GRBL")

    def do_home(self):
        with self.lock:
            self._send("$H")
            _, self.home = self._status(True)
            self._send("G21")

    def jog(self, axis, dist):
        with self.lock:
            if axis == "Z" and dist < 0:       # go below, then come up: same as the scan
                self._send(f"$J=G91 G21 Z{dist - BACKLASH_Z:.4f} F{FEED['Z']}")
                self._status(True)
                self._send(f"$J=G91 G21 Z{BACKLASH_Z:.4f} F{FEED['Z']}")
            else:
                self._send(f"$J=G91 G21 {axis}{dist:.4f} F{FEED[axis]}")
            self._status(True)

    def goto(self, x=None, y=None, z=None):
        """Absolute move, mm from home. XY arrive moving +, Z arrives moving up."""
        hx, hy, hz = self.home
        with self.lock:
            if x is not None and y is not None:
                X, Y = hx + x, hy + y
                self._send(f"G53 G1 X{X - XY_OVER:.3f} Y{Y - XY_OVER:.3f} F3000")
                self._send(f"G53 G1 X{X:.3f} Y{Y:.3f} F3000")
                self._status(True)
            if z is not None:
                z = min(z, Z_LIMIT)
                self._send(f"G53 G1 Z{hz + z - BACKLASH_Z:.4f} F{FEED['Z']}")
                self._send(f"G53 G1 Z{hz + z:.4f} F{FEED['Z']}")
                self._status(True)

    def z_up_to(self, z):
        """Small upward Z move (no dip needed when already moving up)."""
        with self.lock:
            self._send(f"G53 G1 Z{self.home[2] + min(z, Z_LIMIT):.4f} F{FEED['Z']}")
            self._status(True)

    def pos(self):
        with self.lock:
            state, p = self._status(False)
        return state, tuple(a - b for a, b in zip(p, self.home))

    def close(self):
        try:
            self.s.write(b"\x85")
        finally:
            self.s.close()


# ---------------------------------------------------------------- camera
class Frames(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.cond = threading.Condition()

    def write(self, buf):
        with self.cond:
            self.frame = buf
            self.cond.notify_all()


def sharpness_of(img):
    """Same idea as the autofocus: edge detail in the middle of the picture."""
    g = img[..., 1]
    h, w = g.shape[:2]
    g = g[h // 5: 4 * h // 5, w // 5: 4 * w // 5]
    g = cv2.resize(g, (384, int(384 * g.shape[0] / g.shape[1])), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(g.astype(np.float32), cv2.CV_32F).var())


def autofocus_here(grbl, cam, cam_lock, z_start):
    """Coarse + fine sweep around z_start (mm from home). Returns (best z, contrast)."""
    def score_at(z, first):
        if first:
            grbl.goto(z=z)                 # dip below and come up
        else:
            grbl.z_up_to(z)                # sweep only ever moves up
        time.sleep(0.1)
        with cam_lock:
            cam.capture_array("main")
            img = cam.capture_array("main")
        return sharpness_of(cv2.resize(img, PREVIEW, interpolation=cv2.INTER_AREA))

    def sweep(centre, half, step):
        zs = np.arange(centre - half, centre + half + step / 2, step)
        zs = zs[zs <= Z_LIMIT]
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
    grbl.goto(z=float(best))
    return float(best), ratio


def load_spots():
    """Focus-map spots per well, in mm from home, labelled C, i1-i6, o1-o6."""
    try:
        with open(MAP_FILE) as f:
            pts = json.load(f)["points"]
    except Exception:
        return {}
    out = {}
    for w, x, y, z in pts:
        out.setdefault(w, []).append([x, y, z])
    labelled = {}
    for w in [k for k in WELLS if k in out]:
        cx, cy = -WELLS[w][0], WELLS[w][1]
        items = []
        for x, y, z in out[w]:
            dx, dy = x - cx, y - cy
            r = (dx * dx + dy * dy) ** 0.5
            items.append({"x": x, "y": y, "z": z, "dx": round(dx, 1), "dy": round(dy, 1),
                          "ring": 0 if r < 2 else (1 if r < 11 else 2)})
        items.sort(key=lambda d: (d["ring"], float(np.arctan2(d["dy"], d["dx"]))))
        n = {0: 0, 1: 0, 2: 0}
        for d in items:
            n[d["ring"]] += 1
            d["label"] = "C" if d["ring"] == 0 else ("i" if d["ring"] == 1 else "o") + str(n[d["ring"]])
        labelled[w] = items
    return labelled


def stream_loop(cam, cam_lock, flat, frames, stop):
    """Grab frames, correct them, and publish as JPEG for the browser."""
    while not stop.is_set():
        t = time.time()
        try:
            with cam_lock:
                img = cam.capture_array("main")
            small = cv2.resize(img, PREVIEW, interpolation=cv2.INTER_AREA)
            SHARP["v"] = sharpness_of(small)
            ok, jpg = cv2.imencode(".jpg", flat.apply(small), [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                frames.write(jpg.tobytes())
        except Exception as e:
            print("preview error:", e)
            time.sleep(0.5)
        time.sleep(max(0.0, 1.0 / PREVIEW_FPS - (time.time() - t)))


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>ColonyScope live</title>
<style>
body{font-family:sans-serif;margin:0;background:#111;color:#eee;display:flex;flex-wrap:wrap}
#v{flex:1 1 640px;padding:8px}#v img{width:100%;border:1px solid #444}
#c{width:300px;padding:12px}button{font-size:16px;margin:3px;padding:10px 12px;min-width:56px;
background:#2a2a2a;color:#eee;border:1px solid #555;border-radius:6px;cursor:pointer}
button.on{background:#2d6cdf;border-color:#2d6cdf}button:active{background:#444}
.g{display:grid;grid-template-columns:repeat(3,64px);gap:4px;margin:8px 0}
#pos,#msg{font-family:monospace;font-size:14px;margin:8px 0;white-space:pre}
h3{margin:12px 0 4px;font-size:15px;color:#aaa}
</style></head><body>
<div id="v"><img src="/stream.mjpg"></div>
<div id="c">
<h3>Step (1-4)</h3>
<div id="steps"><button data-s="0.1">0.1</button><button data-s="1" class="on">1</button>
<button data-s="5">5</button><button data-s="10">10</button> mm<br>
<button data-s="0.01">0.01</button><button data-s="0.02">0.02</button><button data-s="0.05">0.05</button> fine</div>
<h3>X / Y (a d w s)</h3>
<div class="g"><span></span><button onclick="jog('Y',-1)">w &#8593;</button><span></span>
<button onclick="jog('X',-1)">a &#8592;</button><span></span><button onclick="jog('X',1)">d &#8594;</button>
<span></span><button onclick="jog('Y',1)">s &#8595;</button><span></span></div>
<h3>Z focus (r f)</h3>
<button onclick="jog('Z',1)">r up</button><button onclick="jog('Z',-1)">f down</button>
<h3>Actions</h3>
<button onclick="cap()">c capture</button><button onclick="home()">h home</button>
<div id="pos">...</div><div id="sharp" style="font:bold 20px monospace;color:#7fd17f">sharpness -</div>
<div id="msg" style="white-space:normal"></div>
<h3>Focus check (map spots)</h3>
<div id="wells"></div>
<svg id="well" width="270" height="270" viewBox="-20 -20 40 40" style="background:#1b1b1b;border-radius:8px"></svg>
<div id="spot" style="font-family:monospace;font-size:14px;white-space:pre;margin:6px 0">click a spot</div>
<button onclick="af()">Auto-focus here</button><button onclick="save()" style="background:#2f7d32">Save my focus</button>
</div>
<script>
let step=1,busy=false;
const msg=t=>document.getElementById('msg').textContent=t;
document.querySelectorAll('#steps button').forEach(b=>b.onclick=()=>setStep(+b.dataset.s));
function setStep(s){step=s;document.querySelectorAll('#steps button').forEach(b=>
 b.classList.toggle('on',+b.dataset.s===s));}
async function call(u){if(busy)return;busy=true;try{const r=await fetch(u);const j=await r.json();
 if(j.error)msg('Error: '+j.error);else if(j.msg)msg(j.msg);show(j);}catch(e){msg('Error: '+e);}busy=false;}
function jog(a,sign){call(`/api/jog?axis=${a}&dist=${sign*step}`);}
function cap(){msg('Capturing...');call('/api/capture');}
function home(){msg('Homing...');call('/api/home');}
function show(j){if(!j.pos)return;const[x,y,z]=j.pos;
 document.getElementById('pos').textContent=
 `From home (${j.state})\\nX ${Math.abs(x).toFixed(1)} mm ${x<0?'left':'right'}`+
 `\\nY ${Math.abs(y).toFixed(1)} mm ${y>0?'towards':'away'}`+
 `\\nZ ${z.toFixed(3)} mm\\nStep ${step} mm`;}
let spots={},well=null,cur=null,auto=null,saved={};
fetch('/api/spots').then(r=>r.json()).then(d=>{spots=d;const w=document.getElementById('wells');
 if(!Object.keys(d).length){w.textContent='No focus map yet (make one with the scan script).';return;}
 Object.keys(d).forEach(k=>{const b=document.createElement('button');b.textContent=k;
  b.onclick=()=>pickWell(k);w.appendChild(b);});});
function pickWell(k){well=k;document.querySelectorAll('#wells button').forEach(b=>
 b.classList.toggle('on',b.textContent===k));draw();}
function draw(){const s=document.getElementById('well');let h='<circle r="17.4" fill="none" stroke="#888" stroke-width="0.3"/>';
 (spots[well]||[]).forEach((p,i)=>{const k=well+p.label,c=saved[k]?'#3fae49':(cur&&cur.k===k?'#2d6cdf':'#ddd');
  h+=`<circle cx="${p.dx}" cy="${p.dy}" r="1.6" fill="${c}" style="cursor:pointer" onclick="go(${i})"/>`+
     `<text x="${p.dx}" y="${p.dy+3.4}" font-size="2" fill="#aaa" text-anchor="middle">${p.label}</text>`;});
 s.innerHTML=h;}
function spotText(){if(!cur)return;let t=well+' '+cur.p.label+'  (dx '+cur.p.dx+', dy '+cur.p.dy+')'+
 String.fromCharCode(10)+'map  Z '+cur.p.z.toFixed(3);
 if(auto!==null)t+=String.fromCharCode(10)+'auto Z '+auto.toFixed(3);document.getElementById('spot').textContent=t;}
function go(i){const p=spots[well][i];cur={k:well+p.label,p:p};auto=null;msg('Moving...');draw();spotText();
 call(`/api/goto?x=${p.x}&y=${p.y}&z=${p.z}`);}
function af(){if(!cur){msg('Click a spot first');return;}if(busy)return;busy=true;msg('Auto-focusing... (~30-60 s)');
 fetch(`/api/af?z=${cur.p.z}`).then(r=>r.json()).then(j=>{busy=false;if(j.error)msg('Error: '+j.error);
  else{auto=j.z;msg(`Auto-focus: Z ${j.z.toFixed(3)} (contrast ${j.contrast.toFixed(1)}). Check by eye, adjust with the fine steps, then Save.`);}
  spotText();show(j);}).catch(e=>{busy=false;msg('Error: '+e);});}
function save(){if(!cur){msg('Click a spot first');return;}
 fetch(`/api/save?well=${well}&label=${cur.p.label}&dx=${cur.p.dx}&dy=${cur.p.dy}&map=${cur.p.z}&auto=${auto===null?'':auto}`)
 .then(r=>r.json()).then(j=>{if(j.error){msg('Error: '+j.error);return;}saved[cur.k]=1;draw();msg(j.msg);});}
setInterval(()=>fetch('/api/sharp').then(r=>r.json()).then(j=>document.getElementById('sharp').textContent=
 'sharpness '+j.v.toFixed(0)).catch(()=>{}),400);
const K={a:['X',-1],d:['X',1],w:['Y',-1],s:['Y',1],r:['Z',1],f:['Z',-1]};
document.addEventListener('keydown',e=>{const k=e.key.toLowerCase();
 if(K[k])jog(...K[k]);else if({'1':1,'2':1,'3':1,'4':1}[k])setStep([0.1,1,5,10][+k-1]);
 else if({'5':1,'6':1,'7':1}[k])setStep([0.01,0.02,0.05][+k-5]);
 else if(k==='c')cap();else if(k==='h')home();});
setInterval(()=>{if(!busy)fetch('/api/pos').then(r=>r.json()).then(show).catch(()=>{});},1000);
</script></body></html>"""


def make_handler(grbl, cam, cam_lock, flat, frames, outdir):
    spots = load_spots()
    class H(server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, d):
            b = json.dumps(d).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _withpos(self, d):
            try:
                d["state"], d["pos"] = grbl.pos()
            except Exception as e:
                d.setdefault("error", str(e))
            return d

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                b = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif u.path == "/stream.mjpg":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
                self.end_headers()
                try:
                    while True:
                        with frames.cond:
                            frames.cond.wait()
                            f = frames.frame
                        self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(f)}\r\n\r\n".encode())
                        self.wfile.write(f)
                        self.wfile.write(b"\r\n")
                except Exception:
                    pass
            elif u.path == "/api/spots":
                self._json(spots)
            elif u.path == "/api/sharp":
                self._json({"v": SHARP["v"]})
            elif u.path == "/api/goto":
                d = {}
                try:
                    grbl.goto(float(q["x"]), float(q["y"]), float(q["z"]))
                    d["msg"] = "At the spot, at the map's height. Fine-tune Z by eye."
                except Exception as e:
                    d["error"] = str(e)
                self._json(self._withpos(d))
            elif u.path == "/api/af":
                d = {}
                try:
                    z, c = autofocus_here(grbl, cam, cam_lock, float(q["z"]))
                    d.update(z=z, contrast=c)
                except Exception as e:
                    d["error"] = str(e)
                self._json(self._withpos(d))
            elif u.path == "/api/save":
                d = self._withpos({})
                try:
                    my_z = d["pos"][2]
                    mz = float(q["map"])
                    az = float(q["auto"]) if q.get("auto") else None
                    new = not os.path.exists(MANUAL_CSV)
                    with open(MANUAL_CSV, "a") as f:
                        if new:
                            f.write("time,well,spot,dx_mm,dy_mm,map_z,auto_z,my_z,"
                                    "my_minus_map_um,my_minus_auto_um,sharpness\n")
                        f.write(f"{time.strftime('%H:%M:%S')},{q['well']},{q['label']},{q['dx']},"
                                f"{q['dy']},{mz:.4f},{'' if az is None else f'{az:.4f}'},{my_z:.4f},"
                                f"{1000 * (my_z - mz):+.0f},"
                                f"{'' if az is None else f'{1000 * (my_z - az):+.0f}'},"
                                f"{SHARP['v']:.0f}\n")
                    d["msg"] = (f"Saved {q['well']} {q['label']}: you {my_z:.3f}, map {mz:.3f} "
                                f"({1000 * (my_z - mz):+.0f} um)"
                                + ("" if az is None else
                                   f", auto {az:.3f} ({1000 * (my_z - az):+.0f} um)"))
                except Exception as e:
                    d["error"] = str(e)
                self._json(d)
            elif u.path == "/api/pos":
                self._json(self._withpos({}))
            elif u.path == "/api/jog":
                d = {}
                try:
                    axis = q.get("axis", "").upper()
                    dist = float(q.get("dist", "0"))
                    if axis not in FEED or abs(dist) > 50:
                        raise ValueError("bad jog request")
                    grbl.jog(axis, dist)
                except Exception as e:
                    d["error"] = f"move refused ({e}), maybe past a soft limit"
                self._json(self._withpos(d))
            elif u.path == "/api/home":
                d = {}
                try:
                    grbl.do_home()
                    d["msg"] = "Homed"
                except Exception as e:
                    d["error"] = str(e)
                self._json(self._withpos(d))
            elif u.path == "/api/capture":
                d = self._withpos({})
                try:
                    x, y, z = d.get("pos", (0, 0, 0))
                    name = time.strftime("%H%M%S") + f"_X{-x:.1f}_Y{y:.1f}_Z{z:.1f}.jpg"
                    with cam_lock:
                        img = cam.capture_array("main")
                    os.makedirs(os.path.join(outdir, "raw"), exist_ok=True)
                    cv2.imwrite(os.path.join(outdir, "raw", name), img,
                                [cv2.IMWRITE_JPEG_QUALITY, 95])
                    cv2.imwrite(os.path.join(outdir, name), flat.apply(img),
                                [cv2.IMWRITE_JPEG_QUALITY, 95])
                    d["msg"] = "Saved " + name
                except Exception as e:
                    d["error"] = str(e)
                self._json(d)
            else:
                self.send_error(404)
    return H


class Server(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    outdir = os.path.expanduser(time.strftime("~/scans/live_%Y-%m-%d"))
    os.makedirs(outdir, exist_ok=True)

    print("Connecting to GRBL...")
    grbl = Grbl(PORT_SERIAL)
    print("Homing (Z first, then X and Y)...")
    grbl.do_home()

    print("Starting camera...")
    cam = open_camera()
    cam.configure(cam.create_video_configuration(main={"size": SIZE, "format": "RGB888"},
                                                 controls={"FrameRate": 15}))
    cam.start()
    settings = load_camera_settings()
    if settings:
        apply_camera_settings(cam, settings)
        print(f"Colour calibration from {settings.get('made', '?')} loaded "
              f"(exposure {settings['ExposureTime'] / 1000:.2f} ms)")
    else:
        print("No colour calibration yet: run flatfield_calibrate.py "
              "(plate out, light pad on). Using automatic settings for now.")
    flat = FlatField()
    if flat.flat is None:
        print("No flat.npy yet, so the corners will look pink.")
    cam_lock = threading.Lock()
    frames = Frames()
    stop = threading.Event()
    threading.Thread(target=stream_loop, args=(cam, cam_lock, flat, frames, stop),
                     daemon=True).start()

    httpd = Server(("", HTTP_PORT), make_handler(grbl, cam, cam_lock, flat, frames, outdir))
    ip = os.popen("hostname -I").read().split()
    print(f"\nOpen in your laptop browser:  http://{ip[0] if ip else '<Pi IP>'}:{HTTP_PORT}")
    print(f"Photos are saved in {outdir}")
    print("Ctrl+C here to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        stop.set()
        time.sleep(0.3)
        cam.stop()
        grbl.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
