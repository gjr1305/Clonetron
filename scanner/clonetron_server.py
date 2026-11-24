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
already has. Step 2a: login, live view, jog, home, stop.
"""
import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import socketserver
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

WEB = os.path.join(HERE, "web")
AUTH_FILE = os.path.expanduser("~/.clonetron_auth.json")
SESSION_HOURS = 12
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
class App:
    def __init__(self, cfg, sim):
        self.cfg, self.sim = cfg, sim
        self.stage, self.cam, self.flat = hw.open_hardware(cfg, sim)
        self.cam_lock = threading.Lock()
        self.busy = threading.Lock()              # one stage action at a time
        self.action = ""                          # what the stage is doing, for the page
        self.sharp = 0.0
        self.frame = None
        self.frame_cond = threading.Condition()
        self.sessions = {}                        # token -> expiry time
        self.stop_evt = threading.Event()
        self.fail = {}                            # ip -> (count, last time): slow down guessing
        self._last_pos = (0.0, 0.0, 0.0)
        threading.Thread(target=self._preview_loop, daemon=True).start()

    def _preview_loop(self):
        while not self.stop_evt.is_set():
            t = time.time()
            try:
                with self.cam_lock:
                    img = self.cam.capture_array("main")
                small = cv2.resize(img, hw.PREVIEW, interpolation=cv2.INTER_AREA)
                self.sharp = hw.sharpness_of(small)
                ok, jpg = cv2.imencode(".jpg", self.flat.apply(small),
                                       [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    with self.frame_cond:
                        self.frame = jpg.tobytes()
                        self.frame_cond.notify_all()
            except Exception as e:
                print("preview error:", e)
                time.sleep(0.5)
            time.sleep(max(0.0, 0.1 - (time.time() - t)))

    def state(self):
        if self.busy.locked() and not self.sim:
            st, p = "Moving", self._last_pos      # GRBL is busy with the move; don't wait on it
        else:
            try:
                st, p = self.stage.pos()
                self._last_pos = p
            except Exception as e:
                st, p = "Error: " + str(e), self._last_pos
        x, y, z = p
        return {"state": st, "x": round(x, 3), "y": round(y, 3), "z": round(z, 4),
                "sharp": round(self.sharp, 1), "busy": self.busy.locked(),
                "action": self.action, "sim": self.sim,
                "z_limit": self.cfg["z_limit_mm"]}

    def run(self, label, fn):
        """Do a stage action unless another one is running. Returns an error string or None."""
        if not self.busy.acquire(blocking=False):
            return "the stage is busy (" + self.action + ")"
        self.action = label
        try:
            fn()
            return None
        except Exception as e:
            return str(e)
        finally:
            self.action = ""
            self.busy.release()

    def jog(self, axis, dist):
        if axis not in ("X", "Y", "Z") or not 0 < abs(dist) <= 50:
            return "bad jog"

        def do():
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

    def close(self):
        self.stop_evt.set()
        time.sleep(0.2)
        self.cam.stop()
        self.stage.close()


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
            else:
                return self._send(404, {"error": "not found"})
            d = app.state()
            if err:
                d["error"] = err
            self._send(200, d)

        def _stream(self):
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
        set_password()
        if a.set_password:
            return

    cfg = config.load()
    port = a.port or int(cfg.get("web_port", 8000))
    app = App(cfg, a.sim)
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
