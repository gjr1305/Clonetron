#!/usr/bin/env python3
"""
Clonetron analysis: open .clonescan files on a laptop, stitch them, count colonies.

  python clonetron_analysis.py            (or double-click "Start Clonetron Analysis.bat")

Runs a small web server on this computer only (http://127.0.0.1:8770) and opens it
in the browser. Nothing is sent anywhere. Needs numpy and opencv-python.

What it keeps, next to each scan file, in "<scan name> results":
  BL_stitched.jpg / .json   stitched well and its scale (from the stitcher)
  colonies.json             colonies found per well, with your edits
  counts.csv                one row per well (written on Export)
  colonies.csv              one row per colony (written on Export)
The .clonescan file itself is never changed.
"""
import argparse
import csv
import io
import json
import os
import string
import sys
import threading
import time
import traceback
import types
import webbrowser
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import clonescan  # noqa: E402
import colonies  # noqa: E402
import stitch_scan  # noqa: E402

WEB = os.path.join(HERE, "web")
SETTINGS = os.path.join(os.path.expanduser("~"), ".clonetron_analysis.json")
VERSION = "0.1"
WELL_ORDER = ["FL", "ML", "BL", "FR", "MR", "BR"]


def load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    try:
        with open(SETTINGS, "w") as f:
            json.dump(s, f, indent=1)
    except OSError:
        pass


def remember_folder(path):
    s = load_settings()
    rec = [p for p in s.get("recent", []) if p != path]
    s["recent"] = [path] + rec[:7]
    save_settings(s)


# ---------------------------------------------------------------- finding scans
def drives():
    """Folders worth offering: USB sticks / drives that have a Clonetron folder, home."""
    out = []
    if os.name == "nt":
        for L in string.ascii_uppercase[2:]:
            root = f"{L}:\\"
            if os.path.exists(os.path.join(root, "Clonetron")):
                out.append(os.path.join(root, "Clonetron"))
    else:
        for base in ("/media", "/mnt", "/Volumes", "/tmp/clonetron-sim-usb"):
            if not os.path.isdir(base):
                continue
            for dirpath, dirs, _ in os.walk(base):
                if "Clonetron" in dirs:
                    out.append(os.path.join(dirpath, "Clonetron"))
                if dirpath.count(os.sep) - base.count(os.sep) >= 2:
                    dirs[:] = []
    return out


def describe(path):
    """Short facts about one scan, for the list."""
    name = os.path.basename(path)
    base = name[:-len(clonescan.EXT)] if name.endswith(clonescan.EXT) else name
    item = {"path": path, "name": base, "file": name.endswith(clonescan.EXT)}
    parts = base.split("_", 2)
    if len(parts) >= 2 and len(parts[0]) == 10:
        item["date"] = parts[0]
        item["time"] = parts[1][:2] + ":" + parts[1][2:4] if len(parts[1]) >= 4 else ""
        item["plate"] = parts[2] if len(parts) > 2 else ""
    else:
        item["plate"] = base
    try:
        if item["file"]:
            item["mb"] = round(os.path.getsize(path) / 1e6)
            import zipfile
            with zipfile.ZipFile(path) as z:
                m = json.loads(z.read(clonescan.MANIFEST))
            item.update(photos=m.get("photos"), wells=m.get("wells"), complete=m.get("complete", True))
    except Exception:
        item["bad"] = True
    res = results_dir(path, create=False)
    item["stitched"] = bool(res and any(f.endswith("_stitched.jpg") for f in os.listdir(res)))
    item["counted"] = bool(res and os.path.exists(os.path.join(res, "colonies.json")))
    return item


def results_dir(path, create=True):
    if os.path.isdir(path):
        return path
    d = os.path.join(os.path.dirname(path), os.path.basename(path)[:-len(clonescan.EXT)] + " results")
    if create:
        os.makedirs(d, exist_ok=True)
    return d if os.path.isdir(d) else None


def _hidden(entry):
    """Windows hidden/system entries and old-style links such as 'My Documents'
    inside the user folder, which only give 'Access is denied'."""
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & (0x2 | 0x4))                  # HIDDEN | SYSTEM (OneDrive folders stay)


def browse(path):
    path = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(path):
        raise ValueError("That folder doesn't exist")
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except PermissionError:
        raise ValueError("Windows doesn't allow opening this folder. Pick another one, "
                         "e.g. Documents or the USB stick.")
    dirs, scans = [], []
    for e in entries:
        n, p = e.name, e.path
        if n.startswith(".") or n.endswith(".part") or _hidden(e):
            continue
        try:
            if n.endswith(clonescan.EXT):
                scans.append(describe(p))
            elif e.is_dir():
                if os.path.exists(os.path.join(p, "positions.csv")):
                    scans.append(describe(p))
                elif not n.endswith(" results"):
                    dirs.append(n)
        except OSError:
            continue                                   # a folder we may not look into
    scans.sort(key=lambda s: s["name"], reverse=True)
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else None, "dirs": dirs, "scans": scans}


# ---------------------------------------------------------------- one scan
def well_info(res, w):
    try:
        with open(os.path.join(res, f"{w}_stitched.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def open_scan(path):
    s = clonescan.Scan(path)
    res = results_dir(path)
    info = s.read_json("scan_info.json", {}) or {}
    wells = s.manifest.get("wells") or info.get("wells")
    if not wells:
        rows = s.read_text("positions.csv").splitlines()[1:]
        wells = list(dict.fromkeys(r.split(",")[1] for r in rows if r))
    counts = read_colonies(res)
    per_well = {}
    try:                                         # which wells actually have photos
        for r in s.read_text("positions.csv").splitlines()[1:]:
            parts = r.split(",")
            if len(parts) > 1:
                per_well[parts[1]] = per_well.get(parts[1], 0) + 1
    except (OSError, KeyError):
        pass
    out = {"path": path, "name": s.name, "results": res, "manifest": s.manifest,
           "wells": [], "has_flat": s.has("flat.npy") or os.path.exists(
               os.path.join(os.path.dirname(path), "calibration", "flat.npy")),
           "made": s.manifest.get("made") or info.get("made"), "plate_stitched":
           os.path.exists(os.path.join(res, "plate_stitched.jpg"))}
    for w in wells:
        wi = well_info(res, w)
        c = counts.get("wells", {}).get(w)
        out["wells"].append({
            "name": w, "stitched": os.path.exists(os.path.join(res, f"{w}_stitched.jpg")),
            "photos": per_well.get(w, 0),
            "info": wi, "counted": c is not None,
            "count": None if c is None else sum(1 for k in c["colonies"] if not k.get("removed")),
            "reviewed": bool(c and c.get("reviewed"))})
    return out


def read_colonies(res):
    try:
        with open(os.path.join(res, "colonies.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"version": 1, "wells": {}}


def write_colonies(res, data):
    tmp = os.path.join(res, "colonies.json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, os.path.join(res, "colonies.json"))


def count_well(path, w, params, fresh=False):
    res = results_dir(path)
    img = cv2.imread(os.path.join(res, f"{w}_stitched.jpg"))
    if img is None:
        raise RuntimeError(f"{w} isn't stitched yet")
    wi = well_info(res, w) or {}
    ppm = wi.get("px_per_mm")
    if not ppm:
        raise RuntimeError(f"{w}: no scale saved. Stitch the scan again (Re-stitch).")
    found, stats = colonies.detect(img, ppm, wi.get("centre_px"), wi.get("well_radius_mm", 17.4), params)
    data = read_colonies(res)
    old = data["wells"].get(w, {})
    manual = [] if fresh else [c for c in old.get("colonies", []) if c.get("source") == "manual"]
    nid = max([c["id"] for c in found] + [0]) + 1
    for c in manual:                        # hand-added colonies survive a recount
        c["id"] = nid
        nid += 1
    data["wells"][w] = {"colonies": found + manual, "params": stats["params"],
                        "noise": stats["noise"], "threshold": stats["threshold"],
                        "px_per_mm": ppm, "counted": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "reviewed": False}
    write_colonies(res, data)
    return len(found)


def export(path):
    s = open_scan(path)
    res = s["results"]
    data = read_colonies(res)
    plate = describe(path).get("plate", "")
    rows, crow = [], []
    for w in s["wells"]:
        d = data["wells"].get(w["name"])
        if not d:
            rows.append({"scan": s["name"], "plate": plate, "well": w["name"], "colonies": "",
                         "reviewed": "", "median_diameter_mm": "", "mean_area_mm2": ""})
            continue
        cols = [c for c in d["colonies"] if not c.get("removed")]
        diam = sorted(c["diameter_mm"] for c in cols)
        rows.append({"scan": s["name"], "plate": plate, "well": w["name"], "colonies": len(cols),
                     "reviewed": "yes" if d.get("reviewed") else "no",
                     "median_diameter_mm": round(float(np.median(diam)), 3) if diam else "",
                     "mean_area_mm2": round(float(np.mean([c["area_mm2"] for c in cols])), 4) if cols else ""})
        for c in cols:
            crow.append({"scan": s["name"], "plate": plate, "well": w["name"], "colony": c["id"],
                         "x_mm": round(c["x"] / d["px_per_mm"], 3), "y_mm": round(c["y"] / d["px_per_mm"], 3),
                         "area_mm2": c["area_mm2"], "diameter_mm": c["diameter_mm"],
                         "darkness": c.get("darkness", ""), "source": c.get("source", "auto")})
    for fname, rr in (("counts.csv", rows), ("colonies.csv", crow)):
        if rr:
            with open(os.path.join(res, fname), "w", newline="") as f:
                wr = csv.DictWriter(f, fieldnames=list(rr[0].keys()))
                wr.writeheader()
                wr.writerows(rr)
    return res, rows


# ---------------------------------------------------------------- background jobs
JOB = {"status": "idle", "title": "", "log": [], "path": None, "progress": None}
JOB_LOCK = threading.Lock()


class _LogWriter(io.TextIOBase):
    def write(self, s):
        for line in s.splitlines():
            if line.strip():
                JOB["log"].append(line.rstrip())
                if "[well " in line:
                    try:
                        a, b = line.split("[well ")[1].split("]")[0].split("/")
                        JOB["progress"] = [int(a) - 1, int(b)]
                    except ValueError:
                        pass
        return len(s)


def start_job(title, path, fn):
    with JOB_LOCK:
        if JOB["status"] == "running":
            raise RuntimeError("Something is already running: " + JOB["title"])
        JOB.update(status="running", title=title, log=[], path=path, progress=None,
                   started=time.strftime("%H:%M:%S"))

    def run():
        try:
            with redirect_stdout(_LogWriter()):
                fn()
            JOB["status"] = "done"
        except Exception as e:
            JOB["log"].append("Problem: " + str(e))
            JOB["log"] += traceback.format_exc().splitlines()[-4:]
            JOB["status"] = "failed"
    threading.Thread(target=run, daemon=True).start()


def stitch_job(path, redo=False):
    a = types.SimpleNamespace(rotate=None, no_align=False, no_plate=False, redo=redo)
    stitch_scan.stitch_folder(path, a)


def count_job(path, wells, params, fresh=False):
    for i, w in enumerate(wells):
        JOB["progress"] = [i, len(wells)]
        print(f"Counting {w}...")
        n = count_well(path, w, params, fresh)
        print(f"  {w}: {n} colonies")
    JOB["progress"] = [len(wells), len(wells)]


def stitch_and_count(path, params, fresh=False):
    """fresh: stitch every well again and count from scratch, dropping all hand edits."""
    stitch_job(path, redo=fresh)
    s = open_scan(path)
    count_job(path, [w["name"] for w in s["wells"] if w["stitched"]], params, fresh)


# ---------------------------------------------------------------- web server
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
         ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype=None):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        self._send(200, data, ctype or TYPES.get(os.path.splitext(path)[1], "application/octet-stream"))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._file(os.path.join(WEB, "index.html"))
            if u.path.startswith("/web/"):
                name = os.path.basename(u.path)
                return self._file(os.path.join(WEB, name))
            if u.path == "/api/places":
                s = load_settings()
                return self._send(200, {"drives": drives(), "recent": [p for p in s.get("recent", [])
                                                                        if os.path.isdir(p)],
                                        "home": os.path.expanduser("~"), "version": VERSION})
            if u.path == "/api/browse":
                return self._send(200, browse(q.get("path")))
            if u.path == "/api/scan":
                return self._send(200, open_scan(q["path"]))
            if u.path == "/api/image":
                res = results_dir(q["path"], create=False) or ""
                name = "plate_stitched.jpg" if q.get("well") == "plate" else f"{q['well']}_stitched.jpg"
                return self._file(os.path.join(res, name), "image/jpeg")
            if u.path == "/api/colonies":
                res = results_dir(q["path"], create=False)
                d = read_colonies(res) if res else {"wells": {}}
                return self._send(200, d["wells"].get(q["well"]) or {})
            if u.path == "/api/job":
                return self._send(200, {k: JOB.get(k) for k in ("status", "title", "log", "path",
                                                                 "progress", "started")})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(400, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        if self.headers.get("X-Clonetron") != "1":   # only our own page may change things
            return self._send(403, {"error": "forbidden"})
        try:
            b = self._body()
            if u.path == "/api/remember":
                remember_folder(b["path"])
                return self._send(200, {"ok": True})
            if u.path == "/api/stitch":
                start_job("Stitching " + os.path.basename(b["path"]), b["path"],
                          lambda: stitch_job(b["path"], b.get("redo", False)))
                return self._send(200, {"ok": True})
            if u.path == "/api/analyse":
                fresh = bool(b.get("fresh"))
                start_job("Starting over: stitching and counting" if fresh else "Stitching and counting",
                          b["path"], lambda: stitch_and_count(b["path"], b.get("params"), fresh))
                return self._send(200, {"ok": True})
            if u.path == "/api/count":
                wells = b.get("wells") or [w["name"] for w in open_scan(b["path"])["wells"] if w["stitched"]]
                start_job("Counting colonies", b["path"], lambda: count_job(b["path"], wells, b.get("params")))
                return self._send(200, {"ok": True})
            if u.path == "/api/colonies":
                res = results_dir(b["path"])
                data = read_colonies(res)
                w = data["wells"].setdefault(b["well"], {"colonies": [], "px_per_mm": b.get("px_per_mm")})
                w["colonies"] = b["colonies"]
                if "reviewed" in b:
                    w["reviewed"] = bool(b["reviewed"])
                w["edited"] = time.strftime("%Y-%m-%d %H:%M:%S")
                write_colonies(res, data)
                return self._send(200, {"ok": True})
            if u.path == "/api/export":
                res, rows = export(b["path"])
                return self._send(200, {"folder": res, "rows": rows})
            if u.path == "/api/show":                      # open the results folder
                res = results_dir(b["path"])
                if os.name == "nt":
                    os.startfile(res)  # noqa: S606
                return self._send(200, {"folder": res})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(400, {"error": str(e)})


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="Clonetron analysis app")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    try:
        srv = Server(("127.0.0.1", a.port), Handler)
    except OSError:
        url = f"http://127.0.0.1:{a.port}"
        print(f"Clonetron Analysis seems to be running already: {url}")
        if not a.no_browser:
            webbrowser.open(url)
        return
    url = f"http://127.0.0.1:{a.port}"
    print(f"Clonetron Analysis {VERSION} is running at {url}")
    print("Keep this window open while you use it. Close it to stop.")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
