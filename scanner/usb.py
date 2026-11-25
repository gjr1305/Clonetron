"""
USB stick handling for the scanner app: find the stick, check space, eject.

Scans are only ever saved to a USB stick, never to the Pi's SD card. On the
Pi, USB sticks show up as /dev/sda1, /dev/sdb1 ... (the SD card is
/dev/mmcblk0), so anything mounted from /dev/sd* counts as a stick.
"""
import os
import shutil
import subprocess
import tempfile

SCAN_SUBFOLDER = "Clonetron"            # scans go in <stick>/Clonetron/<date>_<name>/
SIM_ROOT = os.path.join(tempfile.gettempdir(), "clonetron-sim-usb")


def _run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _writable(path):
    try:
        with tempfile.NamedTemporaryFile(dir=path):
            return True
    except Exception:
        return False


def sticks(sim=False):
    """Mounted USB sticks: [{dev, mount, label, free, total, writable}]"""
    out = []
    if sim:
        if os.path.isdir(SIM_ROOT):
            u = shutil.disk_usage(SIM_ROOT)
            out.append({"dev": "sim", "mount": SIM_ROOT, "label": "SIM-USB", "free": u.free,
                        "total": u.total, "writable": True})
        return out
    try:
        with open("/proc/mounts") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or not parts[0].startswith("/dev/sd"):
            continue
        dev, mount = parts[0], parts[1].replace("\\040", " ")
        try:
            u = shutil.disk_usage(mount)
        except OSError:
            continue
        out.append({"dev": dev, "mount": mount, "label": os.path.basename(mount) or dev,
                    "free": u.free, "total": u.total, "writable": _writable(mount)})
    return out


def unmounted(sim=False):
    """USB partitions that are plugged in but not opened (mounted) yet."""
    if sim:
        return []
    code, txt = _run(["lsblk", "-nrpo", "NAME,TYPE,MOUNTPOINT"])
    if code:
        return []
    found = []
    for line in txt.splitlines():
        p = line.split()
        if len(p) >= 2 and p[0].startswith("/dev/sd") and p[1] == "part" and len(p) == 2:
            found.append(p[0])
    return found


def try_mount(dev):
    """Open a stick that wasn't opened automatically (needs udisks2, standard on Pi OS)."""
    code, txt = _run(["udisksctl", "mount", "-b", dev, "--no-user-interaction"])
    return None if code == 0 else txt or "couldn't open the stick"


def get(sim=False):
    """The stick to save to, opening one if it's plugged in but not mounted."""
    s = sticks(sim)
    if not s:
        for dev in unmounted(sim):
            try_mount(dev)
        s = sticks(sim)
    return s[0] if s else None


def eject(stick, sim=False):
    """Finish writing, unmount and power the stick off. Returns an error or None."""
    _run(["sync"], timeout=120)
    if sim:
        return None
    code, txt = _run(["udisksctl", "unmount", "-b", stick["dev"], "--no-user-interaction"], 60)
    if code:
        return "couldn't unmount: " + txt
    disk = stick["dev"].rstrip("0123456789")
    _run(["udisksctl", "power-off", "-b", disk, "--no-user-interaction"])
    return None


def recent_scans(stick, n=10):
    root = os.path.join(stick["mount"], SCAN_SUBFOLDER)
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root), reverse=True)[:n]:
        d = os.path.join(root, name)
        if os.path.isdir(d):
            try:
                photos = sum(1 for f in os.scandir(d) if f.name.endswith(".jpg") and "_stitched" not in f.name)
            except OSError:
                photos = 0
            out.append({"name": name, "photos": photos})
    return out
